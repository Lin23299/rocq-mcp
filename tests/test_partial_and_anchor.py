"""Tests for failure-state honesty (``partial``) and anchor self-description.

- ``_build_check_failure_dict`` must mark mid-body failure states as
  ``partial`` and must not recommend them as "the state before the next
  file sentence"; the hint points at the original base ``from_state``.
- Position-based starts must echo the anchored line text + file
  fingerprint so callers can confirm which sentence they anchored to
  after edits shifted line numbers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from rocq_mcp.interactive import (
    _build_check_failure_dict,
    _build_position_start_result,
    _read_line_text,
)
from tests.conftest import make_lifespan_state


class TestPartialFailureEnvelope:
    @staticmethod
    def _build(command_index: int, last_valid: int = 7, base: int = 3) -> dict:
        return _build_check_failure_dict(
            error_message="Coq: nope.",
            failed_command="bad.",
            command_index=command_index,
            last_valid_state_id=last_valid,
            base_state_id=base,
            goals_at_failure=None,
            feedback_pairs=[],
            stale_warning=None,
        )

    def test_first_command_failure_is_not_partial(self):
        result = self._build(0, last_valid=3, base=3)
        assert result["partial"] is False
        assert result["commands_run"] == 0
        assert result["last_valid_state_id"] == 3
        assert "from_state=3" in result["hint"]
        assert "No command ran" in result["hint"]

    def test_mid_body_failure_is_marked_partial(self):
        result = self._build(2)
        assert result["partial"] is True
        assert result["commands_run"] == 2
        # The partial state is named as partial; resume points at the base.
        assert "from_state=7 is a PARTIAL state" in result["hint"]
        assert "original from_state (3)" in result["hint"]

    def test_partial_without_base_falls_back_to_original(self):
        result = _build_check_failure_dict(
            error_message="Coq: nope.",
            failed_command="bad.",
            command_index=1,
            last_valid_state_id=9,
            base_state_id=None,
            goals_at_failure=None,
            feedback_pairs=[],
            stale_warning=None,
        )
        assert result["partial"] is True
        assert "original from_state" in result["hint"]


class TestReadLineText:
    def test_reads_0_indexed_line(self, tmp_path):
        f = tmp_path / "x.v"
        f.write_text("line0\nline1\nline2\n")
        assert _read_line_text(str(f), 1) == "line1"

    def test_missing_line_returns_none(self, tmp_path):
        f = tmp_path / "x.v"
        f.write_text("only\n")
        assert _read_line_text(str(f), 5) is None

    def test_unreadable_returns_none(self, tmp_path):
        assert _read_line_text(str(tmp_path / "nope.v"), 0) is None


class TestPositionAnchorEcho:
    def test_anchor_describes_line_and_fingerprint(self, tmp_path):
        import rocq_mcp.interactive as _int

        _int._state_invalidate_all()
        try:
            f = tmp_path / "anchor.v"
            f.write_text("Theorem t : True.\nProof. exact I. Qed.\n")
            pet = MagicMock()
            pet.get_state_at_pos.return_value = SimpleNamespace(proof_finished=False)
            pet.complete_goals.return_value = SimpleNamespace(
                goals=[], stack=[], shelf=[], given_up=[]
            )
            ls = make_lifespan_state()
            result = _build_position_start_result(
                pet,
                file="anchor.v",
                resolved_file=str(f),
                workspace=str(tmp_path),
                lifespan_state=ls,
                line=1,
                character=0,
            )
            assert result["success"] is True
            anchor = result["anchor"]
            assert anchor["file"] == "anchor.v"
            assert anchor["line"] == 1
            assert anchor["character"] == 0
            assert anchor["line_text"] == "Proof. exact I. Qed."
            assert anchor["file_hash"], "fingerprint must be echoed"
        finally:
            _int._state_invalidate_all()
