"""The validation harness — runs the generated code so a human never reviews junk.

Layered, cheapest-decisive-first, in a throwaway temp dir:
  1. static   — every artifact parses (ast).
  2. schema   — the connector subclasses BaseConnector and exposes a method for every
                planned action.
  3. dry_run  — instantiate with a MockTransport and actually CALL each action with its
                sample_input; assert it hits the right method+path and returns 2xx. No
                network, no credentials.
  4. tests    — run the generated pytest file in a subprocess.

Returns a plain dict (JSON-simple for the checkpointer). Blocking failures feed the
generate→validate retry loop; the report is shown to the human at Gate 2.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

from connectors.base import BaseConnector, HttpResponse, MockTransport

REPO_ROOT = Path(__file__).resolve().parent


def validate(generated: dict, plan: dict) -> dict:
    checks: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ia_val_") as tmp:
        sandbox = Path(tmp)
        for art in generated.get("artifacts", []):
            (sandbox / art["filename"]).write_text(art["content"])

        checks.append(_static(generated))
        cls, err = _load(sandbox, generated)
        if cls is None:
            checks.append(_check("import_connector", "schema", False, err))
            return _report(checks)

        checks.append(_schema(cls, plan))
        checks.append(_dry_run(cls))
        checks.append(_tests(sandbox, generated))
    return _report(checks)


def _check(name, category, passed, detail="", blocking=True) -> dict:
    return {"name": name, "category": category, "passed": passed, "detail": detail, "blocking": blocking}


# 1. static -----------------------------------------------------------------
def _static(generated: dict) -> dict:
    for art in generated.get("artifacts", []):
        try:
            ast.parse(art["content"], filename=art["filename"])
        except SyntaxError as e:
            return _check("static_syntax", "static", False, f"{art['filename']}: {e}")
    return _check("static_syntax", "static", True, f"{len(generated.get('artifacts', []))} file(s) parse")


# import the generated connector fresh (safe across repeated runs) ----------
def _load(sandbox: Path, generated: dict):
    mod = generated.get("module_name", "")
    if not mod:
        return None, "no module_name on GeneratedCode"
    sys.modules.pop(mod, None)  # force a fresh import
    for p in (str(REPO_ROOT), str(sandbox)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        module = importlib.import_module(mod)
        importlib.reload(module)
        cls = getattr(module, generated.get("connector_class", ""), None)
        if cls is None:
            return None, f"class {generated.get('connector_class')} not found in {mod}"
        if not (isinstance(cls, type) and issubclass(cls, BaseConnector)):
            return None, f"{generated.get('connector_class')} does not subclass BaseConnector"
        return cls, ""
    except Exception as e:  # noqa: BLE001 — surface any import error to the ticket
        return None, f"{type(e).__name__}: {e}"
    finally:
        for p in (str(sandbox),):
            if p in sys.path:
                sys.path.remove(p)


# 2. schema / interface -----------------------------------------------------
def _schema(cls, plan: dict) -> dict:
    problems = []
    if not getattr(cls, "capability", ""):
        problems.append("connector.capability is empty")
    try:
        spec_names = set(cls.spec().action_names())
    except Exception as e:  # noqa: BLE001
        return _check("interface_schema", "schema", False, f"spec() raised: {e}")
    for action in plan.get("actions", []):
        name = action.get("name", "")
        if not callable(getattr(cls, name, None)):
            problems.append(f"planned action '{name}' has no method")
        if name not in spec_names:
            problems.append(f"planned action '{name}' missing from spec()")
    detail = "; ".join(problems) if problems else f"implements {sorted(spec_names)}"
    return _check("interface_schema", "schema", not problems, detail)


# 3. dry-run — actually call every action -----------------------------------
def _dry_run(cls) -> dict:
    transport = MockTransport()
    try:
        conn = cls(config={"site": "example", "email": "a@b.co", "api_token": "x"}, transport=transport)
    except Exception as e:  # noqa: BLE001
        return _check("dry_run", "dry_run", False, f"init failed: {e}")

    try:
        actions = cls.spec().actions  # guard: a malformed spec() fails validation, not the harness
    except Exception as e:  # noqa: BLE001
        return _check("dry_run", "dry_run", False, f"spec() unusable: {e}")

    problems = []
    for action in actions:
        method = getattr(conn, action.name, None)
        if not callable(method):
            problems.append(f"{action.name}: no method")
            continue
        before = len(transport.calls)
        try:
            result = method(**(action.sample_input or {}))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{action.name}: raised {type(e).__name__}: {e}")
            continue
        if not isinstance(result, HttpResponse) or result.status_code >= 400:
            problems.append(f"{action.name}: bad response {getattr(result, 'status_code', result)}")
            continue
        prefix = action.path.split("{")[0].rstrip("/") or action.path
        if not any(
            c["method"].upper() == action.http_method.upper() and prefix in c["url"]
            for c in transport.calls[before:]
        ):
            problems.append(f"{action.name}: expected {action.http_method} {prefix}, none seen")
    detail = "; ".join(problems) if problems else f"{len(actions)} action(s) dry-ran OK"
    return _check("dry_run", "dry_run", not problems, detail)


# 4. generated tests --------------------------------------------------------
def _tests(sandbox: Path, generated: dict) -> dict:
    if importlib.util.find_spec("pytest") is None:
        return _check("generated_tests", "tests", True, "pytest not installed; skipped", blocking=False)
    test_art = next((a for a in generated.get("artifacts", []) if a["kind"] == "test"), None)
    if test_art is None:
        # A generated test is REQUIRED — a missing one fails (feeds the retry loop).
        return _check("generated_tests", "tests", False, "no test file generated (a 'test' artifact is required)")

    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(sandbox), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", test_art["filename"]],
        cwd=str(sandbox), env=env, capture_output=True, text=True,
    )
    ok = proc.returncode == 0
    tail = (proc.stdout + proc.stderr).strip()[-400:]
    return _check("generated_tests", "tests", ok, "passed" if ok else tail)


def _report(checks: list[dict]) -> dict:
    blocking = [c for c in checks if c["blocking"]]
    passed = all(c["passed"] for c in blocking)
    score = sum(1 for c in checks if c["passed"]) / len(checks) if checks else 0.0
    summary = ", ".join(f"{c['name']}={'ok' if c['passed'] else 'FAIL'}" for c in checks)
    failures = "; ".join(f"{c['name']}: {c['detail']}" for c in checks if c["blocking"] and not c["passed"])
    return {"checks": checks, "passed": passed, "score": round(score, 2), "summary": summary, "failures": failures}
