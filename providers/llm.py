"""Model-agnostic LLM access (Claude by default).

`init_chat_model` + `.with_structured_output(Schema)` → the model returns a
validated pydantic object instead of free text. Two small, separate structured
calls live here:
  • identify(task)              — what integration + actions does the task need?
  • find_similar(task, catalog) — which things we ALREADY do are closest? (for clarify)

Tests monkeypatch these with a fake; the app calls the real Claude.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from pydantic import BaseModel, Field

from config import get_settings


class Identified(BaseModel):
    """What `detect` asks the model to fill in."""

    capability: str = Field(
        description="canonical lowercase id, e.g. 'jira','github','stripe'. "
        "Empty string if the task names no specific external service."
    )
    actions: list[str] = Field(
        default_factory=list,
        description="operations implied, snake_case, e.g. ['create_issue','search_issues']",
    )
    confidence: float = Field(description="0..1 — how sure a specific integration was identified")


class SimilarMatch(BaseModel):
    capability: str = Field(description="an existing service id from the provided list")
    action: str = Field(description="the specific operation under it, e.g. 'create_issue'")
    why: str = Field(default="", description="short reason it's close to the task")


class SimilarResult(BaseModel):
    matches: list[SimilarMatch] = Field(default_factory=list)


@lru_cache
def _chat_model():
    """Build the chat model once (lazily). Fails fast if the key is missing."""
    from langchain.chat_models import init_chat_model

    settings = get_settings()
    settings.require_keys()
    return init_chat_model(settings.llm_model, model_provider=settings.llm_provider)


def identify(task: str) -> Identified:
    """What integration + actions does the task need?"""
    system = (
        "You identify which third-party integration/API a task needs. Return a canonical "
        "lowercase id (e.g. 'jira', 'github', 'stripe'). If the task names no specific external "
        "service, set capability='' and confidence low. For `actions`, list ONLY operations the task "
        "explicitly states or clearly implies (e.g. 'create a ticket' → ['create_issue']); if the user "
        "just names a service WITHOUT saying what to do with it, return an EMPTY actions list — do not "
        "invent one."
    )
    return _chat_model().with_structured_output(Identified).invoke(
        [("system", system), ("user", task)]
    )


def find_similar(task: str, available: list[str], limit: int = 3) -> SimilarResult:
    """Given the WHOLE catalog (as `service:action` pairs), return the closest few.

    `available` is action-granular on purpose: a service like Jira has many operations,
    so we match on the specific thing (`github:create_issue`), not just the service name.
    """
    system = (
        f"Here is everything we can ALREADY do, as service:action pairs: {available or '[]'}. "
        f"Given the user's task, return up to {limit} of THESE pairs whose purpose is closest to "
        "the task (closest first). Only choose from the list; return none if nothing is relevant."
    )
    return _chat_model().with_structured_output(SimilarResult).invoke(
        [("system", system), ("user", task)]
    )


# --------------------------------------------------------------------------- #
# research — web search (Claude tool) → structured report
# --------------------------------------------------------------------------- #
class Endpoint(BaseModel):
    method: str = ""
    path: str = ""
    purpose: str = Field(default="", description="which action this endpoint implements")


class Source(BaseModel):
    url: str = ""
    title: str = ""


class ResearchReport(BaseModel):
    capability: str = ""
    base_url: str = ""
    api_style: str = "rest"
    auth_method: str = Field(default="", description="e.g. basic_api_token | oauth2 | bearer | api_key")
    auth_notes: str = ""
    endpoints: list[Endpoint] = Field(default_factory=list)
    pagination: str = ""
    rate_limits: str = ""
    findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    openapi_spec_url: str = ""


class BlockingQuestions(BaseModel):
    questions: list[str] = Field(
        default_factory=list,
        description="questions whose answers you TRULY need from the user before you can research "
        "the API correctly (e.g. Cloud vs self-hosted, OAuth vs API token, region). Empty if you "
        "can research without asking. At most 3.",
    )


def blocking_questions(capability: str, actions: list[str]) -> list[str]:
    """Ask the model whether it needs anything from the human BEFORE researching."""
    system = (
        "Before researching a third-party API, list ONLY the questions whose answers you genuinely "
        "need from the user to research correctly. If you can proceed without asking, return an empty "
        "list. Never ask more than 3."
    )
    out = _chat_model().with_structured_output(BlockingQuestions).invoke(
        [("system", system), ("user", f"Integration: {capability}\nActions: {actions}")]
    )
    return out.questions


def build_blocking_questions(plan: dict) -> list[str]:
    """Ask the model whether it needs anything from the human BEFORE writing the code."""
    system = (
        "Before WRITING connector code for this plan, list ONLY questions whose answers you truly "
        "need from the user and that neither the plan nor your API knowledge can supply. In almost "
        "all cases return an empty list. Never ask more than 2."
    )
    out = _chat_model().with_structured_output(BlockingQuestions).invoke(
        [("system", system), ("user", f"Plan:\n{json.dumps(plan, indent=2)[:4000]}")]
    )
    return out.questions


def _text(msg) -> str:
    """Anthropic responses with tools come back as a list of content blocks; pull the text out."""
    content = msg.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


# Anthropic's server-side web-search tool (Claude-specific; Tavily is the model-agnostic swap).
_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}


def research(capability: str, actions: list[str], context: str = "") -> ResearchReport:
    """Two steps: (1) Claude reads the real docs via its web-search tool; (2) we extract a
    typed ResearchReport from those notes. `context` carries any answers the user gave to
    blocking questions (e.g. 'Cloud, API-token auth')."""
    prompt = (
        f"Research the official REST API of '{capability}'. First hunt for an OpenAPI/Swagger spec. "
        f"Determine: base URL, authentication method, and the exact endpoints implementing these "
        f"actions: {actions or ['core actions']}. Note pagination and rate limits. "
        "Prefer official developer docs over blogs, and cite the URLs you used."
    )
    if context:
        prompt += f"\n\nUser-provided specifics to respect: {context}"
    try:
        researcher = _chat_model().bind_tools([_WEB_SEARCH_TOOL])
        notes = _text(
            researcher.invoke(
                [
                    ("system", "You are an API integration researcher. Use web search to find primary docs."),
                    ("user", prompt),
                ]
            )
        )
    except Exception:
        # Web search not available → fall back to the model's own knowledge (still useful).
        notes = _text(_chat_model().invoke([("user", prompt)]))

    return _chat_model().with_structured_output(ResearchReport).invoke(
        [
            (
                "system",
                "Extract a precise structured report from the notes. Only include facts the notes "
                "support; leave fields empty if unknown.",
            ),
            ("user", f"Capability: {capability}\nActions: {actions}\n\nNotes:\n{notes}"),
        ]
    )


# --------------------------------------------------------------------------- #
# plan — research → build contract
# --------------------------------------------------------------------------- #
class PlannedAction(BaseModel):
    name: str = Field(description="snake_case action id, e.g. create_issue")
    http_method: str = ""
    path: str = Field(default="", description="endpoint path, e.g. /rest/api/3/issue")
    description: str = ""
    input_fields: list[str] = Field(default_factory=list, description="inputs the caller provides")
    output_fields: list[str] = Field(default_factory=list, description="key fields returned")


class ImplementationPlan(BaseModel):
    capability: str = ""
    base_url_template: str = Field(
        default="", description="e.g. https://{site}.atlassian.net/rest/api/3"
    )
    auth_method: str = ""
    actions: list[PlannedAction] = Field(default_factory=list)
    notes: str = ""
    open_questions: list[str] = Field(
        default_factory=list,
        description="ONLY blocking unknowns that prevent building a CORRECT connector for the "
        "requested actions and that you can't reasonably decide yourself (e.g. a required auth "
        "choice or an ambiguous base URL). NOT optional fields, full response schemas, pagination, "
        "or rate limits — use sensible defaults from your API knowledge. Prefer an empty list.",
    )


def plan(capability: str, actions: list[str], research: dict, feedback: str = "") -> ImplementationPlan:
    """Distill research into a concrete, minimal build contract — the thing both the
    generator and validator follow. Uses ONLY facts from the research. `feedback` is
    the reviewer's notes when a plan was sent back for changes at Gate 1."""
    system = (
        "You turn API research into a concrete, MINIMAL implementation plan — the contract the code "
        "generator and validator will both follow. For each requested action, give the HTTP method, "
        "endpoint path, the input fields the caller provides, and the key output fields. Use ONLY "
        "facts supported by the research. In `open_questions` put ONLY blocking unknowns that would "
        "prevent a correct build for the requested actions and that you can't reasonably decide "
        "yourself (e.g. a required auth method, an ambiguous base URL) — NOT optional fields, "
        "response schemas, pagination, or rate limits (use sensible defaults). Prefer an empty list."
    )
    user = (
        f"Capability: {capability}\n"
        f"Actions to implement: {actions or 'the core actions'}\n\n"
        f"Research (JSON):\n{json.dumps(research, indent=2)[:6000]}"
    )
    if feedback:
        user += f"\n\nA reviewer sent the previous plan back — address this feedback: {feedback}"
    return _chat_model().with_structured_output(ImplementationPlan).invoke(
        [("system", system), ("user", user)]
    )


