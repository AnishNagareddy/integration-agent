# DESIGN.md — how the agent works

The agent turns *"I got a task for a system I don't integrate with yet"* into a
**reviewed, registered capability**, for any integration. Jira is the demo.

## Guiding principle

**A fixed workflow with model-driven judgment at the joints.** The lifecycle
(detect → research → plan → build → validate → approve → register) never varies —
you always want those stages, in that order, with human gates. The *hard
reasoning inside* stages is where the LLM has latitude. That buys an auditable,
resumable, testable system without giving up flexibility where it matters. The
whole thing is a **LangGraph `StateGraph`** whose state *is the ticket*.

```
START → detect ─┬─ have_it ─────────────────────────────────────────────► END (reuse)
                ├─ unknown ─► ⏸ clarify ─(human)─► detect      (bounded loop)
                └─ build ─► ask?* ─► research ─► plan ─► ⏸ GATE 1 ─┬─ approve ─► ask?* ─► generate ─► validate ─┐
   (*ask? = an optional clarify: the node pauses to ask the user ONLY if the model says it needs specifics —
    ask_before_research asks about auth/deployment; ask_before_generate asks about build details. Both use the
    reusable ask_user() primitive, so any node can pause to ask with one line.)
                                                          ├─ reject ─► END                     │
                                                          └─ changes ─► plan                    │
                            generate ◀─ retry (bounded) ─ validate ◀───────────────────────────┘
                            validate ─ pass ─► ⏸ GATE 2 ─┬─ approve ─► register ─► END (live)
                                     ─ fail(exhausted) ─► END   ├─ reject ─► END
                                                                └─ changes ─► generate
```

Lifecycle states (`TicketStatus`): `open → detecting → researching → planning →
pending_plan_approval → generating → validating → pending_final_approval →
approved → registered`, with off-ramps `no_action_needed`, `rejected`, `failed`.

---

## The brief's eight questions

### 1. How the agent detects a missing capability
`nodes/detect.py`. Two separated questions:
- **What does the task need?** — the **LLM** (`providers.llm.identify`) returns
  `{capability, actions, confidence}` for *any* service, no hardcoded list.
- **Do we have it?** — the **catalog** (SQLite) answers, at **`service:action`
  granularity** (a service like Jira has many operations). Reuse only if the
  service *and all requested actions* are present.

Routing: unsure-which-service (low confidence) → `clarify`; **service named but no action**
(a vague "build an integration for X") → `clarify` "what should it do?" (identify is told *not*
to invent an action); present → `have_it`; missing → `build`. "Definitely not there" is never a
guess — the LLM confidently names a service and the catalog *lookup* confirms absence. The
confidence threshold is the dial between asking and building.

### 2. How it decides what to research
`nodes/research.py`. The needed actions (from detect) frame the research: find the
**base URL, auth, the exact endpoints for those actions, pagination, rate limits**.
It is told to **hunt for an OpenAPI/Swagger spec first** — a machine-readable spec
beats reading prose. First, **`ask_before_research`** lets the agent pause and ask
the user for blocking specifics (Cloud vs self-hosted, OAuth vs API token) — *only*
if the model says it needs them. It's a separate node *before* the web search on
purpose: an `interrupt()` re-runs its node from the top on resume, so keeping the
expensive search in its own downstream node means the search never re-runs.

### 3. How it searches the web and evaluates sources
`providers/llm.research` runs in **two steps**: (1) Claude with the **web-search
tool** reads primary docs and writes cited notes; (2) a structured-output call
distills those into a `ResearchReport` (base_url, auth, endpoints, sources,
`openapi_spec_url`). Source evaluation is **instructed** (prefer official developer
docs over blogs) and visible in the retained `sources`. *(Web search is
Claude-specific for now; Tavily is the documented model-agnostic swap — §Next.)*

### 4. How it turns research into an implementation plan
`nodes/plan.py`. A structured-output call distills the research into an
`ImplementationPlan` — the **build contract**: for each action, the HTTP method,
path, and input/output fields; plus auth + base URL. It's told to use *only* facts
from the research; uncertainties go to `open_questions` (surfaced at Gate 1).

### 5. How it generates code
`nodes/generate.py`. Claude writes the code **one file per call** (connector → MCP
server → test) — a single fenced block per call parses reliably, whereas asking for
all three in one structured object came back *empty* in live testing. Three artifacts
against the fixed house interface (`connectors.base.BaseConnector`): the **connector**
(logic; all HTTP via an injectable transport), a **FastMCP server** (each action as an
MCP tool), and a **pytest** file. The test call is handed `MockTransport`'s *exact* API
so its assertions actually run (another bug the live smoke caught). Prior validation
failures are fed back for self-correction (the retry loop fixed a wrong return type live).

