"""Tests for session staleness detection.

Unit tests exercise _check_staleness directly.
Integration tests mock pet to verify stale_warning propagates through
run_check and run_step_multi results.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rocq_mcp.interactive import _check_staleness, _StateEntry
from tests.conftest import make_lifespan_state


class TestCheckStaleness:
    """Unit tests for _check_staleness."""

    def test_no_warning_for_unchanged_file(self, tmp_path):
        """No warning when file hasn't been modified since session start."""
        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        mtime = os.path.getmtime(str(f))

        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=mtime,
            resolved_file=str(f),
        )
        assert _check_staleness(entry) is None

    def test_warning_on_modified_file(self, tmp_path):
        """Warning when file has been modified since session start."""
        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        old_mtime = os.path.getmtime(str(f))

        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=old_mtime,
            resolved_file=str(f),
        )

        # Modify the file (ensure mtime changes)
        time.sleep(0.05)
        f.write_text("Theorem t : False. Admitted.\n")
        os.utime(str(f), (time.time() + 1, time.time() + 1))

        warning = _check_staleness(entry)
        assert warning is not None
        assert "modified" in warning.lower()
        assert "rocq_start" in warning

    def test_no_warning_for_touch_only_change(self, tmp_path):
        """An mtime-only change with identical content is not stale."""
        from rocq_mcp.interactive import _file_content_hash

        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=os.path.getmtime(str(f)),
            resolved_file=str(f),
            file_hash=_file_content_hash(str(f)),
        )
        # Touch only: mtime changes, bytes stay identical.
        os.utime(str(f), (time.time() + 10, time.time() + 10))
        assert _check_staleness(entry) is None

    def test_warning_when_hash_differs(self, tmp_path):
        """A content change is stale even with a matching stored fingerprint."""
        from rocq_mcp.interactive import _file_content_hash

        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=os.path.getmtime(str(f)),
            resolved_file=str(f),
            file_hash=_file_content_hash(str(f)),
        )
        f.write_text("Theorem t : True. Proof. exact I. Qed. (* x *)\n")
        os.utime(str(f), (time.time() + 10, time.time() + 10))
        warning = _check_staleness(entry)
        assert warning is not None
        assert "not tracked" in warning.lower()
        assert "rocq_start" in warning

    def test_warning_on_deleted_file(self, tmp_path):
        """Warning when file has been deleted since session start."""
        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        mtime = os.path.getmtime(str(f))

        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=mtime,
            resolved_file=str(f),
        )

        f.unlink()
        warning = _check_staleness(entry)
        assert warning is not None
        assert "no longer accessible" in warning.lower()

    def test_no_warning_for_preamble_mode(self):
        """No warning for preamble-mode states (no backing file)."""
        entry = _StateEntry(
            state=None,
            file="<preamble>",
            theorem="<preamble>",
            workspace="/tmp",
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=None,
            resolved_file=None,
        )
        assert _check_staleness(entry) is None

    def test_no_warning_when_mtime_is_none(self, tmp_path):
        """No warning when file_mtime is None (OSError during capture)."""
        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")

        entry = _StateEntry(
            state=None,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=None,
            resolved_file=str(f),
        )
        assert _check_staleness(entry) is None


