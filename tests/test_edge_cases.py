"""In-depth edge-case + systematic-error probes (fake LLM, real graph + harness)."""

import catalog
import nodes.detect
import nodes.generate
import nodes.plan
from graph import resume, submit
from providers import llm
from validation import validate

JIRA = "Create a Jira ticket for the failed build"
PLAN = {"actions": [{"name": "create_issue", "http_method": "POST", "path": "/issue"}]}

CONNECTOR_OK = '''\
from connectors.base import ActionSpec, BaseConnector, CapabilitySpec, HttpResponse


class JiraConnector(BaseConnector):
    capability = "jira"

    @classmethod
    def spec(cls):
        return CapabilitySpec(capability="jira", base_url_template="https://{site}.example/api",
            actions=[ActionSpec(name="create_issue", http_method="POST", path="/issue",
                                sample_input={"summary": "x"})])

    def create_issue(self, summary, **kw) -> HttpResponse:
        return self._request("POST", "/issue", json={"summary": summary})
'''

TEST_OK = '''\
from connectors.base import MockTransport
from jira_connector import JiraConnector


def test_ok():
    assert JiraConnector(transport=MockTransport()).create_issue("hi").status_code == 200
'''

CONNECTOR_RAISES = CONNECTOR_OK.replace(
    'return self._request("POST", "/issue", json={"summary": summary})',
    'raise ValueError("boom")',
)
CONNECTOR_BADRET = CONNECTOR_OK.replace(
    'return self._request("POST", "/issue", json={"summary": summary})',
    'return "not an HttpResponse"',
)
BROKEN = "def create_issue(self)\n    pass\n"  # syntax error (missing colon)


def _gc(connector=CONNECTOR_OK, test=TEST_OK):
    arts = [llm.GeneratedArtifact(filename="jira_connector.py", kind="connector", content=connector)]
    if test is not None:
        arts.append(llm.GeneratedArtifact(filename="test_jira_connector.py", kind="test", content=test))
    return llm.GeneratedCode(
        capability="jira", connector_class="JiraConnector", module_name="jira_connector", artifacts=arts
    )


def _dict(connector=CONNECTOR_OK, test=TEST_OK):
    return _gc(connector, test).model_dump()


# --------------------------------------------------------------------------- #
# Validation-harness failure modes (direct, fast)
# --------------------------------------------------------------------------- #
def test_missing_test_file_is_blocking():
    rep = validate(_dict(test=None), PLAN)
    gt = next(c for c in rep["checks"] if c["name"] == "generated_tests")
    assert not gt["passed"] and gt["blocking"]
    assert not rep["passed"]


def test_connector_import_error_is_caught():
    rep = validate(_dict("import totally_missing_pkg_xyz\n" + CONNECTOR_OK), PLAN)
    assert not rep["passed"]
    assert "import" in rep["failures"].lower()


def test_dry_run_catches_action_exception():
    rep = validate(_dict(CONNECTOR_RAISES), PLAN)
    dry = next(c for c in rep["checks"] if c["name"] == "dry_run")
    assert not dry["passed"]


def test_dry_run_rejects_non_httpresponse():
    rep = validate(_dict(CONNECTOR_BADRET), PLAN)
    dry = next(c for c in rep["checks"] if c["name"] == "dry_run")
    assert not dry["passed"]


def test_malformed_spec_fails_cleanly_without_crashing():
    bad_spec = CONNECTOR_OK.replace(
        "        return CapabilitySpec(",
        "        return None  # malformed\n        _unused = CapabilitySpec(",
    )
    rep = validate(_dict(bad_spec), PLAN)  # must return a failing report, not raise
    assert not rep["passed"]


# --------------------------------------------------------------------------- #
# Graph behavior / systematic errors
# --------------------------------------------------------------------------- #
def test_retry_recovers_on_second_attempt(isolated, fake_llm, monkeypatch):
    calls = {"n": 0}

    def gen(p, r, f="", ctx=""):
        calls["n"] += 1
        return _gc(BROKEN, test=None) if calls["n"] == 1 else _gc()

    monkeypatch.setattr(nodes.generate, "run_generate", gen)
    r = submit(JIRA)
    r2 = resume(r["ticket_id"], {"decision": "approve"})  # attempt 1 broken → retry → attempt 2 good
    assert calls["n"] == 2
    assert r2["interrupted"] and (r2["interrupt"] or {}).get("gate") == "final"  # recovered → Gate 2


def test_service_exists_missing_action_builds(isolated, fake_llm, monkeypatch):
    catalog.register(catalog.Capability(name="jira", supported_actions=["create_issue"]))
    monkeypatch.setattr(
        nodes.detect, "identify",
        lambda t: llm.Identified(capability="jira", actions=["transition_issue"], confidence=0.9),
    )
    r = submit("transition a jira issue to done")
    # jira exists but transition_issue is missing → build (not have_it) → pauses at a gate
    assert r["capability"] == "jira"
    assert r["interrupted"]  # entered the build path
    assert r["status"] != "no_action_needed"


