"""NODE: plan — distill the research into a build contract.

Takes the (dict) research report and produces a typed `ImplementationPlan`: exactly
which actions to implement, each with method + path + fields, plus auth + base URL.
The plan is the contract the generator builds to and the validator checks against —
and it's what the human reviews at Gate 1. Stored as a dict to keep state JSON-simple.
"""

from __future__ import annotations

from nodes.ask import ask_user
from providers.llm import plan as run_plan
from state import TicketState, TicketStatus


def plan(state: TicketState) -> dict:
    result = run_plan(
        state["capability"],
        state.get("requested_actions", []),
        state.get("research") or {},
        state.get("plan_feedback", ""),  # reviewer notes if this is a re-plan after Gate 1
    )
    names = [a.name for a in result.actions]
    return {
        "plan": result.model_dump(),
        "status": TicketStatus.PLANNING.value,
        "messages": [
            {
                "role": "assistant",
                "content": f"Plan ready: {names} via {result.auth_method or '?'} "
                f"on {result.base_url_template or '?'}.",
            }
        ],
    }


def plan_md(plan: dict) -> str:
    """Render the plan as a readable one-pager (written to PLAN.md for Gate-1 review)."""
    lines = [f"# Build plan — {plan.get('capability', '?')} connector", ""]
    lines.append(f"- **Auth:** {plan.get('auth_method') or '?'}")
    lines.append(f"- **Base URL:** {plan.get('base_url_template') or '?'}")
    if plan.get("notes"):
        lines.append(f"- **Notes:** {plan['notes']}")
    lines += ["", "## Actions"]
    for a in plan.get("actions", []):
        lines.append(f"\n### {a['name']} — `{a['http_method']} {a['path']}`")
        if a.get("description"):
            lines.append(a["description"])
        if a.get("input_fields"):
            lines.append(f"- **Inputs:** {', '.join(a['input_fields'])}")
        if a.get("output_fields"):
            lines.append(f"- **Returns:** {', '.join(a['output_fields'])}")
    if plan.get("open_questions"):
        lines += ["", "## Open questions"] + [f"- {q}" for q in plan["open_questions"]]
    return "\n".join(lines) + "\n"


def resolve_plan_questions(state: TicketState) -> dict:
    """If the plan has open questions, MAKE the user answer them (not just approve/reject),
    then fold the answers into a re-plan. Bounded by `plan_qa_attempts`."""
    questions = (state.get("plan") or {}).get("open_questions", [])
    answer = ask_user(questions)
    prior = state.get("plan_feedback", "")
    return {
        "plan_feedback": (prior + "\n" if prior else "") + "Answers to open questions: " + answer,
        "plan_qa_attempts": state.get("plan_qa_attempts", 0) + 1,
        "status": TicketStatus.PLANNING.value,
        "messages": [
            {"role": "assistant", "content": "Before I finalize the plan, please answer: " + " ".join(questions)},
            {"role": "user", "content": answer},
        ],
    }
