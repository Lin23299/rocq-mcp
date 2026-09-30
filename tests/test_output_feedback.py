"""Batch feedback is saved before per-step and aggregate display limits."""

from __future__ import annotations

from collections import deque
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

import rocq_mcp.interactive as interactive
import rocq_mcp.server as server
from rocq_mcp.output_store import OutputStore, OutputStoreError


@pytest.fixture
def proof_boundary(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    parent = SimpleNamespace(proof_finished=False, feedback=[])
    state_id = interactive._state_add(parent, "Example.v", "t", str(ws), None, None, 0)
    answers = {}

    class Pet:
        @staticmethod
        def run(_state, command, **_kw):
            value = answers[command]
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(proof_finished=False, feedback=[(3, value)])

        @staticmethod
        def complete_goals(_state):
            return SimpleNamespace(goals=[], stack=[], shelf=[], given_up=[])

    async def run_with_pet(fn, _state, _tool, **_kwargs):
        try:
            return fn(Pet())
        except TimeoutError:
            return {"success": False, "reason": "timeout", "error": "outer timeout",
                    **_kwargs["partial_state"]}

    monkeypatch.setattr(server, "_run_with_pet", run_with_pet)
    monkeypatch.setattr(server, "_set_workspace_if_needed", lambda *args: None)
    monkeypatch.setattr(interactive, "_check_staleness", lambda *args: None)
    with OutputStore(tmp_path / "private", workspace=ws, allow_test_temp_root=True) as store:
        ctx = SimpleNamespace(lifespan_context={
            "pet_timeout": 30, "recent_errors": deque(maxlen=20),
            "output_stdio": True, "output_principal": "single-stdio-client",
            "output_store": store,
        })
        yield ws, state_id, answers, ctx, store


@pytest.mark.asyncio
async def test_short_check_feedback_preserved_without_disk(proof_boundary):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "tiny result"
    result = await server.rocq_check("first.", from_state=state_id, ctx=ctx)
    assert result["success"] and result["feedback"] == [["first.", "tiny result"]]
    assert result["view_status"] == "complete"
    assert result["warning_filter"] == "include_warnings"
    assert not store._entries


def test_omitted_proof_tactics_reports_real_serialized_field_size():
    tactics = ["λ" * 500 for _ in range(50)]
    raw = json.dumps(tactics, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    result = {"success": True, "proof_tactics": tactics,
              "view_status": "complete", "views": {}}
    server._batch_auxiliary_view(result)
    assert result["proof_tactics"] == []
    assert result["proof_tactics_count"] == 50
    assert result["view_status"] == "partial_unrecoverable"
    assert result["views"]["proof_tactics"]["total_bytes"] == len(raw) > 0
    assert result["views"]["proof_tactics"]["shown_bytes"] == 0


@pytest.mark.asyncio
async def test_small_known_error_keeps_inline_text_without_snapshot(proof_boundary):
    ws, _state_id, _answers, ctx, store = proof_boundary
    original = {"success": False, "reason": "tactic_failed",
                "failed_command": "bad.", "error": "ERR" * 600}
    result = await server._diagnostic_views(
        dict(original), original=original, ctx=ctx,
        workspace=str(ws), origin="rocq_check",
    )
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["error"] == original["error"] and result["view_status"] == "complete"
    assert result["views"] == {} and not store._entries


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [512, 513, 1024, 1025])
@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("location", ["error", "failed_command", "results[0].error"])
async def test_audit8_diagnostic_thresholds_follow_actual_final_projection(proof_boundary, size, oversized, location):
    from mcp.types import TextContent

    ws, sid, _answers, ctx, store = proof_boundary
    tail = "MID_DIAGNOSTIC_TAIL"
    text = "E" * (size - len(tail)) + tail
    original = {"success": False, "reason": "tactic_failed", "state_id": sid}
    if location.startswith("results"):
        original = {"success": True, "from_state_id": sid,
                    "results": [{"success": False, "reason": "tactic_failed", "error": text}]}
    else:
        original[location] = text
    if oversized:
        original["metadata"] = "M" * 40_000
    before = copy.deepcopy(original)
    projected = await server._diagnostic_views(
        dict(original), original=original, ctx=ctx, workspace=str(ws), origin="rocq_check",
    )
    result = SimpleNamespace(structured_content=projected, content=[TextContent(
        type="text", text=json.dumps(projected, ensure_ascii=False))])

    async def next_call(_context):
        return result

    final = (await server.TextBlockOutputMiddleware().on_call_tool(None, next_call)).structured_content
    assert final["success"] == original["success"]
    if location.startswith("results"):
        assert final["results"][0]["reason"] == "tactic_failed"
        displayed = final["results"][0]["error"]
    else:
        assert final["reason"] == "tactic_failed" and final["state_id"] == sid
        displayed = final[location]
    if oversized and size > 512:
        ref = final["views"][location]
        assert ref["kind"] == "stored" and ref["handle"] in store._entries
        found = await server.rocq_find_output(ref["handle"], tail, workspace=str(ws), ctx=ctx)
        part = await server.rocq_read_output(ref["handle"], found["hits"][0]["offset_bytes"],
                                              max_bytes=len(tail), workspace=str(ws), ctx=ctx)
        assert part["success"] and part["text"] == tail
        assert len(store._entries) == 1
    else:
        assert displayed == text and not store._entries
    assert original == before  # Planning must not mutate its source mapping.
    assert server._output_view_fits(final)


@pytest.mark.asyncio
async def test_audit8_collective_short_candidate_errors_are_saved_once(proof_boundary, monkeypatch):
    ws, sid, _answers, ctx, store = proof_boundary
    rows = [{"success": False, "reason": "tactic_failed", "error": '"' * 490 + f"SHORT_TAIL_{i}"}
            for i in range(20)]
    original = {"success": True, "from_state_id": sid, "results": rows}
    assert all(len(row["error"].encode()) <= 512 for row in rows)
    assert not server._output_view_fits(original)
    calls, save = [], store.save

    def counted(*args, **kwargs):
        calls.append(1)
        return save(*args, **kwargs)

    monkeypatch.setattr(store, "save", counted)
    result = await server._diagnostic_views(dict(original), original=original, ctx=ctx,
                                             workspace=str(ws), origin="rocq_step_multi")
    if not server._output_view_fits(result):
        result = server._project_oversized_result(result)
    assert calls == [1]
    handle = result["views"]["diagnostics"]["handle"]
    index = await server.rocq_list_output_segments(handle, workspace=str(ws), ctx=ctx)
    assert index["success"] and index["total_entries"] == 20
    raw = store._entries[handle].path.read_bytes()
    for item, row in zip(index["items"], rows):
        assert raw[item["span_start"]:item["span_end"]] == row["error"].encode()
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_audit8_new_reference_cost_closes_secondary_diagnostic_omissions(proof_boundary, monkeypatch):
    ws, sid, _answers, ctx, store = proof_boundary

    def independently_fits(payload):
        structured = len(json.dumps(payload, ensure_ascii=True).encode())
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        wire = len(json.dumps([{"type": "text", "text": raw}], ensure_ascii=True,
                              separators=(",", ":")).encode())
        return structured <= 16384 and wire <= 16384 and structured + wire <= 32768

    base = None
    # Select by independently measured wire cost, not by the planner's output.
    # The provisional ref below is slightly smaller than the actual header/span.
    for width in range(100, 400):
        probe = {"schema_version": 1, "success": True, "from_state_id": sid,
                 "view_status": "partial_unrecoverable", "view_error_code": "view_unavailable",
                 "error": "already shortened", "views": {"error": {
                     "location": "error", "kind": "unavailable", "complete": False,
                     "total_bytes": 900, "shown_bytes": 0, "reason": "view_unavailable"}},
                 "results": [{"success": False, "reason": "tactic_failed",
                              "error": "\n" * width + f"SECOND_TAIL_{i}"} for i in range(20)]}
        draft = copy.deepcopy(probe)
        identity = {"sha256": "a" * 64, "handle": "H" * 195, "owner_generation": "g" * 32}
        draft["views"]["diagnostics"] = {"location": "diagnostics", "kind": "stored",
            "complete": False, "total_bytes": 901, "shown_bytes": 0, **identity}
        draft["views"]["error"] = {"location": "error", "kind": "stored", "complete": False,
            "total_bytes": 900, "shown_bytes": 96, "span_start": 1, "span_end": 901, **identity}
        draft["error"] = "E" * 96 + "\n... [diagnostic incomplete; see views.diagnostics]"
        draft["view_status"] = "partial_recoverable"
        draft.pop("view_error_code")
        if independently_fits(probe) and not independently_fits(draft):
            base = probe
            break
    assert base is not None  # A genuine reference-induced budget crossing.
    original = copy.deepcopy(base)
    original["error"] = "E" * 900
    before = copy.deepcopy(original)
    calls, save = [], store.save

    def counted(*args, **kwargs):
        calls.append(1)
        return save(*args, **kwargs)

    monkeypatch.setattr(store, "save", counted)
    result = await server._diagnostic_views(base, original=original, ctx=ctx,
                                             workspace=str(ws), origin="rocq_step_multi")
    assert calls == [1] and original == before
    handle = result["views"]["diagnostics"]["handle"]
    first = store.list_segments(handle, owner_session="single-stdio-client", workspace=str(ws))
    second = store.list_segments(handle, owner_session="single-stdio-client", workspace=str(ws), cursor=20)
    assert first["total_entries"] == 21 and len(second["items"]) == 1
    raw = store._entries[handle].path.read_bytes()
    span = second["items"][0]
    assert b"SECOND_TAIL_19" in raw[span["span_start"]:span["span_end"]]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_last_resort_batch_list_does_not_claim_nonempty_results_have_zero_bytes(proof_boundary):
    ws, _state_id, _answers, ctx, _store = proof_boundary
    rows = [{"success": True, "tactic": f"t{index}" * 500,
             "goals": "G" * 22_000, "error": "E" * 9_000}
            for index in range(20)]
    original = len(json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    projected = await server._batch_feedback_view(
        {"success": True, "results": rows}, entries=[], kind="rocq_step_multi", ctx=ctx,
        workspace=str(ws), warning_filter="include_warnings",
    )
    assert projected["success"] and projected["view_status"] == "partial_unrecoverable"
    assert projected["views"]["results"]["kind"] == "unavailable"
    assert projected["views"]["results"]["total_bytes"] == original > 0
    assert all(row["success"] for row in projected["results"])


@pytest.mark.asyncio
async def test_last_resort_feedback_display_keeps_existing_trusted_snapshot(proof_boundary):
    ws, _state_id, _answers, ctx, store = proof_boundary
    feedback = [[f"cmd{index}.", "F" * 3_000] for index in range(8)]
    result = {"success": True, "feedback": feedback,
              "metadata": "M" * 50_000}
    original = len(json.dumps(feedback, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    projected = await server._batch_feedback_view(
        result, entries=[(index, command, text) for index, (command, text)
                         in enumerate(feedback)], kind="rocq_check", ctx=ctx,
        workspace=str(ws), warning_filter="include_warnings",
    )
    assert projected["success"] and projected["view_status"] == "partial_unrecoverable"
    assert projected["views"]["feedback"]["kind"] == "stored"
    assert projected["views"]["feedback_display"]["kind"] == "unavailable"
    assert projected["views"]["feedback_display"]["total_bytes"] == original > 0
    assert projected["views"]["feedback"]["handle"] in store._entries


@pytest.mark.asyncio
async def test_medium_goal_keeps_original_text_until_goal_selector_is_ready(proof_boundary, monkeypatch):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "tiny result"
    goal = "H : nat\n|- " + "G" * 3000
    monkeypatch.setattr(interactive, "_format_complete_goals", lambda *_a, **_kw: goal)
    result = await server.rocq_check("first.", from_state=state_id, ctx=ctx)
    assert result["success"] and result["goals"] == goal
    assert result["feedback"] == [["first.", "tiny result"]]
    assert result["view_status"] == "complete"
    assert not store._entries


@pytest.mark.asyncio
async def test_check_large_first_and_late_feedback_share_one_snapshot(proof_boundary):
    ws, state_id, answers, ctx, store = proof_boundary
    first = "一🙂" * 20_001
    later = "SECOND_UNIQUE_TAIL"
    answers["first."] = first
    answers["second."] = later
    result = await server.rocq_check("first. second.", from_state=state_id, ctx=ctx)
    assert result["success"] and result["commands_run"] == 2
    assert result["view_status"] == "partial_recoverable"
    assert result["feedback_total_entries"] == 2
    ref = result["views"]["feedback"]
    assert ref["kind"] == "stored" and len(store._entries) == 1
    assert server._output_view_fits(result)
    handle = ref["handle"]
    assert store._entries[handle].warning_filter == "include_warnings"
    assert store._entries[handle].covers_filtered_all is True
    whole = store._entries[handle].path.read_bytes()
    assert ref["sha256"] == hashlib.sha256(whole).hexdigest()
    for index, original in [(0, first), (1, later)]:
        part = result["views"][f"feedback[{index}].text"]
        assert part["handle"] == handle
        assert part["sha256"] == ref["sha256"]
        assert part["total_bytes"] == part["span_end"] - part["span_start"]
        assert whole[part["span_start"]:part["span_end"]] == original.encode("utf-8")
        assert part["sha256"] != hashlib.sha256(original.encode("utf-8")).hexdigest()
    found = await server.rocq_find_output(handle, "SECOND_UNIQUE_TAIL",
                                          workspace=str(ws), ctx=ctx)
    assert found["success"] and found["hits"]


@pytest.mark.asyncio
async def test_check_after_aggregate_limit_does_not_silently_drop_later_feedback(proof_boundary):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "a" * 60_000
    answers["second."] = "b" * 60_000
    answers["third."] = "c" * 60_000
    answers["fourth."] = "d" * 60_000
    answers["fifth."] = "LAST_RESULT_ONLY_IN_RAW_CAPTURE"
    result = await server.rocq_check("first. second. third. fourth. fifth.",
                                     from_state=state_id, ctx=ctx)
    assert result["success"] and result["commands_run"] == 5
    assert result["feedback_total_entries"] == 5
    handle = result["views"]["feedback"]["handle"]
    assert len(store._entries) == 1
    assert "LAST_RESULT_ONLY_IN_RAW_CAPTURE" in store._entries[handle].path.read_text()
    assert "feedback[4].text" in result["views"]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_overflowed_per_item_roster_stays_searchable_without_30_fsyncs(proof_boundary):
    ws, state_id, answers, ctx, store = proof_boundary
    for index in range(30):
        answers[f"step{index}."] = f"RESULT_{index}_" + "x" * 8_000
    answers["step0."] += "[rocq-feedback-index:25;bytes:999]"  # Forged text marker.
    body = " ".join(f"step{index}." for index in range(30))
    result = await server.rocq_check(body, from_state=state_id, ctx=ctx)
    assert result["success"] and result["commands_run"] == 30
    assert result["feedback_total_entries"] == 30
    assert result["feedback_preview_entries"] <= 20
    assert len(store._entries) == 1
    assert server._output_view_fits(result)
    handle = result["views"]["feedback"]["handle"]
    hidden = await server.rocq_find_output(handle, "rocq-feedback-index:25",
                                           workspace=str(ws), ctx=ctx)
    assert hidden["success"] and hidden["hits"]
    authoritative = await server.rocq_list_output_segments(
        handle, cursor=20, max_entries=10, workspace=str(ws), ctx=ctx,
    )
    assert authoritative["success"] and len(authoritative["items"]) == 10
    position = authoritative["items"][5]
    assert position["index"] == 25
    raw = store._entries[handle].path.read_bytes()
    assert raw[position["span_start"]:position["span_end"]].startswith(b"RESULT_25_")


@pytest.mark.asyncio
async def test_step_multi_feedback_remains_per_candidate_with_single_handle(proof_boundary):
    ws, state_id, answers, ctx, store = proof_boundary
    answers["left."] = "X" * 90_000
    answers["right."] = "RIGHT_CANDIDATE_FEEDBACK"
    result = await server.rocq_step_multi(["left.", "right."], from_state=state_id, ctx=ctx)
    assert result["success"] and len(result["results"]) == 2
    assert result["view_status"] == "partial_recoverable"
    assert result["results"][1]["success"] is True
    assert result["views"]["results[0].feedback"]["handle"] == result["views"]["feedback"]["handle"]
    assert result["views"]["results[1].feedback"]["handle"] == result["views"]["feedback"]["handle"]
    assert len(store._entries) == 1
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_mid_batch_tactic_failure_keeps_first_feedback_and_recovery_state(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "P" * 80_000
    answers["second."] = PetanqueError(0, "bad tactic")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_check("first. second.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["partial"] is True and result["command_index"] == 1
    assert result["last_valid_state_id"] != state_id
    assert result["failed_command"] == "second."
    assert result["views"]["feedback"]["kind"] == "stored"
    assert "_raw_feedback" not in result
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_tactic_failure_and_snapshot_failure_keep_both_error_channels(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "P" * 80_000
    answers["second."] = PetanqueError(0, "bad tactic")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)

    def no_space(*_args, **_kwargs):
        raise OutputStoreError("quota_exceeded", "no room for batch snapshot")

    monkeypatch.setattr(store, "save", no_space)
    result = await server.rocq_check("first. second.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["last_valid_state_id"] != state_id
    assert result["failed_command"] == "second." and result["partial"] is True
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "quota_exceeded"
    assert result["views"]["feedback"]["kind"] == "unavailable"
    assert "handle" not in result["views"]["feedback"]


@pytest.mark.asyncio
async def test_invalid_prior_feedback_cannot_erase_real_later_tactic_failure(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    _ws, state_id, answers, ctx, _store = proof_boundary
    answers["first."] = "bad\ud800feedback"
    answers["second."] = PetanqueError(0, "actual failed command")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_check("first. second.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "invalid_text"
    assert result["failed_command"] == "second."
    assert "actual failed command" in result["error"]
    assert result["command_index"] == 1
    assert result["last_valid_state_id"] != state_id
    assert result["service_generation"] == "single-stdio-client"
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_long_known_coq_error_tail_is_recoverable_without_losing_failure(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    ws, state_id, answers, ctx, store = proof_boundary
    answers["bad."] = PetanqueError(0, "COQ_ERROR_START" + "E" * 70_000 + "COQ_ERROR_TAIL")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_check("bad.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["failed_command"] == "bad."
    assert result["views"]["diagnostics"]["kind"] == "stored"
    error_ref = result["views"]["error"]
    assert error_ref["handle"] == result["views"]["diagnostics"]["handle"]
    assert error_ref["span_end"] - error_ref["span_start"] == error_ref["total_bytes"]
    found = await server.rocq_find_output(
        error_ref["handle"], "COQ_ERROR_TAIL", workspace=str(ws), ctx=ctx,
    )
    assert found["success"] and found["hits"]
    fragment = await server.rocq_read_output(
        error_ref["handle"], offset_bytes=found["hits"][0]["offset_bytes"],
        workspace=str(ws), ctx=ctx,
    )
    assert fragment["success"] and fragment["text"].startswith("COQ_ERROR_TAIL")
    assert server._output_view_fits(result) and len(store._entries) == 1


@pytest.mark.asyncio
async def test_long_candidate_failure_error_uses_trusted_diagnostic_index(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    ws, state_id, answers, ctx, store = proof_boundary
    answers["bad."] = PetanqueError(0, "CANDIDATE_START" + "Q" * 64_000 + "CANDIDATE_END")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_step_multi(["bad."], from_state=state_id, ctx=ctx)
    assert result["success"] is True and result["results"][0]["success"] is False
    assert result["results"][0]["reason"] == "tactic_failed"
    ref = result["views"]["results[0].error"]
    assert ref["kind"] == "stored" and ref["handle"] in store._entries
    index = await server.rocq_list_output_segments(
        ref["handle"], workspace=str(ws), ctx=ctx,
    )
    assert index["success"] and index["items"][0]["span_start"] == ref["span_start"]
    found = await server.rocq_find_output(
        ref["handle"], "CANDIDATE_END", workspace=str(ws), ctx=ctx,
    )
    assert found["success"] and found["hits"]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_long_coq_error_storage_failure_keeps_real_tactic_status(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    _ws, state_id, answers, ctx, store = proof_boundary
    answers["bad."] = PetanqueError(0, "E" * 65_000 + "TAIL")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)

    def refuse(*_args, **_kwargs):
        raise OutputStoreError("quota_exceeded", "full")

    monkeypatch.setattr(store, "save", refuse)
    result = await server.rocq_check("bad.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "tactic_failed"
    assert result["failed_command"] == "bad."
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "quota_exceeded"
    assert result["views"]["error"]["kind"] == "unavailable"
    assert "handle" not in result["views"]["error"]


@pytest.mark.asyncio
async def test_nested_invalid_unicode_error_cannot_escape_diagnostic_projector(proof_boundary):
    ws, _state_id, _answers, ctx, _store = proof_boundary
    invalid = "bad\ud800tail"
    original = {"success": True, "results": [
        {"success": False, "reason": "tactic_failed", "error": invalid},
    ]}
    projected = await server._diagnostic_views(
        {"success": True, "results": [dict(original["results"][0])]},
        original=original, ctx=ctx, workspace=str(ws), origin="rocq_step_multi",
    )
    assert projected["success"] and projected["results"][0]["success"] is False
    assert projected["results"][0]["reason"] == "tactic_failed"
    assert "\ud800" not in projected["results"][0]["error"]
    assert projected["view_status"] == "partial_unrecoverable"
    assert projected["view_error_code"] == "invalid_text"
    assert projected["views"]["results[0].error"]["reason"] == "invalid_text"
    assert server._output_view_fits(projected)


@pytest.mark.asyncio
async def test_many_large_candidate_errors_keep_one_indexed_handle_under_wire_budget(
    proof_boundary, monkeypatch,
):
    from pytanque import PetanqueError

    ws, state_id, answers, ctx, store = proof_boundary
    for index in range(20):
        answers[f"attempt_{index}."] = PetanqueError(
            0, "X" * 16_000 + f"UNIQUE_ERROR_TAIL_{index}",
        )
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_step_multi(
        [f"attempt_{index}." for index in range(20)], from_state=state_id, ctx=ctx,
    )
    assert result["success"] and len(result["results"]) == 20
    assert all(row["success"] is False for row in result["results"])
    assert server._output_view_fits(result)
    handle = result["views"]["diagnostics"]["handle"]
    assert handle in store._entries
    segments = await server.rocq_list_output_segments(
        handle, cursor=19, workspace=str(ws), ctx=ctx,
    )
    assert segments["success"] and segments["items"][0]["index"] == 19
    raw = store._entries[handle].path.read_bytes()
    span = segments["items"][0]
    assert b"UNIQUE_ERROR_TAIL_19" in raw[span["span_start"]:span["span_end"]]


@pytest.mark.asyncio
async def test_outer_timeout_preserves_executed_feedback_but_not_future_commands(proof_boundary):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "T" * 80_000
    answers["second."] = TimeoutError("outer timeout")
    result = await server.rocq_check("first. second.", from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "timeout"
    assert result["commands_run"] == 1
    assert result["last_valid_state_id"] != state_id
    assert result["views"]["feedback"]["kind"] == "stored"
    assert "_raw_feedback" not in result
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_step_multi_tactic_failure_does_not_launder_callee_status(proof_boundary, monkeypatch):
    from pytanque import PetanqueError

    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "S" * 80_000
    answers["second."] = PetanqueError(0, "no applicable tactic")
    monkeypatch.setattr(server, "_pet_alive", lambda _client: True)
    result = await server.rocq_step_multi(["first.", "second."], from_state=state_id, ctx=ctx)
    assert result["success"] is True  # Exploration call completed.
    assert result["results"][0]["success"] is True
    assert result["results"][1]["success"] is False
    assert result["results"][1]["reason"] == "tactic_failed"
    assert "feedback" not in result["results"][1]
    assert result["views"]["feedback"]["kind"] == "stored"
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_step_multi_timeout_keeps_bounded_partial_results(proof_boundary):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "S" * 80_000
    answers["second."] = TimeoutError("outer timeout")
    result = await server.rocq_step_multi(["first.", "second."], from_state=state_id, ctx=ctx)
    assert result["success"] is False and result["reason"] == "timeout"
    assert len(result["partial_results"]) == 1
    assert result["partial_results"][0]["success"] is True
    assert result["views"]["partial_results[0].feedback"]["kind"] == "stored"
    assert result["views"]["feedback"]["kind"] == "stored"
    assert "_raw_feedback" not in result
    assert server._output_view_fits(result)
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_invalid_feedback_unicode_does_not_erase_execution_state(proof_boundary):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "broken\ud800feedback"
    result = await server.rocq_check("first.", from_state=state_id, ctx=ctx)
    assert result["success"] is True and result["state_id"] != state_id
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "invalid_text"
    assert "\ud800" not in str(result)
    assert not store._entries


@pytest.mark.asyncio
async def test_batch_storage_failure_never_changes_a_succeeded_tactic(proof_boundary, monkeypatch):
    _ws, state_id, answers, ctx, store = proof_boundary
    answers["first."] = "Y" * 80_000

    def fail(_state, _workspace):
        raise OutputStoreError("quota_exceeded", "full")

    monkeypatch.setattr(server, "_get_output_store", fail)
    result = await server.rocq_check("first.", from_state=state_id, ctx=ctx)
    assert result["success"] is True and result["state_id"] != state_id
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "quota_exceeded"
    assert result["views"]["feedback"]["kind"] == "unavailable"
    assert not store._entries
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_audit8_ttl_cleanup_fault_keeps_completed_check_state(proof_boundary, monkeypatch):
    ws, sid, answers, ctx, store = proof_boundary
    tick = [0.0]
    store._clock, store._ttl_seconds = lambda: tick[0], 1
    old = store.save("old", owner_session="single-stdio-client", workspace=str(ws), origin="test")
    path = store._entries[old["handle"]].path
    tick[0] = 2.0
    unlink = Path.unlink

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("TTL cleanup fault")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", denied)
        answers["first."] = "F" * 20_000
        result = await server.rocq_check("first.", from_state=sid, ctx=ctx)
        assert result["success"] and result["state_id"] != sid and result["commands_run"] == 1
        assert result["view_error_code"] == "store_failed"
        assert "reason" not in result and "handle" not in result["views"]["feedback"]
        assert store._used_bytes == 3 and len(store._entries) == 1


@settings(max_examples=65, deadline=None)
@given(parts=st.lists(st.text(alphabet="abc🙂\n", max_size=45), min_size=1, max_size=30))
def test_property_group_spans_reconstruct_each_raw_feedback(tmp_path_factory, parts):
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.getbasetemp()) as dirname:
        root = Path(dirname)
        ws = root / "ws"
        ws.mkdir()
        entries = [(index, f"step{index}.", text) for index, text in enumerate(parts)]
        transcript, spans = server._feedback_transcript(entries)
        with OutputStore(root / "private", workspace=ws, allow_test_temp_root=True) as store:
            saved = store.save(transcript, owner_session="owner", workspace=str(ws),
                               origin="rocq_check", warning_filter="include_warnings",
                               covers_filtered_all=True,
                               segments=[(index, start, end)
                                         for (index, _cmd, _text), (start, end) in zip(entries, spans)])
            handle = saved["handle"]
            raw = store._entries[handle].path.read_bytes()
            cursor, actual = 0, []
            while True:
                page = store.list_segments(handle, owner_session="owner", workspace=str(ws),
                                           cursor=cursor, max_entries=7)
                actual.extend(page["items"])
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]
            assert [item["index"] for item in actual] == list(range(len(parts)))
            for item, original in zip(actual, parts):
                assert raw[item["span_start"]:item["span_end"]] == original.encode("utf-8")