def test_clarify_can_resolve_to_reuse(isolated, fake_llm, monkeypatch):
    monkeypatch.setattr(
        nodes.detect, "identify",
        lambda t: llm.Identified(capability="github", actions=["create_issue"], confidence=0.9)
        if "github" in t.lower()
        else llm.Identified(capability="", actions=[], confidence=0.1),
    )
    r = submit("help me file a thing somewhere")
    assert r["interrupted"] and (r["interrupt"] or {}).get("gate") is None  # clarify
    r2 = resume(r["ticket_id"], "github is fine")  # → detect github → have_it (reuse)
    assert r2["status"] == "no_action_needed"


def test_clarify_exhausts_and_fails_no_infinite_loop(isolated, fake_llm, monkeypatch):
    monkeypatch.setattr(
        nodes.detect, "identify", lambda t: llm.Identified(capability="", actions=[], confidence=0.1)
    )
    r = submit("something vague")
    tid = r["ticket_id"]
    for _ in range(6):  # bounded: MAX_CLARIFY=3 → must terminate well within 6
        if not r["interrupted"]:
            break
        r = resume(tid, "still vague")
    assert not r["interrupted"]
    assert r["status"] == "failed"


def test_plan_feedback_reaches_replan(isolated, fake_llm, monkeypatch):
    seen = {}

    def rp(cap, acts, research, feedback=""):
        seen["feedback"] = feedback
        return llm.ImplementationPlan(
            capability=cap, actions=[llm.PlannedAction(name="create_issue", http_method="POST", path="/issue")]
        )

    monkeypatch.setattr(nodes.plan, "run_plan", rp)
    r = submit(JIRA)  # → Gate 1
    r2 = resume(r["ticket_id"], {"decision": "request_changes", "notes": "use OAuth not basic"})
    assert seen["feedback"] == "use OAuth not basic"  # feedback threaded into the re-plan
    assert r2["interrupted"] and (r2["interrupt"] or {}).get("gate") == "plan"  # looped back to Gate 1


def test_plan_open_questions_are_asked_before_gate1(isolated, fake_llm, monkeypatch):
    calls = {"n": 0}

    def rp(c, a, r, f=""):
        calls["n"] += 1
        oq = ["Which project key should be used?"] if calls["n"] == 1 else []  # resolved after answer
        return llm.ImplementationPlan(
            capability=c,
            actions=[llm.PlannedAction(name="create_issue", http_method="POST", path="/issue")],
            open_questions=oq,
        )

    monkeypatch.setattr(nodes.plan, "run_plan", rp)
    r = submit(JIRA)  # plan has an open question → MUST be answered before Gate 1
    assert r["interrupted"] and (r["interrupt"] or {}).get("gate") is None  # a clarify, not the gate
    assert "project key" in (r["prompt"] or "").lower()

    r2 = resume(r["ticket_id"], "PLAT")  # answer → re-plan (no open qs) → Gate 1
    assert r2["interrupted"] and (r2["interrupt"] or {}).get("gate") == "plan"
    assert calls["n"] == 2


def test_gate2_request_changes_regenerates(isolated, fake_llm, monkeypatch):
    calls = {"n": 0}

    def gen(p, r, f="", ctx=""):
        calls["n"] += 1
        return _gc()

    monkeypatch.setattr(nodes.generate, "run_generate", gen)
    r = submit(JIRA)
    resume(r["ticket_id"], {"decision": "approve"})  # → Gate 2 (generate called once)
    r3 = resume(r["ticket_id"], {"decision": "request_changes", "notes": "add retries"})
    assert calls["n"] == 2  # regenerated
    assert r3["interrupted"] and (r3["interrupt"] or {}).get("gate") == "final"


def test_empty_task_goes_to_clarify(isolated, fake_llm):
    r = submit("")  # no service → unknown → clarify
    assert r["interrupted"] and (r["interrupt"] or {}).get("gate") is None


def test_known_service_but_no_action_asks_what_to_do(isolated, fake_llm, monkeypatch):
    # A vague "build an integration for retell" names a service but no operation → clarify,
    # NOT a confidently-invented action.
    def ident(t):
        acts = ["create_agent"] if "create" in t.lower() else []
        return llm.Identified(capability="retell", actions=acts, confidence=0.9)

    monkeypatch.setattr(nodes.detect, "identify", ident)
    r = submit("I'd like to build an integration for retell")
    assert r["interrupted"] and (r["interrupt"] or {}).get("gate") is None  # a clarify, not a gate
    assert "retell" in (r["prompt"] or "").lower()  # asks what retell should do

    r2 = resume(r["ticket_id"], "create an agent")  # now has an action → build → Gate 1
    assert r2["interrupted"] and (r2["interrupt"] or {}).get("gate") == "plan"


def test_conversation_accumulates_not_overwrites(isolated, fake_llm):
    r = submit(JIRA)
    msgs = r.get("messages", [])
    assert len(msgs) >= 3  # human task + detect + research + plan …
    assert any(getattr(m, "type", "") == "human" for m in msgs)
    assert sum(getattr(m, "type", "") == "ai" for m in msgs) >= 2  # multiple nodes appended
