"""Shared test fixtures.

- `isolated` — points the app at a throwaway data dir so tests never touch real
  .data (and resets the cached settings + catalog engine).
- `fake_llm` — swaps every LLM call for a deterministic fake (no API), the same
  "test doubles, not mock mode" approach we designed.
- `good_code` / `broken_code` — GeneratedCode objects for the harness + loop tests.
"""

from __future__ import annotations

import pytest

GOOD_CONNECTOR = '''\
from connectors.base import ActionSpec, BaseConnector, CapabilitySpec, HttpResponse


class JiraConnector(BaseConnector):
    capability = "jira"

    @classmethod
    def spec(cls):
        return CapabilitySpec(
            capability="jira", auth="basic_api_token",
            base_url_template="https://{site}.atlassian.net/rest/api/3",
            actions=[ActionSpec(name="create_issue", http_method="POST", path="/issue",
                                sample_input={"project_key": "PLAT", "summary": "x"})],
        )

    def create_issue(self, project_key, summary, **kwargs) -> HttpResponse:
        return self._request("POST", "/issue", json={"fields": {"project": {"key": project_key}, "summary": summary}})
'''

GOOD_TEST = '''\
from connectors.base import MockTransport
from jira_connector import JiraConnector


def test_create_issue():
    c = JiraConnector(config={"site": "acme"}, transport=MockTransport())
    r = c.create_issue("PLAT", "boom")
    assert r.status_code == 200
    assert c.transport.calls[-1]["method"] == "POST"
'''


def _good_generated():
    from providers import llm

    return llm.GeneratedCode(
        capability="jira", connector_class="JiraConnector", module_name="jira_connector",
        artifacts=[
            llm.GeneratedArtifact(filename="jira_connector.py", kind="connector", content=GOOD_CONNECTOR),
            llm.GeneratedArtifact(filename="test_jira_connector.py", kind="test", content=GOOD_TEST),
        ],
    )


def _broken_generated():
    from providers import llm

    return llm.GeneratedCode(
        capability="jira", connector_class="JiraConnector", module_name="jira_connector",
        artifacts=[llm.GeneratedArtifact(filename="jira_connector.py", kind="connector",
                                         content="def create_issue(self)\n    pass\n")],  # syntax error
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / ".data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")  # so require_keys() passes if ever hit
    import catalog
    import config

    config.get_settings.cache_clear()
    catalog._engine = None
    catalog.init_db()
    yield
    config.get_settings.cache_clear()
    catalog._engine = None


@pytest.fixture
def fake_llm(monkeypatch):
    import nodes.clarify
    import nodes.detect
    import nodes.generate
    import nodes.plan
    import nodes.research
    from providers import llm

    def identify(task):
        if "jira" in task.lower():
            return llm.Identified(capability="jira", actions=["create_issue"], confidence=0.9)
        return llm.Identified(capability="", actions=[], confidence=0.1)  # ambiguous → clarify

    monkeypatch.setattr(nodes.detect, "identify", identify)
    monkeypatch.setattr(nodes.research, "blocking_questions", lambda cap, acts: [])  # no pre-research Qs
    monkeypatch.setattr(nodes.research, "run_research",
        lambda c, a, ctx="": llm.ResearchReport(capability=c, base_url="https://x.atlassian.net/rest/api/3",
            auth_method="basic_api_token",
            endpoints=[llm.Endpoint(method="POST", path="/issue", purpose="create_issue")]))
    monkeypatch.setattr(nodes.plan, "run_plan",
        lambda c, a, r, f="": llm.ImplementationPlan(capability=c,
            base_url_template="https://{site}.atlassian.net/rest/api/3", auth_method="basic_api_token",
            actions=[llm.PlannedAction(name="create_issue", http_method="POST", path="/issue")]))
    monkeypatch.setattr(nodes.generate, "build_blocking_questions", lambda plan: [])  # no pre-gen Qs
    monkeypatch.setattr(nodes.generate, "run_generate", lambda p, r, f="", ctx="": _good_generated())
    monkeypatch.setattr(nodes.clarify, "find_similar", lambda task, avail, limit=3: llm.SimilarResult(matches=[]))


@pytest.fixture
def good_code():
    return _good_generated()


@pytest.fixture
def broken_code():
    return _broken_generated()
