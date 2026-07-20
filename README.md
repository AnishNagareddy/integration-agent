# integration-agent

An agent that **builds its own integrations**. Given a task that needs an external
system it doesn't yet support, it recognizes the gap, **researches the API from
the web**, **generates a connector (+ an MCP server)**, **validates the generated
code**, and holds the work as a **ticket that stays pending until a human approves
it** — twice. Only after approval is the new capability registered and reusable.

> **Example scenario:** a task requires **Jira**, but there's no Jira connector.
> The agent detects the gap, researches the Jira REST API, generates integration
> code, and treats the work as a ticket pending review/approval. Jira is just the
> *demo* — the agent is general (Stripe, Notion, …).

Built on **LangGraph** (the orchestration), **LangChain** (`init_chat_model` →
model-agnostic reasoning; **Claude** by default), and **SQLite** (catalog +
durable checkpoints).

---

## The workflow

```
   task
     │
     ▼
  ┌────────┐  detect: LLM identifies the service+actions; catalog says have/missing
  │ detect │ ──unknown──▶ ⏸ clarify ──(human answer)──▶ detect        (loops, bounded)
  └────────┘ ──have_it──▶ END (reuse)
     │ build
     ▼
  research ──▶ plan ──▶ ⏸ GATE 1 (approve the plan) ──approve──▶ generate ──▶ validate
   (web search)          │ reject → END                              ▲            │
                         │ request_changes → re-plan                 │ retry      │ pass
                         └───────────────────────────────────────────┘ (bounded) │
                                                                                  ▼
                                        register ◀──approve── ⏸ GATE 2 (approve the built code)
                                           │                    │ reject → END
                                           ▼                    │ request_changes → generate
                                      catalog (live)
```

- **`detect`** — LLM extracts the needed `service:action`; the **catalog** decides `have_it` / `build`; ambiguous → **clarify** (human).
- **`research`** — Claude's **web-search tool** reads the real docs (hunts the OpenAPI spec) → a structured `ResearchReport`.
- **`plan`** — distills research into a **build contract** (`ImplementationPlan`).
- **⏸ Gate 1** — a human approves/edits/rejects the *plan* (cheap, before any code).
- **`generate`** — Claude writes the connector + FastMCP server + tests.
- **`validate`** — a harness parses, dry-runs, and pytests the generated code; failures loop back to `generate` (bounded).
- **⏸ Gate 2** — a human approves the *built + validated* capability.
- **`register`** — writes the capability to the catalog + saves the code. The gap is closed.

Both ⏸ gates are LangGraph `interrupt()`s: the ticket **pauses and is checkpointed to SQLite**, so it can be approved in a separate process, later.

---

## Quickstart

**One command** (Python ≥ 3.10; first run sets up the venv + deps):

```bash
./start.sh
```

Put your `ANTHROPIC_API_KEY` in `.env` (created on first run) — the app is live-only
and won't start without it. Then just **talk to it**:

```
you> create a Jira ticket in project PLAT for the failed nightly build
  · No 'jira' integration yet — I'll build it (create_issue).
  · Researched jira: base_url=…, auth=basic_api_token, 5 endpoints, 11 sources.
  · Plan ready: ['create_issue'] …
❓ Approve this build plan?            → reply: approve / reject / or say what to change
you> approve
  · Generated ['jira_connector.py', 'jira_mcp_server.py'] · Validation PASSED
❓ Approve this built capability?      → reply: approve / reject / or say what to change
you> approve
✅ registered — jira is now available (re-ask for Jira later and it's reused, not rebuilt)
```

Ctrl-C or `exit` to quit. It asks inline whenever it needs a clarification or a decision.

**Things to try in the chat:**
- **build:** `create a Jira ticket for the failed build` → approve twice → registered
- **reuse:** `open a GitHub issue for the flaky test` → `no_action_needed` (a builtin)
- **clarify:** `help me stay on top of my work` → it asks which service → answer `jira`
- **change it:** at a gate, instead of `approve` type `use OAuth, not basic auth` → it re-plans

**Inspect what it built / reset:**
```bash
ls .data/artifacts/<capability>/                                # generated connector + MCP server + tests
sqlite3 .data/catalog.sqlite "select name,status,supported_actions from capability;"
rm -rf .data                                                    # wipe everything; next run starts fresh
```
Troubleshooting: a missing `ANTHROPIC_API_KEY` → add it to `.env` (tests don't need it);
editor import squiggles → point your IDE's interpreter at `.venv/bin/python`.

## Tests

```bash
.venv/bin/python -m pytest        # 28 tests, fast + deterministic — a fake LLM, no API calls
```
Covers the catalog, the validation harness (good code passes; syntax error / missing
method / wrong endpoint / import error / missing test all fail), and the full graph
lifecycle (build → 2 gates → register, plus reject, **retry-then-recover**,
retry-then-fail, clarify + exhaustion, and reuse).

---

## What's real vs. mocked

| Layer | Status |
|---|---|
| LLM reasoning (detect / research-extract / plan / generate) | **real** — Claude via `init_chat_model` (model-agnostic) |
| Web search | **real** — Claude's built-in `web_search` tool (Tavily is the documented model-agnostic swap) |
| Validation harness (parse / dry-run / pytest) | **real** — actually runs the generated code |
| Catalog, checkpoints, ticket lifecycle, gates | **real** — SQLite + LangGraph |
| **Tests** | use a **fake LLM** (test doubles) — the app itself is never mocked |

There is **no "mock mode."** Only the tests substitute a fake model; the running
app always uses real Claude.

## Project structure

```
chat.py          the interface — an interactive chat (start.sh launches it)
start.sh         one-command setup + launch
state.py         the ticket (LangGraph state) + lifecycle enum
config.py        settings from .env; live-only, fails fast without a key
catalog.py       SQLite (SQLModel): the capabilities catalog
graph.py         the LangGraph StateGraph: nodes, edges, gates, checkpointer, submit/resume
nodes/           detect · clarify · research · plan · approval (gates) · generate · validate · register · ask
providers/llm.py model-agnostic LLM + structured-output calls (identify/find_similar/research/plan/generate)
connectors/base.py  the interface generated connectors implement (+ MockTransport for dry-runs)
validation.py    the validation harness
tests/           fake-LLM test suite
```

See **[DESIGN.md](DESIGN.md)** for the full workflow rationale, the data model, and
the documented next-steps (ENHANCE, multi-capability, LangSmith/evals, richer plan schema).
