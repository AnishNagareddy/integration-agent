"""End-to-end graph tests with a fake LLM — the real wiring, no API."""

import catalog
import nodes.detect
import nodes.generate
from graph import resume, submit
from providers import llm

JIRA_TASK = "Create a Jira ticket for the failed nightly build"


def test_full_lifecycle_build_to_registered(isolated, fake_llm):
    # detect → research → plan → ⏸ Gate 1
    r = submit(JIRA_TASK)
    assert r["interrupted"]
    assert (r["interrupt"] or {}).get("gate") == "plan"
    assert not catalog.has("jira")

    # approve plan → generate → validate (pass) → ⏸ Gate 2
    r2 = resume(r["ticket_id"], {"decision": "approve"})
    assert r2["interrupted"]
    assert (r2["interrupt"] or {}).get("gate") == "final"
    assert r2["validation"]["passed"]

    # approve final → register → live in the catalog
    r3 = resume(r["ticket_id"], {"decision": "approve", "reviewer": "anish"})
    assert r3["status"] == "registered"
    assert catalog.has("jira")
    assert catalog.get("jira").supported_actions == ["create_issue"]

    # the loop is closed: a new Jira task is now reused, not rebuilt
    r4 = submit("Create another Jira ticket")
    assert r4["status"] == "no_action_needed"


def test_reject_plan_does_not_build(isolated, fake_llm):
    r = submit(JIRA_TASK)
    r2 = resume(r["ticket_id"], {"decision": "reject"})
    assert r2["status"] == "rejected"
    assert not catalog.has("jira")


def test_broken_code_retries_then_fails(isolated, fake_llm, monkeypatch, broken_code):
    monkeypatch.setattr(nodes.generate, "run_generate", lambda p, r, f="", ctx="": broken_code)
    r = submit(JIRA_TASK)
    r2 = resume(r["ticket_id"], {"decision": "approve"})  # generate(broken) → validate fail → retry×3
    assert r2["status"] == "failed"
    assert not catalog.has("jira")


def test_clarify_loop_resolves_to_build(isolated, fake_llm):
    r = submit("help me get organized this week")  # no service named → clarify
    assert r["interrupted"]
    assert (r["interrupt"] or {}).get("gate") is None  # a clarify, not a gate

    r2 = resume(r["ticket_id"], "actually I mean jira")  # → re-detect finds jira → build → Gate 1
    assert r2["interrupted"]
    assert (r2["interrupt"] or {}).get("gate") == "plan"


def test_ask_before_research_pauses_then_proceeds(isolated, fake_llm, monkeypatch):
    import nodes.research

    monkeypatch.setattr(nodes.research, "blocking_questions", lambda cap, acts: ["Cloud or self-hosted?"])
    r = submit(JIRA_TASK)
    assert r["interrupted"]
    assert (r["interrupt"] or {}).get("gate") is None  # a pre-research clarify, not a gate
    assert "Cloud or self-hosted?" in (r["prompt"] or "")

    r2 = resume(r["ticket_id"], "Cloud, API-token auth")  # → research → plan → Gate 1
    assert r2["interrupted"]
    assert (r2["interrupt"] or {}).get("gate") == "plan"


def test_ask_before_generate_pauses_then_builds(isolated, fake_llm, monkeypatch):
    import nodes.generate

    monkeypatch.setattr(nodes.generate, "build_blocking_questions", lambda plan: ["Which project key?"])
    r = submit(JIRA_TASK)
    r2 = resume(r["ticket_id"], {"decision": "approve"})  # Gate 1 approve → ask_before_generate pauses
    assert r2["interrupted"]
    assert (r2["interrupt"] or {}).get("gate") is None  # a clarify, not a gate
    assert "Which project key?" in (r2["prompt"] or "")

    r3 = resume(r["ticket_id"], "PLAT")  # answer → generate → validate → Gate 2
    assert r3["interrupted"]
    assert (r3["interrupt"] or {}).get("gate") == "final"


def test_existing_capability_short_circuits(isolated, fake_llm, monkeypatch):
    monkeypatch.setattr(
        nodes.detect, "identify",
        lambda t: llm.Identified(capability="github", actions=["create_issue"], confidence=0.9),
    )
    r = submit("open a github issue for the flaky test")
    assert r["status"] == "no_action_needed"
    assert not r["interrupted"]
