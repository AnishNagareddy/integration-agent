"""Interactive chat — the simplest way to use the agent.

    ./start.sh                     (first run also does setup)
    # or:  .venv/bin/python chat.py

Tell it what you need in plain English. It asks inline when it needs a clarification
or a decision. There are TWO approvals on the build path — first the PLAN (before any
code), then the BUILT connector (after it's generated + validated) — labelled 1/2 and 2/2.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time

import catalog
from config import get_settings
from graph import resume, submit

_APPROVE = {"approve", "yes", "y", "ok", "okay", "lgtm", "ship it", "looks good", "sure", "go", "approved"}
_REJECT = {"reject", "no", "n", "cancel", "stop", "nope"}


class _Spinner:
    """A tiny animated 'the agent is reasoning' indicator for the slow LLM/web steps."""

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        def run():
            for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                if self._stop.is_set():
                    return
                sys.stdout.write(f"\r  {ch}  {self.label}…   ")
                sys.stdout.flush()
                time.sleep(0.08)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.2)
        sys.stdout.write("\r" + " " * 48 + "\r")  # clear the spinner line
        sys.stdout.flush()


def _gate_answer(reply: str) -> dict:
    r = reply.strip().lower()
    if r in _APPROVE:
        return {"decision": "approve", "reviewer": "you"}
    if r in _REJECT:
        return {"decision": "reject", "reviewer": "you"}
    return {"decision": "request_changes", "notes": reply, "reviewer": "you"}  # anything else = change it


def _print_new_ai(res: dict, seen: list) -> None:
    msgs = res.get("messages", [])
    for m in msgs[seen[0] :]:
        if getattr(m, "type", "") == "ai":
            print(f"  · {getattr(m, 'content', '')}")
    seen[0] = len(msgs)


def _render_plan(iv: dict) -> None:
    pl = iv.get("plan") or {}
    print(
        f"   Build a '{pl.get('capability')}' connector  "
        f"(auth: {pl.get('auth_method') or '?'} · {pl.get('base_url_template') or '?'})"
    )
    for i, a in enumerate(pl.get("actions", []), 1):
        print(f"     {i}. {a['name']}  —  {a['http_method']} {a['path']}")
        if a.get("description"):
            print(f"        {a['description']}")
        inputs = a.get("input_fields") or []
        if inputs:
            shown = ", ".join(inputs[:6])
            more = f"  (+{len(inputs) - 6} more)" if len(inputs) > 6 else ""
            print(f"        inputs: {shown}{more}")
    for q in pl.get("open_questions", []):
        print(f"     ⚠ needs an answer: {q}")


def _run_ticket(res: dict) -> None:
    seen = [0]
    _print_new_ai(res, seen)
    while res.get("interrupted"):
        iv = res.get("interrupt") or {}
        gate = iv.get("gate")
        if gate == "plan":
            print("\n❓ APPROVAL 1 of 2 — review the PLAN (before any code is written):")
            _render_plan(iv)
            if iv.get("plan_file"):
                print(f"   📄 full plan written to: {iv['plan_file']}")
            print("   → reply: approve  /  reject  /  or say what to change")
        elif gate == "final":
            print("\n❓ APPROVAL 2 of 2 — review the generated CODE (it passed validation ✓):")
            print(f"     validation: {iv.get('validation')}")
            print(f"     💻 review the code in: {iv.get('review_dir')}")
            for f in iv.get("files", []):
                print(f"        {f}")
            print("   → reply: approve to register  /  reject  /  or say what to change")
        else:  # a clarification question
            print(f"\n❓ {res['prompt']}")
        reply = input("you> ").strip()
        if reply.lower() in {"quit", "exit"}:
            raise KeyboardInterrupt
        answer = _gate_answer(reply) if gate in ("plan", "final") else reply
        with _Spinner("working"):
            res = resume(res["ticket_id"], answer)
        _print_new_ai(res, seen)
    print(f"\n✅ {res['ticket_id']} → {res['status']}\n")


def main() -> int:
    try:
        get_settings().require_keys()
    except RuntimeError as e:
        print(e)
        return 1
    catalog.init_db()
    print('integration-agent · tell me what you need (e.g. "create a Jira ticket for the failed build").')
    print("Ctrl-C or 'exit' to quit.\n")
    while True:
        try:
            task = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            continue
        if task.lower() in {"quit", "exit"}:
            return 0
        try:
            with _Spinner("thinking"):
                res = submit(task)
            _run_ticket(res)
        except KeyboardInterrupt:
            print("\n(cancelled)\n")


if __name__ == "__main__":
    raise SystemExit(main())