# --------------------------------------------------------------------------- #
# generate — plan → code (connector + MCP server + tests)
# --------------------------------------------------------------------------- #
class GeneratedArtifact(BaseModel):
    filename: str = Field(description="e.g. jira_connector.py")
    kind: str = Field(description="connector | mcp_server | test")
    content: str = Field(description="the full file contents")


class GeneratedCode(BaseModel):
    capability: str = ""
    connector_class: str = Field(default="", description="e.g. JiraConnector")
    module_name: str = Field(default="", description="e.g. jira_connector (no .py)")
    artifacts: list[GeneratedArtifact] = Field(default_factory=list)


# The exact house interface, INCLUDING MockTransport/HttpResponse — so generated tests
# use the real API instead of inventing kwargs (a bug the live smoke caught).
_INTERFACE = '''\
# connectors.base — generated code targets this:
class BaseConnector:
    capability: str
    def __init__(self, config: dict | None = None, transport=None): ...
    @classmethod
    def spec(cls) -> CapabilitySpec: ...              # declares actions + a realistic sample_input each
    def _request(self, method, path, *, headers=None, params=None, json=None) -> HttpResponse: ...

class ActionSpec(BaseModel):      # fields: name, http_method, path, sample_input: dict
class CapabilitySpec(BaseModel):  # fields: capability, auth, base_url_template, actions: list[ActionSpec]

@dataclass
class HttpResponse:               # fields: status_code: int, json: dict, text: str

# For the TEST file only — the mock transport, EXACT signature (do not invent kwargs):
class MockTransport:
    def __init__(self, routes: dict[str, HttpResponse] | None = None): ...
        # routes: {"POST /path-substring": HttpResponse(201, {...})}; unmatched -> HttpResponse(200, {"ok": True})
    calls: list[dict]             # each recorded call: {"method","url","headers","params","json"}
'''