class TestVoEpochStaleness:
    """Dependency-.vo rebuild detection via the per-workspace .vo epoch (#29)."""

    def _entry(self, tmp_path, vo_epoch):
        # No backing .v (resolved_file=None) so only the epoch check applies.
        return _StateEntry(
            state=None,
            file="X.v",
            theorem="foo",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=None,
            resolved_file=None,
            vo_epoch=vo_epoch,
        )

    def test_warns_when_epoch_advanced(self, tmp_path):
        ls = make_lifespan_state()
        ls["vo_epochs"] = {str(tmp_path.resolve()): 1}
        entry = self._entry(tmp_path, vo_epoch=0)
        warning = _check_staleness(entry, ls)
        assert warning is not None
        assert "dependency .vo" in warning
        assert "rebuilt" in warning

    def test_no_warning_when_epoch_matches(self, tmp_path):
        ls = make_lifespan_state()
        ls["vo_epochs"] = {str(tmp_path.resolve()): 1}
        entry = self._entry(tmp_path, vo_epoch=1)
        assert _check_staleness(entry, ls) is None

    def test_no_warning_without_lifespan_state(self, tmp_path):
        # Backward-compatible one-arg call skips the epoch check entirely.
        entry = self._entry(tmp_path, vo_epoch=0)
        assert _check_staleness(entry) is None

    def test_epoch_helpers_bump_only_on_rebuild(self, tmp_path):
        import rocq_mcp.server as _server

        ls = make_lifespan_state()
        ws = str(tmp_path)
        key = str(tmp_path.resolve())
        assert _server._current_vo_epoch(ls, ws) == 0
        # No change in the snapshot -> no bump.
        _server._bump_vo_epoch_if_rebuilt(ls, ws, {"a.vo": 1.0}, {"a.vo": 1.0})
        assert _server._current_vo_epoch(ls, ws) == 0
        # A rewritten .vo -> bump.
        _server._bump_vo_epoch_if_rebuilt(ls, ws, {"a.vo": 1.0}, {"a.vo": 2.0})
        assert _server._current_vo_epoch(ls, ws) == 1
        # None snapshot (unscanned) -> no bump.
        _server._bump_vo_epoch_if_rebuilt(ls, ws, None, {"a.vo": 3.0})
        assert _server._current_vo_epoch(ls, ws) == 1
        assert ls["vo_epochs"] == {key: 1}

    def test_child_state_inherits_parent_epoch(self, tmp_path):
        """A child created after a rebuild inherits the parent's (older) epoch
        so it is still flagged stale (no false-fresh)."""
        import rocq_mcp.interactive as _it

        ls = make_lifespan_state()
        # Root stamped at epoch 0.
        root_id = _it._state_add(
            state=SimpleNamespace(proof_finished=False),
            file="X.v",
            theorem="foo",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            vo_epoch=0,
        )
        # A rebuild advances the workspace epoch to 1.
        ls["vo_epochs"] = {str(tmp_path.resolve()): 1}
        # Child created now; despite the current epoch being 1, it inherits
        # the parent's 0 (passing the current epoch as a would-be fallback).
        child_id = _it._state_add(
            state=SimpleNamespace(proof_finished=True),
            file="X.v",
            theorem="foo",
            workspace=str(tmp_path),
            parent_id=root_id,
            tactic="reflexivity.",
            step=1,
            vo_epoch=1,
        )
        try:
            child = _it._state_table[child_id]
            assert child.vo_epoch == 0
            assert _check_staleness(child, ls) is not None  # flagged stale
        finally:
            _it._state_remove(root_id)
            _it._state_remove(child_id)


# ---------------------------------------------------------------------------
# Integration: stale_warning in run_check results (mock-based, no pet)
# ---------------------------------------------------------------------------