### 6. How it validates the generated code
`validation.py` — a layered harness in a temp sandbox: **static** (`ast.parse`) →
**schema** (subclasses the interface, a method per planned action) → **dry-run**
(instantiate with `MockTransport`, call every action with its `sample_input`,
assert the right HTTP call + 2xx — no network, no creds) → **generated tests**
(run the emitted pytest — a test file is **required**; a missing one is a blocking
failure that feeds the retry). Blocking failures feed the bounded retry loop.

### 7. How the ticket lifecycle works
The ticket **is** the LangGraph state, persisted by a **SQLite checkpointer** keyed by
`thread_id = ticket_id`. "Pending" is literally the graph **paused at an `interrupt()`,
checkpointed** — the process can exit and the ticket resumes later with
`Command(resume=decision)`. The chat drives that resume inline; because it's durable, an
async front-end (resume by a separate reviewer, later) is a thin add.

### 8. What approval means
Two gates (`nodes/approval.py`). **Gate 1** approves the *plan* (cheap, before any
code). **Gate 2** approves the *built + validated* code before it goes live. A
capability is **complete only after Gate 2 → `register`** writes it to the catalog.
Passing validation makes it *reviewable*, not *done* — a human owns the final call
on a new integration.

---

## Where the agent pauses for a human (all touchpoints)

Every pause is a LangGraph `interrupt()` (durable — the ticket is checkpointed). Two
kinds: **clarifications** (agent asks a question) and **gates** (agent asks for a
decision). All clarifications go through one reusable `ask_user(questions)` primitive
(`nodes/ask.py`), so adding a new ask anywhere is a one-liner.

| # | Where | Kind | Trigger | You reply |
|---|---|---|---|---|
| 1 | `detect → clarify` | clarify | can't identify the service — OR a service with no action specified ("build an integration for X") | freeform text |
| 2 | `ask_before_research` | clarify | model needs coarse specifics (Cloud vs self-hosted, auth) | freeform text |
| 3 | `resolve_plan_questions` | clarify | the plan has **open questions** → you must answer them (bounded), then it re-plans | freeform text |
| 4 | **Gate 1** `plan_approval` | gate | always, after the plan is settled | approve / reject / say what to change |
| 5 | `ask_before_generate` | clarify | model needs build specifics the plan can't supply (rare) | freeform text |
| 6 | **Gate 2** `final_approval` | gate | always, after validation passes | approve / reject / say what to change |

In the chat, you just type the reply; `chat.py` maps a gate reply to a decision
(approve/reject, or *anything else* → a change request). Clarifications #2 and #4 only
fire if the model *says* it needs input (else the node is a no-op) — and each sits at its
stage's *entry*, before the expensive work, so an interrupt's node-restart never re-runs a
web search or a generation. The plan's `open_questions` aren't just shown — `resolve_plan_questions`
makes you answer them (bounded) and re-plans, so they're settled *before* Gate 1 rather than
dangling next to "approve." Everything else (research, generate, validate, retry, register) runs
without pausing. Saying "change X" at a gate is the reverse channel — it loops back to `plan`/`generate`.

## Data model (three stores, three jobs)

- **Checkpointer** (`.data/checkpoints.sqlite`) — LangGraph runtime state per
  `thread_id`; enables pause/resume. Not queried for business questions.
- **Catalog** (`.data/catalog.sqlite`, SQLModel) — `capabilities` (name, status
  `active|building`, `supported_actions`, auth, base_url, `code_ref`, lineage). This is
  what `detect` reads and `register` writes.
- **Artifacts** (`.data/artifacts/<capability>/`) — the generated code (git
  commit/tag in production).

Secrets are never stored — a connector stores its `auth_method`, not credentials.

---

## Granularity: the four-way taxonomy

A capability is `service:action`. detect distinguishes:
- **have_it** — service + all requested actions present → reuse.
- **build** — service absent → build new *(implemented)*.
- **extend** — service exists, a requested action missing → add it by reusing the
  existing connector *(documented, out of scope)*.
- **enhance** — a working `service:action` that must be *updated/tweaked* (API
  change, bug, new field) *(documented, out of scope)*.

EXTEND and ENHANCE both mean *editing existing generated code* — a riskier pipeline
than generating fresh, hence deferred.

---

## Status — what's built, what's mocked, how it's verified

