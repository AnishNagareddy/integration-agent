"""ask_user — the one clarification primitive.

Any node calls this to PAUSE the graph and ask the human a question. It's a thin
wrapper over LangGraph's `interrupt()`, so clarification is a one-liner anywhere:
`answer = ask_user(["Cloud or self-hosted?"])`. Placing the call at a node's *entry*
(before expensive work) keeps the "interrupt re-runs its node from the top" cost tiny.
"""

from __future__ import annotations

from langgraph.types import interrupt


def ask_user(questions: list[str], **extra) -> str:
    """Pause and ask the human one or more questions; returns their reply (freeform text)."""
    payload = {"question": " ".join(questions), "questions": questions}
    payload.update(extra)  # e.g. similar options, the plan — extra context for a UI
    return interrupt(payload)