class TestStalenessInRunCheck:
    """Verify stale_warning appears in run_check success/error results."""

    @pytest.fixture(autouse=True)
    def _setup_mock_state(self, tmp_path):
        """Set up a state entry with a stale file, mock pet."""
        import rocq_mcp.interactive as _int
        import rocq_mcp.server as _srv

        # Reset state table
        _int._state_invalidate_all()
        _srv._pet_semaphore = None

        # Create a file and record its mtime
        f = tmp_path / "test.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        old_mtime = os.path.getmtime(str(f))

        # Modify file so mtime changes
        time.sleep(0.05)
        f.write_text("Theorem t : True. Proof. exact I. Qed. (* changed *)\n")
        os.utime(str(f), (time.time() + 1, time.time() + 1))

        # Create a mock state with the OLD mtime (stale)
        mock_state = SimpleNamespace(st=1, proof_finished=False, feedback=[])
        self._state_id = _int._state_add(
            state=mock_state,
            file="test.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=old_mtime,
            resolved_file=str(f),
        )

        yield
        _int._state_invalidate_all()
        _srv._pet_semaphore = None

    @pytest.fixture(autouse=True)
    def _mock_pytanque(self):
        """Ensure pytanque is importable even if not installed."""
        if "pytanque" in sys.modules:
            yield
            return

        mock_module = SimpleNamespace(
            PetanqueError=type("PetanqueError", (Exception,), {"message": ""}),
            Pytanque=MagicMock,
            PytanqueMode=SimpleNamespace(STDIO="stdio"),
        )
        sys.modules["pytanque"] = mock_module
        yield
        sys.modules.pop("pytanque", None)

    @pytest.mark.asyncio
    async def test_untracked_stale_state_runs_with_warning(self):
        """Untracked file-backed states keep the warn-but-run fallback."""
        import rocq_mcp.server as _srv
        import rocq_mcp.interactive as _int

        new_state = SimpleNamespace(st=2, proof_finished=True, feedback=[])
        mock_pet = MagicMock()
        mock_pet.process = MagicMock()
        mock_pet.process.poll.return_value = None
        mock_pet.run.return_value = new_state
        mock_goals = SimpleNamespace(goals=[], stack=[], shelf=[], given_up=[])
        mock_pet.complete_goals.return_value = mock_goals

        lifespan_state = make_lifespan_state()
        lifespan_state["pet_client"] = mock_pet
        lifespan_state["current_workspace"] = "/tmp"

        with patch.object(_srv, "_ensure_pet", return_value=mock_pet):
            result = await _int.run_check(
                body="exact I.",
                timeout=30.0,
                lifespan_state=lifespan_state,
                from_state=self._state_id,
            )

        assert result["success"] is True
        assert "stale_warning" in result
        assert "modified" in result["stale_warning"].lower()

    @pytest.mark.asyncio
    async def test_stale_warning_in_error_response(self):
        """A tactic failure on an untracked stale state still reports the warning."""
        import rocq_mcp.server as _srv
        import rocq_mcp.interactive as _int
        from pytanque import PetanqueError

        mock_pet = MagicMock()
        mock_pet.process = MagicMock()
        mock_pet.process.poll.return_value = None
        try:
            err = PetanqueError(0, "No such tactic.")
        except TypeError:
            err = PetanqueError()
            err.message = "No such tactic."
        mock_pet.run.side_effect = err
        mock_pet.complete_goals.return_value = SimpleNamespace(
            goals=[], stack=[], shelf=[], given_up=[]
        )

        lifespan_state = make_lifespan_state()
        lifespan_state["pet_client"] = mock_pet
        lifespan_state["current_workspace"] = "/tmp"

        with patch.object(_srv, "_ensure_pet", return_value=mock_pet):
            result = await _int.run_check(
                body="bad_tactic.",
                timeout=30.0,
                lifespan_state=lifespan_state,
                from_state=self._state_id,
            )

        assert result["success"] is False
        assert result["reason"] == "tactic_failed"
        assert "stale_warning" in result
        assert "modified" in result["stale_warning"].lower()

    @pytest.mark.asyncio
    async def test_no_stale_warning_for_fresh_state(self, tmp_path):
        """run_check should NOT include stale_warning when file is unchanged."""
        import rocq_mcp.server as _srv
        import rocq_mcp.interactive as _int

        # Create a fresh (non-stale) state
        f = tmp_path / "fresh.v"
        f.write_text("Theorem t : True. Proof. exact I. Qed.\n")
        current_mtime = os.path.getmtime(str(f))

        mock_state = SimpleNamespace(st=10, proof_finished=False, feedback=[])
        fresh_id = _int._state_add(
            state=mock_state,
            file="fresh.v",
            theorem="t",
            workspace=str(tmp_path),
            parent_id=None,
            tactic=None,
            step=0,
            file_mtime=current_mtime,
            resolved_file=str(f),
        )

        new_state = SimpleNamespace(st=11, proof_finished=True, feedback=[])
        mock_pet = MagicMock()
        mock_pet.process = MagicMock()
        mock_pet.process.poll.return_value = None
        mock_pet.run.return_value = new_state
        mock_goals = SimpleNamespace(goals=[], stack=[], shelf=[], given_up=[])
        mock_pet.complete_goals.return_value = mock_goals

        lifespan_state = make_lifespan_state()
        lifespan_state["pet_client"] = mock_pet
        lifespan_state["current_workspace"] = str(tmp_path)

        with patch.object(_srv, "_ensure_pet", return_value=mock_pet):
            result = await _int.run_check(
                body="exact I.",
                timeout=30.0,
                lifespan_state=lifespan_state,
                from_state=fresh_id,
            )

        assert result["success"] is True
        assert "stale_warning" not in result
