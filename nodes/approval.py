"""NODE: plan_approval — Gate 1. Pause for a human to review the build PLAN.

Same interrupt()/resume machinery as `clarify`, but the payload is the plan and the
answer is a decision. Approving here means "yes, build to this plan" — it does NOT
mean the capability is done; that's Gate 2, after the code is generated and validated.

Resume value can be a plain string ("approve"/"reject") or a dict
{"decision": ..., "notes": ...} (notes drive a re-plan on request_changes).
"""

from __future__ import annotations

from langgraph.types import interrupt

from config import get_settings
from nodes.plan import plan_md
from nodes.register import write_artifacts
from state import TicketState, TicketStatus


def _normalize(raw) -> tuple[str, str]:
    if isinstance(raw, dict):
        return str(raw.get("decision", "reject")).lower(), raw.get("notes", "")
    return str(raw).strip().lower(), ""


def plan_approval(state: TicketState) -> dict:
    plan = state.get("plan") or {}
    # Write a readable one-pager the human can open + review, and point to it.
    art_dir = get_settings().artifacts_dir / (state.get("capability") or "unknown")
    art_dir.mkdir(parents=True, exist_ok=True)
    plan_file = art_dir / "PLAN.md"
    plan_file.write_text(plan_md(plan))

    decision, notes = _normalize(
        interrupt(
            {
                "gate": "plan",
                "prompt": "Approve this build plan? Reply approve / reject / request_changes (+notes).",
                "plan": plan,
                "plan_file": str(plan_file),
            }
        )
    )
    approval = {"gate": "plan", "decision": decision, "notes": notes}
    log = [
        {"role": "assistant", "content": "[Gate 1] Please review the build plan."},
        {"role": "user", "content": f"{decision}" + (f" — {notes}" if notes else "")},
    ]

    if decision == "approve":
        return {"approval": approval, "status": TicketStatus.GENERATING.value, "messages": log}
    if decision in ("request_changes", "changes", "revise"):
        return {
            "approval": approval,
            "plan_feedback": notes,
            "status": TicketStatus.PLANNING.value,
            "messages": log,
        }
    return {"approval": approval, "status": TicketStatus.REJECTED.value, "messages": log}


def final_approval(state: TicketState) -> dict:
    """Gate 2 — the human reviews the BUILT + VALIDATED capability before it goes live.

    approve → register · reject → done · request_changes → back to generate (with notes).
    Approving here is what makes the capability 'complete'.
    """
    gen = state.get("generated") or {}
    report = state.get("validation") or {}
    # Write the generated code to disk BEFORE asking, so the human reviews the real files.
    art_dir = write_artifacts(state["capability"], gen)
    files = [str(art_dir / a["filename"]) for a in gen.get("artifacts", [])]
    raw = interrupt(
        {
            "gate": "final",
            "prompt": "Approve this built capability for registration? approve / reject / request_changes (+notes).",
            "validation": report.get("summary"),
            "review_dir": str(art_dir),
            "files": files,
        }
    )
    decision, notes = _normalize(raw)
    reviewer = raw.get("reviewer", "human") if isinstance(raw, dict) else "human"
    approval = {"gate": "final", "decision": decision, "notes": notes, "reviewer": reviewer}
    log = [
        {"role": "assistant", "content": "[Gate 2] Review the built + validated capability."},
        {"role": "user", "content": f"{decision}" + (f" — {notes}" if notes else "")},
    ]

    if decision == "approve":
        return {"approval": approval, "status": TicketStatus.APPROVED.value, "messages": log}
    if decision in ("request_changes", "changes", "revise"):
        return {
            "approval": approval,
            "error": f"reviewer requested changes: {notes}",
            "status": TicketStatus.GENERATING.value,
            "messages": log,
        }
    return {"approval": approval, "status": TicketStatus.REJECTED.value, "messages": log}
