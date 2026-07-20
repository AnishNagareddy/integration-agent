"""NODE: validate — run the generated code through the harness.

On pass → the ticket is ready for a human (Gate 2). On (blocking) failure → record the
feedback; the graph's conditional edge decides whether to retry generation or give up.
"""

from __future__ import annotations

from state import TicketState, TicketStatus
from validation import validate as run_harness


def validate(state: TicketState) -> dict:
    report = run_harness(state["generated"], state["plan"])

    if report["passed"]:
        return {
            "validation": report,
            "status": TicketStatus.PENDING_FINAL_APPROVAL.value,  # → Gate 2 (next step)
            "messages": [{"role": "assistant", "content": f"Validation PASSED — {report['summary']}."}],
        }

    attempts = state.get("attempts", 0)
    exhausted = attempts >= state.get("max_attempts", 3)
    note = " [giving up — retries exhausted]" if exhausted else ""
    return {
        "validation": report,
        "error": report["failures"],  # fed back into the next generate attempt
        "status": (TicketStatus.FAILED if exhausted else TicketStatus.VALIDATING).value,
        "messages": [
            {"role": "assistant", "content": f"Validation FAILED (attempt {attempts}) — {report['failures']}{note}"}
        ],
    }
