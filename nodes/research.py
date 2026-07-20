"""NODES: ask_before_research (optional clarify) + research.

`ask_before_research` lets the agent pause and ask the user *before* spending on web
search — the "ask first, spend later" split that avoids re-running the expensive
search on resume (the search lives in the separate `research` node). It only pauses
if the model says it genuinely needs input (auth style, Cloud vs self-hosted, …).

`research` uses Claude's web-search tool to read the real docs (OpenAPI spec first)
and returns a typed `ResearchReport` (stored as a dict to keep state JSON-simple).
"""

from __future__ import annotations

from nodes.ask import ask_user
from providers.llm import blocking_questions
from providers.llm import research as run_research
from state import TicketState, TicketStatus


def ask_before_research(state: TicketState) -> dict:
    if state.get("research_context"):  # already answered on a prior pass → don't re-ask
        return {}
    questions = blocking_questions(state["capability"], state.get("requested_actions", []))
    if not questions:
        return {}  # nothing to ask → straight to research

    # ⏸ pause; on resume `answer` is the user's reply (record Q&A AFTER the interrupt).
    answer = ask_user(questions)
    return {
        "research_context": answer,
        "messages": [
            {"role": "assistant", "content": "Before I research, I need: " + " ".join(questions)},
            {"role": "user", "content": answer},
        ],
    }


def research(state: TicketState) -> dict:
    report = run_research(
        state["capability"],
        state.get("requested_actions", []),
        state.get("research_context", ""),
    )
    return {
        "research": report.model_dump(),
        "status": TicketStatus.PLANNING.value,
        "messages": [
            {
                "role": "assistant",
                "content": (
                    f"Researched {report.capability or state['capability']}: "
                    f"base_url={report.base_url or '?'}, auth={report.auth_method or '?'}, "
                    f"{len(report.endpoints)} endpoint(s), {len(report.sources)} source(s)."
                ),
            }
        ],
    }
