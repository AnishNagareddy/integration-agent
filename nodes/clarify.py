"""NODE: clarify — ask the human when detect is unsure, then loop back to re-detect.

Two cases:
  • service unknown  → offer the CLOSEST things we already do (one `find_similar` call
    over the whole catalog, at `service:action` granularity) and ask which.
  • service known but no action specified (a vague "build an integration for X") → ask
    WHAT it should do. No catalog lookup needed — we know the service.

Either way the freeform answer is appended to the task and we re-detect (bounded by
`clarify_attempts`). If the user names an existing one, detect routes to `have_it`.
"""

from __future__ import annotations

import catalog
from nodes.ask import ask_user
from providers.llm import find_similar
from state import TicketState, TicketStatus


def _catalog_pairs() -> list[str]:
    """Flatten the catalog to action granularity: ['github:create_issue', 'slack:post_message', ...]."""
    pairs: list[str] = []
    for cap in catalog.all_capabilities():
        for action in cap.supported_actions or []:
            pairs.append(f"{cap.name}:{action}")
    return pairs


def clarify(state: TicketState) -> dict:
    cap = (state.get("capability") or "").strip()

    if cap and cap != "unknown":
        # Service is clear; we just don't know what to do with it.
        question = (
            f"What would you like the '{cap}' integration to do? Name the operation(s) — "
            "e.g. create / list / update / delete a specific resource."
        )
        answer = ask_user([question], capability=cap)
    else:
        # Service itself is unclear → offer the closest things we already do.
        similar = find_similar(state["task"], _catalog_pairs(), limit=3).matches
        if similar:
            options = "; ".join(f"{m.capability} ({m.action})" for m in similar)
            question = (
                f"I'm not sure exactly what you need. The closest things I can already do: {options}. "
                "Is one of those it, or which service + action do you mean?"
            )
        else:
            question = "I couldn't tell what integration you need — which service + action do you mean?"
        answer = ask_user([question], similar=[m.model_dump() for m in similar])

    # ⏸ answer arrives from ask_user; record Q&A AFTER (lines above may re-run on resume).
    return {
        "task": f"{state['task']} {answer}",
        "clarify_attempts": state.get("clarify_attempts", 0) + 1,
        "status": TicketStatus.DETECTING.value,
        "messages": [
            {"role": "assistant", "content": question},
            {"role": "user", "content": answer},
        ],
    }
