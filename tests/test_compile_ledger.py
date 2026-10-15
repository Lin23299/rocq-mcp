"""Tests for the Stage-0 compile ledger (rocq_mcp.compile_ledger)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rocq_mcp import compile_ledger as cl


@pytest.fixture()
def ledger_dir(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("CCV_COMPILE_LEDGER_DIR", str(d))
    return d


def test_record_check_appends_non_empty_record(ledger_dir):
    ws = "/tmp/ws/proof/vst"
    ap = cl.record_check(ws, "lifecycle.v", proof_finished=False, goals_chars=120)
    assert ap is not None and ap.endswith("lifecycle.v")
    lines = Path(cl.ledger_path(ap)).read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["kind"] == "check" and rec["ok"] is True
    assert rec["non_empty"] is True and rec["goals_chars"] == 120


def test_record_check_proof_finished_counts_as_progress(ledger_dir):
    ap = cl.record_check("/tmp/ws/proof/vst", "f.v", proof_finished=True, goals_chars=0)
    rec = json.loads(Path(cl.ledger_path(ap)).read_text().splitlines()[-1])
    assert rec["non_empty"] is True and rec["proof_finished"] is True


def test_record_check_ignores_non_v_targets(ledger_dir):
    assert cl.record_check("/tmp/ws/proof/vst", "notes.md") is None
    assert cl.record_check("/tmp/ws/proof/vst", None) is None
    assert not any(ledger_dir.iterdir()) if ledger_dir.exists() else True


def test_record_check_best_effort_on_unwritable_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(cl, "ledger_dir", lambda: Path("/proc/definitely/not/writable"))
    # Must not raise; returns the path it would have used.
    ap = cl.record_check("/tmp/ws/proof/vst", "f.v")
    assert ap is not None


def test_norm_join_resolves_relative_against_workspace():
    assert cl.norm_join("/tmp/ws/proof/vst", "lifecycle.v") == "/tmp/ws/proof/vst/lifecycle.v"
    assert cl.norm_join(None, None) is None


def test_same_file_maps_to_single_ledger(ledger_dir):
    a = cl.record_check("/tmp/ws/proof/vst", "f.v")
    b = cl.record_check("/tmp/ws/proof/vst", "./f.v")
    assert a == b
    assert len(Path(cl.ledger_path(a)).read_text().splitlines()) == 2
