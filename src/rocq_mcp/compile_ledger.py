"""Run-scoped compile/interact ledger for the Stage-1 compile gate.

Append-only JSONL, one file per target `.v` (keyed by SHA-256 of the
normalized absolute path) under a user-level directory — deliberately
outside the audited workspace tree.  The OpenCode plugin reads the same
files to evaluate the four-condition deny rule (fix doc §10 v2); this
module only ever appends proof-side interactions (successful checks /
Qed) so condition (c) "no MCP progress since the failed compile" is
decidable.

Best-effort by design: a ledger failure must never break a proof call.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def ledger_dir() -> Path:
    """User-level ledger root (overridable for tests/sandboxes)."""
    return Path(os.environ.get("CCV_COMPILE_LEDGER_DIR",
                               str(Path.home() / ".ccv" / "compile-ledger")))


def norm_join(workspace: str | None, file: str | None) -> str | None:
    """Normalize the absolute path of a proof file (None when unknown)."""
    if not file:
        return None
    p = Path(file)
    if not p.is_absolute():
        p = Path(workspace or ".") / p
    return os.path.normpath(str(p))


def ledger_path(abs_file: str) -> Path:
    key = hashlib.sha256(abs_file.encode("utf-8")).hexdigest()[:16]
    return ledger_dir() / (key + ".jsonl")


def record_check(workspace: str | None, file: str | None, *,
                 proof_finished: bool = False, goals_chars: int = 0) -> str | None:
    """Append a successful-check record; returns the abs path or None.

    A check counts as MCP progress when the proof finished (e.g. a bare
    ``Qed.`` batch) or the response carried any goal text.
    """
    abs_file = norm_join(workspace, file)
    if not abs_file or not abs_file.endswith(".v"):
        return None
    rec = {
        "kind": "check",
        "ok": True,
        "non_empty": bool(proof_finished or (goals_chars or 0) > 0),
        "proof_finished": bool(proof_finished),
        "goals_chars": int(goals_chars or 0),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ledger_dir().mkdir(parents=True, exist_ok=True)
        with open(ledger_path(abs_file), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError as exc:  # best-effort: never break a proof call
        print(f"[compile-ledger] record failed (ignored): {exc}", file=sys.stderr)
    return abs_file
