"""The STATE — the single most important concept in LangGraph.

A LangGraph app is a state machine. This module defines the shared object — the
"ticket" — that flows through every node. Each node receives the current state,
returns a *partial* dict of just the keys it wants to change, and LangGraph
merges that back in. So the ticket accumulates structure as it moves through
detect → research → plan → generate → validate → approve → register.

We start with the foundation (what the first few nodes need) and add the richer
fields as we build the nodes that produce them — you'll watch the ticket grow.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class TicketStatus(str, Enum):
    """The lifecycle a ticket moves through.

    We list the whole map up front (even though we build the nodes for it
    incrementally) so the end-to-end flow is legible in one place. It's a
    `str` Enum so a status compares equal to its plain string value, which keeps
    routing and storage simple.
    """

    OPEN = "open"
    DETECTING = "detecting"
    NO_ACTION_NEEDED = "no_action_needed"  # capability already existed → short-circuit
    RESEARCHING = "researching"
    PLANNING = "planning"
    PENDING_PLAN_APPROVAL = "pending_plan_approval"  # ⏸ gate 1 (interrupt)
    GENERATING = "generating"
    VALIDATING = "validating"
    PENDING_FINAL_APPROVAL = "pending_final_approval"  # ⏸ gate 2 (interrupt)
    APPROVED = "approved"  # gate 2 passed; about to register
    REGISTERED = "registered"  # capability is live in the catalog
    REJECTED = "rejected"
    FAILED = "failed"  # retries exhausted / unrecoverable


class TicketState(TypedDict, total=False):
    """The shared ticket threaded through the graph.

    `total=False` means every key is optional — nodes fill keys in over time; at
    the very start only `task` is set. A node NEVER returns the whole state, only
    the keys it changed; LangGraph merges those in for us.
    """

    # --- input ---
    task: str  # the incoming request, e.g. "Create a Jira ticket for the failed build"
    ticket_id: str  # also used as the LangGraph thread_id (one ticket = one thread)

    # --- conversation ---
    # `add_messages` is a REDUCER. By default a returned key OVERWRITES the old
    # value; a reducer says how to COMBINE old + new instead. add_messages appends
    # new messages (and merges by id), so this list accumulates the whole dialogue
    # — user query, agent clarification questions, human answers. The checkpointer
    # then persists it for free, so a resumed ticket remembers the conversation.
    messages: Annotated[list, add_messages]

    # --- written by `detect` ---
    capability: str  # the target integration id, e.g. "jira"
    requested_actions: list[str]  # operations the task implies, e.g. ["create_issue"]
    route: str  # detect's decision: "build" | "have_it" | "unknown"

    # --- lifecycle bookkeeping ---
    status: str  # one of TicketStatus
    clarify_attempts: int  # how many times we've asked the human to clarify (bounds the loop)
    attempts: int  # generate/validate retries used so far
    max_attempts: int  # retry budget for the self-correction loop
    error: str  # last failure detail (fed back into generation)

    # --- written by `ask_before_research` / `research` ---
    research_context: str  # answers to any blocking questions asked before research
    # Stored as a plain dict (ResearchReport.model_dump()) so the checkpointer stays
    # JSON-simple — pydantic is used at the LLM boundary, dicts live in the state.
    research: dict

    # --- written by `plan` ---
    plan: dict  # ImplementationPlan.model_dump() — the build contract
    plan_feedback: str  # reviewer notes / answers to open questions, fed into a re-plan
    plan_qa_attempts: int  # bounds the "answer the plan's open questions" loop

    # --- written by `ask_before_generate` / `generate` / `validate` ---
    generate_context: str  # answers to any blocking questions asked before generation
    generated: dict  # GeneratedCode.model_dump() — connector + MCP server + tests
    validation: dict  # the harness report {checks, passed, score, summary, failures}

    # --- written by the approval gates ---
    approval: dict  # latest human decision at a gate: {gate, decision, notes}
