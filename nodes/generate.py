"""NODES: ask_before_generate (optional clarify) + generate.

`ask_before_generate` mirrors `ask_before_research`: before spending on code
generation, it lets the agent pause and ask the user for anything truly blocking
that the plan can't supply (rare — it almost always proceeds). Same `ask_user`
primitive; same "ask at stage entry" placement.

`generate` turns the plan into code (connector + MCP server + tests). Prior
validation failures (`state["error"]`) and any pre-generate answers
(`state["generate_context"]`) are fed in so the model self-corrects.
"""

from __future__ import annotations

from nodes.ask import ask_user
from providers.llm import build_blocking_questions
from providers.llm import generate as run_generate
from state import TicketState, TicketStatus


def ask_before_generate(state: TicketState) -> dict:
    if state.get("generate_context"):  # already answered on a prior pass → don't re-ask
        return {}
    questions = build_blocking_questions(state.get("plan") or {})
    if not questions:
        return {}
    answer = ask_user(questions)
    return {
        "generate_context": answer,
        "messages": [
            {"role": "assistant", "content": "Before I build, I need: " + " ".join(questions)},
            {"role": "user", "content": answer},
        ],
    }


def generate(state: TicketState) -> dict:
    gen = run_generate(
        state["plan"],
        state.get("research") or {},
        state.get("error", ""),
        state.get("generate_context", ""),
    )
    attempts = state.get("attempts", 0) + 1
    return {
        "generated": gen.model_dump(),
        "attempts": attempts,
        "status": TicketStatus.VALIDATING.value,
        "error": "",  # clear prior feedback; validate sets it again if needed
        "messages": [
            {
                "role": "assistant",
                "content": f"Generated {[a.filename for a in gen.artifacts]} (attempt {attempts}).",
            }
        ],
    }