**Built (real):** the full BUILD lifecycle as a LangGraph graph
(`detect → clarify? → research → plan → ⏸Gate 1 → generate → validate ↺ → ⏸Gate 2 → register`);
six human touchpoints (clarifies at detect, pre-research, plan-open-questions, and pre-generate; two gates)
via `interrupt()` + durable SQLite checkpointing; `detect` at `service:action` granularity that
clarifies a vague service *or* a missing action; `research` via Claude's web-search tool;
per-file `generate` (connector + FastMCP server + pytest); the validation harness (parse → schema →
dry-run every action → run generated tests); the SQLModel catalog + artifact persistence; and the
chat interface (`chat.py` / `start.sh`).

**Mocked:** *nothing in the running app* — it's live-only (real Claude + real web search; fails fast
without `ANTHROPIC_API_KEY`). Only the **tests** inject a fake LLM (test doubles, not an app "mode").
Web search is Claude-coupled for now — Tavily is the documented model-agnostic swap.

**Verified:** 28 fake-LLM tests + `ruff`, deterministic (routing, harness failure modes, full
lifecycle, retry-then-recover, clarify + exhaustion, reuse). Live: 10 diverse tasks all routed
correctly; a full run researched real Atlassian docs, generated a correct Jira connector (base64
auth, nested `fields.*` / ADF), validated, passed both gates, and registered — then a re-submitted
Jira task returned `no_action_needed` (reused). The live smoke also caught + fixed real bugs
(empty artifacts, wrong test API, a vague query inventing an action).

---

## Next steps (designed, not built)

- **ENHANCE / EXTEND** — modify or extend an existing connector (load its code,
  diff, re-validate, version-bump). Needs a code-editing pipeline. Adding an action
  would **append its test to the existing test file** (accumulating regression tests).
- **Multi-agent supervisor** — model `generate` (or multi-capability builds) as a
  supervisor over specialist **subgraphs**; a subagent's `interrupt()` propagates to
  the top graph, so questions relay to the user for free. Overkill for the current
  linear pipeline; the home for the `Send` fan-out.
- **Time-travel rebuilds** — `get_state_history()` + `update_state()` to fork from the
  `plan` checkpoint and re-generate with a tweaked plan *without* redoing research.
- **Freeform gate replies** — today the chat maps your reply to a decision with simple
  keyword matching (approve/reject, else "change it"); a small LLM classifier would handle
  arbitrary phrasing ("looks good, ship it" / "hold on, use OAuth").
- **Async / multi-reviewer front-end** — the durable checkpointer already supports resuming
  a ticket in a separate session; a thin command or web UI would let a *different* reviewer
  approve later. (Chat drives it synchronously today for simplicity.)
- **Multi-capability tasks** — `detect` returns a *list*; fan out one ticket per
  capability (LangGraph `Send`). Plus **concurrency/dedup**: a ticket *claims* a
  capability (`status='building'`, unique name) so two builds don't race.
- **Model-agnostic web search** — swap Claude's `web_search` for **Tavily** behind
  a one-file `WebResearcher` interface (restores full provider-agnosticism).
- **Semantic similarity for clarify** — embedding/vector search over capability
  descriptions (the Voyager pattern) instead of an LLM judgment, to scale.
- **Richer plan schema** — type + locate each field (path/query/body) and copy
  request/response schemas straight from the OpenAPI spec, for sturdier generation.
- **LangSmith tracing + evals** — env-var auto-tracing of every graph/LLM step;
  dataset + scorer regression evals with `agentevals` trajectory checks. The seams
  are ready — two env vars, zero code changes.
- **Production storage** — SQLite → Postgres (connection-string change); artifacts
  folder → a real git repo/PR.
- **Hardened codegen sandbox** — run generation/validation in an ephemeral,
  network-isolated container (see Risks).

---

## Risks & limitations

- **Generated code can be plausibly wrong** in ways a mock dry-run won't catch (a
  subtly-wrong payload). That's *why* human approval is mandatory; live contract
  tests against a sandbox account are the next safety layer.
- **Executing generated code is inherently risky.** Mitigated by only ever
  dry-running against a `MockTransport` (no network/creds pre-approval) and running
  tests in a subprocess — but this is **not a hardened sandbox** (no container
  isolation). Production would isolate it.
- **Confidently-wrong detection / prompt injection** — a hallucinated service, or a
  poisoned doc page in live web search, could steer the build. Caught partly by
  research finding no real sources and by the human gates; real defenses (treating
  fetched text as data, output constraints) are future work.
- **Single integration per ticket**; concurrency/dedup and ENHANCE are designed but
  not built.
- **Mock determinism flatters tests** — the fake-LLM suite proves *wiring*; live
  Claude behavior is the real test (and varies run to run).
