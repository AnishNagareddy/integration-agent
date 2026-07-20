"""The CATALOG — a tiny SQLite database of "what integrations do we have?".

This is the source of truth the `detect` node queries. It's built with SQLModel:
you declare a normal Python class, and it becomes both a database table *and* a
typed object you get back from queries — no hand-written SQL.

Seeded with `github` and `slack` (and deliberately **no jira**) so the demo has a
real gap to fill. When a build is approved, `register()` adds a row here — and
from then on `has("jira")` is True.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine, select

from config import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Capability(SQLModel, table=True):
    """One row = one integration we can do. `table=True` makes it a real table."""

    name: str = Field(primary_key=True)  # canonical id, e.g. "jira" (also the dedup key)
    display_name: str = ""
    status: str = "active"  # active | building (the concurrency guard from the design)
    # a list stored as JSON in the cell — which actions this integration supports
    supported_actions: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    base_url_template: str = ""
    auth_method: str = ""
    code_ref: str = ""  # where the generated code lives (path now, git ref in prod)
    origin_ticket_id: str = ""  # lineage: which ticket built this
    approved_by: str = ""
    created_at: str = Field(default_factory=_now)


# One database engine for the whole app (built lazily so importing this file is cheap).
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.ensure_dirs()
        _engine = create_engine(f"sqlite:///{settings.catalog_db}")
    return _engine


# --- setup -----------------------------------------------------------------
def init_db() -> None:
    """Create the table(s) if missing and seed the built-in integrations. Idempotent."""
    SQLModel.metadata.create_all(_get_engine())
    _seed()


def _seed() -> None:
    builtins = [
        Capability(name="github", display_name="GitHub", supported_actions=["create_issue"]),
        Capability(name="slack", display_name="Slack", supported_actions=["post_message"]),
    ]
    with Session(_get_engine()) as session:
        for cap in builtins:
            if session.get(Capability, cap.name) is None:  # don't clobber on re-run
                session.add(cap)
        session.commit()


# --- queries (what `detect` uses) ------------------------------------------
def get(name: str) -> Capability | None:
    with Session(_get_engine()) as session:
        return session.get(Capability, name.lower())


def has(name: str) -> bool:
    return get(name) is not None


def all_capabilities() -> list[Capability]:
    with Session(_get_engine()) as session:
        return list(session.exec(select(Capability)).all())


# --- mutation (used by `register` after approval) --------------------------
def register(cap: Capability) -> None:
    """Insert or update a capability (upsert by name). This is how a newly-built
    integration becomes 'available'."""
    with Session(_get_engine()) as session:
        session.merge(cap)
        session.commit()
