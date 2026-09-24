"""Tests for file-progress bookkeeping (the "bookkeeper" invalidation model).

The tool never re-runs anything: it records how far into the session's file
snapshot each state's executed prefix reaches (``consumed``, advanced only
when a fed command matches the snapshot text verbatim), and on a file edit it
drops exactly the states reaching past the first changed byte.  A dropped
state answers later references with ``reason="state_invalidated"`` plus a
resume hint (``resume_from`` / ``resume_at``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rocq_mcp.interactive import (
    _FileSnapshot,
    _anchor_consumed,
    _build_check_success_dict,
    _first_difference,
    _line_col_to_offset,
    _match_command_at,
    _offset_to_line_col,
    _refresh_session_for,
    _sentence_end_offset,
    _session_snapshots,
    _state_add,
    _state_invalidate_all,
    _state_table,
)
from tests.conftest import make_lifespan_state

TEXT = (
    "Lemma a : True.\n"
    "Proof. exact I. Qed.\n"
    "Lemma b : True.\n"
    "Proof. exact I. Qed.\n"
)
A_END = TEXT.index("\n") + 1  # end of the first sentence


class TestPureHelpers:
    def test_first_difference(self):
        assert _first_difference("abc", "abc") == 3
        assert _first_difference("abc", "abd") == 2
        assert _first_difference("abc", "abcd") == 3
        assert _first_difference("", "x") == 0

    def test_offset_line_col_roundtrip(self):
        off = TEXT.index("Lemma b")
        line, char = _offset_to_line_col(TEXT, off)
        assert (line, char) == (2, 0)
        assert _line_col_to_offset(TEXT, line, char) == off

    def test_match_verbatim(self):
        end = _match_command_at(TEXT, 0, "Lemma a : True.")
        assert end == A_END - 1  # just past the terminating '.'

    def test_match_whitespace_flexible(self):
        # Fed with different internal whitespace than the file.
        end = _match_command_at(TEXT, 0, "Lemma   a\n :   True.")
        assert end == A_END - 1

    def test_match_rejects_variant(self):
        assert _match_command_at(TEXT, 0, "Lemma c : True.") is None
        assert _match_command_at(TEXT, 0, "Lemma a : False.") is None

    def test_match_rejects_token_prefix(self):
        # 'Lem' must not match 'Lemma' as a token.
        assert _match_command_at(TEXT, 0, "Lem") is None

    def test_match_skips_leading_whitespace(self):
        end = _match_command_at(TEXT, A_END, "\nProof. exact I. Qed.")
        assert end > A_END

    def test_sentence_end_skips_decimals_and_comments(self):
        text = "Definition x := 1.5. (* a. b. *) Definition y := 2.\nNext.\n"
        end1 = _sentence_end_offset(text, 0)
        assert text[:end1].endswith("1.5.")
        end2 = _sentence_end_offset(text, end1)
        assert text[end1:end2].strip().endswith("2.")
        assert "Next." not in text[:end2]

    def test_anchor_consumed_whitespace_gap(self):
        # Cursor in the gap before the second sentence -> dependency <= cursor.
        gap = A_END  # just after the newline, before "Proof."
        assert _anchor_consumed(TEXT, 1, 0) == gap

    def test_anchor_consumed_inside_sentence(self):
        # Cursor inside the first sentence -> dependency reaches its end.
        assert _anchor_consumed(TEXT, 0, 5) == A_END - 1

    def test_build_success_dict_includes_file_progress(self):
        result = _build_check_success_dict(
            goals_text="",
            proof_finished=False,
            commands_run=1,
            check_time_ms=1,
            state_id=1,
            from_state_id=1,
            feedback_pairs=[],
            stale_warning=None,
            complete=None,
            file_progress={"consumed": 5, "file_faithful": True},
        )
        assert result["file_progress"] == {"consumed": 5, "file_faithful": True}


@pytest.fixture(autouse=True)
def _clean_tables():
    _state_invalidate_all()
    yield
    _state_invalidate_all()


def _add_root(tmp_path, consumed: int, text: str = TEXT, name: str = "fp.v"):
    f = tmp_path / name
    f.write_text(text)
    sid = _state_add(
        state=SimpleNamespace(proof_finished=False, feedback=[]),
        file=name,
        theorem="@pos(0,0)",
        workspace=str(tmp_path),
        parent_id=None,
        tactic=None,
        step=0,
        consumed=consumed,
        resolved_file=str(f),
    )
    entry = _state_table[sid]
    entry.session_root = sid
    _session_snapshots[sid] = _FileSnapshot(path=str(f), text=text)
    return sid, entry, f


def _mock_pet():
    pet = MagicMock()
    pet.process = MagicMock()
    pet.process.poll.return_value = None
    pet.run.return_value = SimpleNamespace(proof_finished=False, feedback=[])
    pet.complete_goals.return_value = SimpleNamespace(
        goals=[], stack=[], shelf=[], given_up=[]
    )
    return pet


async def _run_check(from_state, lifespan_state, body, pet):
    import rocq_mcp.interactive as _int
    import rocq_mcp.server as _srv

    with patch.object(_srv, "_ensure_pet", return_value=pet):
        return await _int.run_check(
            body=body,
            timeout=30.0,
            lifespan_state=lifespan_state,
            from_state=from_state,
        )


class TestTrackedSessionSurvival:
    @pytest.mark.asyncio
    async def test_edit_after_consumed_keeps_state_usable(self, tmp_path):
        root, entry, f = _add_root(tmp_path, consumed=A_END)
        pet = _mock_pet()
        ls = make_lifespan_state()
        ls["pet_client"] = pet
        ls["current_workspace"] = str(tmp_path)

        # Edit well past the consumed offset.
        f.write_text(TEXT + "Lemma c : True.\n")
        result = await _run_check(root, ls, "cb_variant_tactic.", pet)

        assert result["success"] is True, result
        assert result["file_progress"]["consumed"] == A_END
        # The fed body is not the file text at the offset -> not faithful.
        assert result["file_progress"]["file_faithful"] is False
        pet.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_verbatim_command_advances_consumed(self, tmp_path):
        root, entry, f = _add_root(tmp_path, consumed=0)
        pet = _mock_pet()
        ls = make_lifespan_state()
        ls["pet_client"] = pet
        ls["current_workspace"] = str(tmp_path)

        result = await _run_check(root, ls, "Lemma a : True.", pet)

        assert result["success"] is True, result
        assert result["file_progress"]["file_faithful"] is True
        assert result["file_progress"]["consumed"] == A_END - 1


class TestInvalidation:
    @pytest.mark.asyncio
    async def test_edit_before_consumed_drops_state_and_returns_resume(
        self, tmp_path
    ):
        root, entry, f = _add_root(tmp_path, consumed=A_END)
        pet = _mock_pet()
        ls = make_lifespan_state()
        ls["pet_client"] = pet
        ls["current_workspace"] = str(tmp_path)

        # Change text *before* the consumed offset (insert at byte 0).
        f.write_text("(* new *) " + TEXT)
        result = await _run_check(root, ls, "Proof. exact I. Qed.", pet)

        assert result["success"] is False
        assert result["reason"] == "state_invalidated"
        assert result["invalidated"] is True
        assert result["resume_from"] is None  # nothing survives in this session
        assert result["resume_at"]["offset"] == 0
        pet.run.assert_not_called()
        assert root not in _state_table

    @pytest.mark.asyncio
    async def test_dropped_child_points_at_surviving_root(self, tmp_path):
        root, root_entry, f = _add_root(tmp_path, consumed=A_END)
        child = _state_add(
            state=SimpleNamespace(proof_finished=False, feedback=[]),
            file="fp.v",
            theorem="@pos(0,0)",
            workspace=str(tmp_path),
            parent_id=root,
            tactic="Proof.",
            step=1,
            consumed=len(TEXT),
        )
        _state_table[child].session_root = root

        # Edit between root's and child's offsets (insert after A_END).
        f.write_text(TEXT[:A_END] + "(* inserted *)\n" + TEXT[A_END:])
        _refresh_session_for(_state_table[root])

        assert root in _state_table  # survives
        assert child not in _state_table  # reaches past the edit -> dropped

        # Referencing the dropped child answers with the resume hint.
        pet = _mock_pet()
        ls = make_lifespan_state()
        ls["pet_client"] = pet
        ls["current_workspace"] = str(tmp_path)
        result = await _run_check(child, ls, "Proof.", pet)
        assert result["success"] is False
        assert result["reason"] == "state_invalidated"
        assert result["resume_from"] == root
        assert result["resume_at"]["offset"] == A_END
        pet.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_touch_only_edit_drops_nothing(self, tmp_path):
        import os
        import time as _time

        root, entry, f = _add_root(tmp_path, consumed=A_END)
        os.utime(str(f), (_time.time() + 10, _time.time() + 10))
        _refresh_session_for(_state_table[root])
        assert root in _state_table
