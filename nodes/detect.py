"""NODE: detect — what integration does this task need, and do we already have it?

Two cleanly-separated questions:
  1. "What service + actions?"  → the LLM identifies it (any service, any phrasing).
  2. "Do we already have it?"    → the CATALOG answers — at `service:action` granularity,
     because a service like Jira has many operations.

Routing (see DESIGN "granularity" note for the full taxonomy):
  • unsure which service (no service / low confidence) → clarify ("which service?")
  • service clear but NO action specified (vague)      → clarify ("what should it do?")
  • service + ALL requested actions in catalog         → "have_it"  (reuse)
  • service absent, OR a requested action missing      → "build"    (a gap to fill)

The finer cases — EXTEND (service exists, add a missing action by reusing the
existing connector) and ENHANCE (update a working service:action) — are documented
but out of scope; a missing action routes to `build` for now.
"""

from __future__ import annotations

import catalog
from providers.llm import identify
from state import TicketState, TicketStatus

CONFIDENCE_THRESHOLD = 0.5


def detect(state: TicketState) -> dict:
    result = identify(state["task"])
    capability = result.capability.lower().strip()
    requested = [a.lower() for a in (result.actions or [])]

    # 1. Unsure which SERVICE → clarify (offer closest matches).
    if not capability or result.confidence < CONFIDENCE_THRESHOLD:
        return {
            "capability": "unknown",
            "requested_actions": [],
            "route": "unknown",
            "status": TicketStatus.FAILED.value,
            "messages": [{"role": "assistant", "content": "I couldn't identify a specific integration in that."}],
        }

    # 2. Service is clear, but the user didn't say WHAT to do → clarify the action.
    #    (A vague "build an integration for X" lands here instead of inventing an action.)
    if not requested:
        return {
            "capability": capability,
            "requested_actions": [],
            "route": "unknown",  # → clarify (bounded); status is transient
            "status": TicketStatus.FAILED.value,
            "messages": [
                {"role": "assistant", "content": f"I can build a '{capability}' integration — but what should it do?"}
            ],
        }

    existing = catalog.get(capability)

    # 2. Service doesn't exist at all → build it.
    if existing is None:
        acts = ", ".join(requested) or "actions TBD"
        return {
            "capability": capability,
            "requested_actions": requested,
            "route": "build",
            "status": TicketStatus.RESEARCHING.value,
            "messages": [{"role": "assistant", "content": f"No '{capability}' integration yet — I'll build it ({acts})."}],
        }

    # 3. Service exists — check at ACTION granularity.
    supported = set(existing.supported_actions or [])
    missing = [a for a in requested if a not in supported]

    if not missing:  # all requested actions already supported (or none requested) → reuse
        detail = f"already supports {requested}" if requested else "is already available"
        return {
            "capability": capability,
            "requested_actions": requested,
            "route": "have_it",
            "status": TicketStatus.NO_ACTION_NEEDED.value,
            "messages": [{"role": "assistant", "content": f"'{capability}' {detail} — reusing it."}],
        }

    # 4. Service exists but a needed action is missing → a gap to fill.
    #    (Reusing/extending the existing connector = EXTEND, documented; for now → build.)
    return {
        "capability": capability,
        "requested_actions": missing,  # focus the build on the missing action(s)
        "route": "build",
        "status": TicketStatus.RESEARCHING.value,
        "messages": [
            {"role": "assistant", "content": f"'{capability}' exists but is missing {missing} — I'll add {', '.join(missing)}."}
        ],
    }
