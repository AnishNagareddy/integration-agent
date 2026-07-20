"""NODE: register — the capability goes live.

Only runs after Gate 2 approval. It writes a row into the CATALOG (the code is already
on disk from `write_artifacts`, which runs before Gate 2 so the human can review it).
From then on `detect` finds the capability (`have_it`), and the gap is closed. In
production the artifacts folder would be a git commit/tag instead — same idea, versioned.
"""

from __future__ import annotations

from pathlib import Path

import catalog
from config import get_settings
from state import TicketState, TicketStatus


def write_artifacts(capability: str, generated: dict) -> Path:
    """Write the generated files to `.data/artifacts/<capability>/` and return the dir.

    Called BEFORE Gate 2 (so the human reviews real files) and again by `register`
    (idempotent — same path, same content)."""
    art_dir = get_settings().artifacts_dir / capability
    art_dir.mkdir(parents=True, exist_ok=True)
    for art in generated.get("artifacts", []):
        (art_dir / art["filename"]).write_text(art["content"])
    return art_dir


def register(state: TicketState) -> dict:
    cap = state["capability"]
    gen = state.get("generated") or {}
    plan = state.get("plan") or {}

    art_dir = write_artifacts(cap, gen)  # idempotent — already written before Gate 2

    # write the catalog row → the capability is now "available"
    actions = [a["name"] for a in plan.get("actions", [])]
    catalog.register(
        catalog.Capability(
            name=cap,
            display_name=cap.title(),
            status="active",
            supported_actions=actions,
            auth_method=plan.get("auth_method", ""),
            base_url_template=plan.get("base_url_template", ""),
            code_ref=str(art_dir),
            origin_ticket_id=state.get("ticket_id", ""),
            approved_by=(state.get("approval") or {}).get("reviewer", "human"),
        )
    )
    return {
        "status": TicketStatus.REGISTERED.value,
        "messages": [
            {
                "role": "assistant",
                "content": f"Registered '{cap}' (actions={actions}). Code at {art_dir}.",
            }
        ],
    }
