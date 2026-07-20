"""The GRAPH — detect + a human-in-the-loop clarify loop, with persistence.

Concepts here:
  1. CONDITIONAL EDGE — (state) -> next-node-name; branches build / clarify / done
  2. A CYCLE — clarify loops back to detect (LangGraph allows loops)
  3. interrupt() / Command(resume=...) — pause for a human, then continue
  4. CHECKPOINTER (SqliteSaver) — state saved after each node, keyed by thread_id,
     so a ticket can pause at the interrupt and resume later (even a new process)
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import catalog
from config import get_settings
from nodes.approval import final_approval, plan_approval
from nodes.clarify import clarify
from nodes.detect import detect
from nodes.generate import ask_before_generate, generate
from nodes.plan import plan, resolve_plan_questions
from nodes.register import register
from nodes.research import ask_before_research, research
from nodes.validate import validate
from state import TicketState, TicketStatus

MAX_CLARIFY = 3  # how many times we'll ask "which service?" before giving up
MAX_PLAN_QA = 2  # how many rounds of answering the plan's open questions before Gate 1


def route_after_detect(state: TicketState) -> str:
    """CONDITIONAL EDGE: turn detect's `route` into the next node."""
    route = state.get("route")
    if route == "build":
        return "build"
    if route == "have_it":
        return "done"
    # route == "unknown": ask the human, unless we've already asked too many times
    if state.get("clarify_attempts", 0) < MAX_CLARIFY:
        return "clarify"
    return "done"  # gave up; detect already set status = failed


def route_after_plan(state: TicketState) -> str:
    """CONDITIONAL EDGE: if the plan has open questions, make the user answer them
    (bounded) before the approval gate."""
    plan_dict = state.get("plan") or {}
    if plan_dict.get("open_questions") and state.get("plan_qa_attempts", 0) < MAX_PLAN_QA:
        return "resolve"
    return "approve"


def route_after_plan_approval(state: TicketState) -> str:
    """CONDITIONAL EDGE: branch on the human's Gate 1 decision."""
    decision = (state.get("approval") or {}).get("decision")
    if decision == "approve":
        return "approved"
    if decision in ("request_changes", "changes", "revise"):
        return "revise"
    return "rejected"


def route_after_validate(state: TicketState) -> str:
    """CONDITIONAL EDGE: pass → human gate; fail → retry generate (bounded) → else give up."""
    if (state.get("validation") or {}).get("passed"):
        return "pass"
    if state.get("attempts", 0) < state.get("max_attempts", 3):
        return "retry"  # bounded self-correction loop
    return "fail"


def route_after_final_approval(state: TicketState) -> str:
    """CONDITIONAL EDGE: branch on the human's Gate 2 decision."""
    decision = (state.get("approval") or {}).get("decision")
    if decision == "approve":
        return "approved"
    if decision in ("request_changes", "changes", "revise"):
        return "revise"
    return "rejected"


def build_graph(checkpointer):
    builder = StateGraph(TicketState)
    builder.add_node("detect", detect)
    builder.add_node("clarify", clarify)
    builder.add_node("ask_before_research", ask_before_research)
    builder.add_node("research", research)
    builder.add_node("plan", plan)
    builder.add_node("resolve_plan_questions", resolve_plan_questions)
    builder.add_node("plan_approval", plan_approval)
    builder.add_node("ask_before_generate", ask_before_generate)
    builder.add_node("generate", generate)
    builder.add_node("validate", validate)
    builder.add_node("final_approval", final_approval)
    builder.add_node("register", register)

    builder.add_edge(START, "detect")
    builder.add_conditional_edges(
        "detect",
        route_after_detect,
        {
            "build": "ask_before_research",  # build path: (optional) ask, then research
            "clarify": "clarify",
            "done": END,  # have_it / unknown-gave-up → finished
        },
    )
    builder.add_edge("clarify", "detect")  # loop back to re-detect with the answer
    builder.add_edge("ask_before_research", "research")  # ask first, spend (web search) later
    builder.add_edge("research", "plan")
    builder.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "resolve": "resolve_plan_questions",  # open questions → make the user answer them
            "approve": "plan_approval",  # ⏸ Gate 1
        },
    )
    builder.add_edge("resolve_plan_questions", "plan")  # re-plan with the answers
    builder.add_conditional_edges(
        "plan_approval",
        route_after_plan_approval,
        {
            "approved": "ask_before_generate",  # plan OK'd → (optional ask) → build
            "revise": "plan",  # loop back to re-plan with the reviewer's feedback
            "rejected": END,
        },
    )
    builder.add_edge("ask_before_generate", "generate")
    builder.add_edge("generate", "validate")
    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "pass": "final_approval",  # ⏸ Gate 2
            "retry": "generate",  # bounded self-correction loop
            "fail": END,  # retries exhausted; validate set status = failed
        },
    )
    builder.add_conditional_edges(
        "final_approval",
        route_after_final_approval,
        {
            "approved": "register",  # goes live in the catalog
            "revise": "generate",  # rebuild with the reviewer's notes
            "rejected": END,
        },
    )
    builder.add_edge("register", END)
    return builder.compile(checkpointer=checkpointer)


@contextmanager
def open_graph():
    settings = get_settings()
    settings.ensure_dirs()
    catalog.init_db()
    with SqliteSaver.from_conn_string(str(settings.checkpoints_db)) as checkpointer:
        yield build_graph(checkpointer)


def _report(graph, config) -> dict:
    """Read the current state + any pending interrupt into a simple dict."""
    snap = graph.get_state(config)
    interrupts = [i for task in snap.tasks for i in (task.interrupts or [])]
    iv = interrupts[0].value if interrupts else {}
    values = snap.values
    return {
        "ticket_id": config["configurable"]["thread_id"],
        "capability": values.get("capability"),
        "route": values.get("route"),
        "status": "waiting_for_input" if interrupts else values.get("status"),
        "interrupted": bool(interrupts),
        "prompt": iv.get("question") or iv.get("prompt"),  # clarify uses "question", gates use "prompt"
        "interrupt": iv or None,
        "messages": values.get("messages", []),
        "research": values.get("research"),
        "plan": values.get("plan"),
        "generated": values.get("generated"),
        "validation": values.get("validation"),
        "approval": values.get("approval"),
    }


def submit(task: str) -> dict:
    """Start a new ticket. Runs until it finishes OR pauses at an interrupt."""
    ticket_id = f"TKT-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": ticket_id}}
    initial: TicketState = {
        "task": task,
        "ticket_id": ticket_id,
        "status": TicketStatus.OPEN.value,
        "attempts": 0,
        "max_attempts": 3,
        "clarify_attempts": 0,
        "messages": [{"role": "user", "content": task}],
    }
    with open_graph() as graph:
        graph.invoke(initial, config=config)
        return _report(graph, config)


def resume(ticket_id: str, answer) -> dict:
    """Resume a paused ticket by handing the interrupt an answer."""
    config = {"configurable": {"thread_id": ticket_id}}
    with open_graph() as graph:
        graph.invoke(Command(resume=answer), config=config)
        return _report(graph, config)