def _extract_code(text: str) -> str:
    """Pull code out of a reply: the first ```python fenced block, else the raw text."""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


def _snake(class_name: str) -> str:
    """JiraConnector -> jira_connector."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()


def _ask_code(system: str, user: str) -> str:
    return _extract_code(_text(_chat_model().invoke([("system", system), ("user", user)])))


def generate(plan: dict, research: dict, feedback: str = "", context: str = "") -> GeneratedCode:
    """Generate the connector + FastMCP server + pytest — ONE FILE PER CALL.

    One fenced block per call parses far more reliably than stuffing three files into one
    response (which came back empty in live testing). The test call is handed MockTransport's
    exact API so its assertions actually run. `context` = answers to pre-generate questions.
    """
    extra = ""
    if context:
        extra += f"\n\nUser-provided specifics to respect: {context}"
    if feedback:
        extra += f"\n\nThe previous attempt FAILED validation — fix exactly these problems:\n{feedback}"
    plan_json = json.dumps(plan, indent=2)[:4000]
    research_json = json.dumps(research, indent=2)[:1500]

    # 1) connector
    connector = _ask_code(
        "Write ONE Python connector module against the house interface below. Subclass BaseConnector, "
        "set `capability`, implement classmethod `spec()` (each ActionSpec has a REALISTIC sample_input "
        "whose keys match the method's parameters), and one method per action; ALL HTTP must go through "
        "self._request, and each method must RETURN the HttpResponse it returns (do NOT unwrap to .json). "
        "Build correct request bodies from your API knowledge. "
        f"Output ONLY the code in a single ```python block.\n{_INTERFACE}",
        f"Build contract (plan):\n{plan_json}\n\nResearch context:\n{research_json}{extra}",
    )
    m = re.search(r"class\s+(\w+)\s*\(\s*BaseConnector", connector)
    connector_class = m.group(1) if m else "Connector"
    module = _snake(connector_class)

    # 2) MCP server (wraps the connector)
    mcp_server = _ask_code(
        "Write ONE FastMCP server exposing each connector action as an @mcp.tool() that calls the "
        "connector. Use `from mcp.server.fastmcp import FastMCP` guarded in try/except so the file still "
        f"imports without `mcp`. Import the connector: `from {module} import {connector_class}`. "
        "Output ONLY the code in a single ```python block.",
        f"Connector:\n```python\n{connector}```",
    )

    # 3) pytest (uses MockTransport EXACTLY as documented — the bug the live smoke found)
    test = _ask_code(
        f"Write ONE pytest file for the connector. Import `from {module} import {connector_class}` and "
        "`from connectors.base import MockTransport, HttpResponse`. Build the connector with a "
        "MockTransport and assert each action issues the right HTTP method+path (inspect "
        "connector.transport.calls). Use MockTransport EXACTLY as documented — do NOT invent kwargs. "
        f"Output ONLY the code in a single ```python block.\n{_INTERFACE}",
        f"Connector:\n```python\n{connector}```",
    )

    return GeneratedCode(
        capability=plan.get("capability", ""),
        connector_class=connector_class,
        module_name=module,
        artifacts=[
            GeneratedArtifact(filename=f"{module}.py", kind="connector", content=connector),
            GeneratedArtifact(filename=f"{module}_mcp.py", kind="mcp_server", content=mcp_server),
            GeneratedArtifact(filename=f"test_{module}.py", kind="test", content=test),
        ],
    )
