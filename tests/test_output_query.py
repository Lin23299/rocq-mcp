"""A query omits text only after saving all filtered feedback."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies as st

import rocq_mcp.interactive as interactive
import rocq_mcp.server as server
from rocq_mcp.output_store import OutputStore, OutputStoreError


@pytest.fixture
def query_context(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    pet = SimpleNamespace(feedback=[])
    pet.run = lambda _state, _cmd: SimpleNamespace(feedback=pet.feedback)

    async def run_with_pet(fn, _state, _name, **_kwargs):
        return fn(pet)

    monkeypatch.setattr(server, "_run_with_pet", run_with_pet)
    monkeypatch.setattr(interactive, "_get_or_create_import_state", lambda *args: object())
    with OutputStore(tmp_path / "private", workspace=ws, allow_test_temp_root=True) as store:
        ctx = SimpleNamespace(lifespan_context={
            "pet_timeout": 30.0, "recent_errors": deque(maxlen=20),
            "output_stdio": True, "output_principal": "one-stdio-client",
            "output_store": store,
        })
        yield ws, pet, ctx, store


@pytest.mark.asyncio
async def test_small_query_keeps_full_output_without_snapshot(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = [(3, "nat : Set")]
    result = await server.rocq_query("Check nat.", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["output"] == "nat : Set"
    assert result["view_status"] == "complete"
    assert result["warning_filter"] == "include_warnings"
    assert result["feedback_snapshot_complete"] is False
    assert result["feedback_total_messages"] == result["feedback_shown_messages"] == 1
    assert result["views"]["output"]["kind"] == "inline"
    assert not store._entries  # Under budget does not write a tempfile.


@pytest.mark.asyncio
async def test_empty_feedback_is_complete_without_a_snapshot(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = []
    result = await server.rocq_query("Check nat.", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["output"] == "(no output)"
    assert result["view_status"] == "complete"
    assert result["views"]["output"]["total_bytes"] == len(result["output"].encode("utf-8"))
    assert result["views"]["output"]["shown_bytes"] == len(result["output"].encode("utf-8"))
    assert result["feedback_total_messages"] == 0
    assert not store._entries


@pytest.mark.asyncio
async def test_invalid_unicode_is_an_explicit_view_failure_not_a_successful_empty_query(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = [(3, "bad\ud800text")]
    result = await server.rocq_query("Check nat.", workspace=str(ws), ctx=ctx)
    assert result["success"] is True
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "invalid_text"
    assert "\ud800" not in str(result)
    assert not store._entries


@pytest.mark.asyncio
async def test_one_large_search_message_gets_short_dual_channel_receipt(query_context):
    ws, pet, ctx, store = query_context
    text = "A" * 139_000 + "SEARCH_TAIL_SYMBOL"
    pet.feedback = [(3, text)]
    result = await server.rocq_query("Search _." , workspace=str(ws), ctx=ctx)
    assert result["success"] and result["view_status"] == "partial_recoverable"
    assert result["feedback_total_messages"] == result["feedback_shown_messages"] == 1
    assert "SEARCH_TAIL_SYMBOL" not in result["output"]
    assert result["views"]["output"]["total_bytes"] == len(text)
    assert server._output_view_fits(result)
    assert "_all_feedback" not in result
    handle = result["views"]["output"]["handle"]
    hit = await server.rocq_find_output(handle, "SEARCH_TAIL_SYMBOL", workspace=str(ws), ctx=ctx)
    assert hit["success"] and hit["hits"][0]["offset_bytes"] == 139_000
    chunk = await server.rocq_read_output(handle, 139_000, workspace=str(ws), ctx=ctx)
    assert chunk["success"] and chunk["text"] == "SEARCH_TAIL_SYMBOL"
    blocks = server._split_text_blocks(json.dumps(result, ensure_ascii=False))
    assert blocks and "SEARCH_TAIL_SYMBOL" not in "\n".join(b.text for b in blocks)
    assert len(store._entries) == 1


@pytest.mark.asyncio
async def test_max_results_omission_saves_both_short_feedback_messages(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = [(3, "FIRST_RESULT"), (3, "SECOND_ONLY_IN_SNAPSHOT")]
    result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx,
                                     max_results=1)
    assert result["success"] and result["view_status"] == "partial_recoverable"
    assert result["feedback_shown_messages"] == 1
    assert result["feedback_total_messages"] == 2
    assert result["feedback_snapshot_complete"] is True
    assert result["warning_filter"] == "include_warnings"
    assert "SECOND_ONLY_IN_SNAPSHOT" not in result["output"]
    handle = result["views"]["output"]["handle"]
    found = await server.rocq_find_output(handle, "SECOND_ONLY_IN_SNAPSHOT",
                                          workspace=str(ws), ctx=ctx)
    assert found["success"] and len(found["hits"]) == 1
    assert store._entries[handle].total_bytes == len("FIRST_RESULT\nSECOND_ONLY_IN_SNAPSHOT")
    assert store._entries[handle].warning_filter == "include_warnings"
    assert store._entries[handle].covers_filtered_all is True


@pytest.mark.asyncio
async def test_audit8_ttl_cleanup_fault_keeps_successful_query_anchor(query_context, monkeypatch):
    ws, pet, ctx, store = query_context
    tick = [0.0]
    store._clock, store._ttl_seconds = lambda: tick[0], 1
    old = store.save("old", owner_session="one-stdio-client", workspace=str(ws), origin="test")
    path = store._entries[old["handle"]].path
    tick[0] = 2.0
    sid = interactive._state_add(SimpleNamespace(proof_finished=False), "Query.v", "goal", str(ws), None, None, 0)
    monkeypatch.setattr(interactive, "_check_staleness", lambda *_: None)
    monkeypatch.setattr(server, "_set_workspace_if_needed", lambda *_: None)
    original = Path.unlink

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("TTL cleanup fault")
        return original(target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", denied)
        pet.feedback = [(3, "FIRST"), (3, "SECOND")]
        result = await server.rocq_query("Search _.", from_state=sid, workspace=str(ws), max_results=1, ctx=ctx)
        assert result["success"] and result["from_state_id"] == sid
        assert result["view_error_code"] == "store_failed" and result["view_status"] == "partial_unrecoverable"
        assert "reason" not in result and "handle" not in result["views"]["output"]
        assert len(store._entries) == 1 and store._used_bytes == path.stat().st_size == 3
        # A short query does not need the broken cache and retains its full output.
        pet.feedback = [(3, "short")]
        short = await server.rocq_query("Check nat.", from_state=sid, workspace=str(ws), ctx=ctx)
        assert short["success"] and short["output"] == "short" and short["view_status"] == "complete"


@pytest.mark.asyncio
async def test_filtered_query_snapshot_does_not_restore_omitted_warnings(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = [(2, "WARNING_ONLY"), (3, "X" * 80_000 + "VISIBLE_RESULT")]
    result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx,
                                     include_warnings=False)
    handle = result["views"]["output"]["handle"]
    warning = await server.rocq_find_output(handle, "WARNING_ONLY", workspace=str(ws), ctx=ctx)
    retained = await server.rocq_find_output(handle, "VISIBLE_RESULT", workspace=str(ws), ctx=ctx)
    assert not warning["hits"] and retained["hits"]
    assert result["feedback_total_messages"] == 1
    assert result["warning_filter"] == "exclude_warnings"
    assert result["feedback_snapshot_complete"] is True
    assert store._entries[handle].warning_filter == "exclude_warnings"
    assert store._entries[handle].covers_filtered_all is True
    assert retained["warning_filter"] == "exclude_warnings"
    assert retained["covers_filtered_all"] is True


@pytest.mark.asyncio
async def test_two_warning_policies_pin_distinct_identity_even_if_printed_bytes_match(query_context):
    ws, pet, ctx, store = query_context
    pet.feedback = [(3, "FIRST"), (3, "SAVED_SECOND")]
    visible = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx,
                                      max_results=1, include_warnings=True)
    filtered = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx,
                                       max_results=1, include_warnings=False)
    first = visible["views"]["output"]["handle"]
    second = filtered["views"]["output"]["handle"]
    assert first != second and visible["feedback_snapshot_complete"]
    assert filtered["feedback_snapshot_complete"]
    assert store._entries[first].sha256 == store._entries[second].sha256
    assert store._entries[first].warning_filter == "include_warnings"
    assert store._entries[second].warning_filter == "exclude_warnings"


@pytest.mark.asyncio
async def test_storage_failure_preserves_execution_truth_and_no_fake_handle(query_context, monkeypatch):
    ws, pet, ctx, _store = query_context
    pet.feedback = [(3, "X" * 80_000)]

    def fail(_state, _workspace):
        raise OutputStoreError("quota_exceeded", "cache full")

    monkeypatch.setattr(server, "_get_output_store", fail)
    result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx)
    assert result["success"] is True
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "quota_exceeded"
    assert result["views"]["output"]["kind"] == "unavailable"
    assert "handle" not in result["views"]["output"]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_query_last_resort_metadata_fallback_recounts_visible_source_bytes(
    query_context, monkeypatch,
):
    ws, _pet, ctx, store = query_context
    full = "Z" * 40_000 + "REAL_QUERY_TAIL"

    async def query(**_kwargs):
        return {"success": True, "output": full, "metadata": "M" * 30_000,
                "_all_feedback": full, "_shown_feedback": full,
                "_feedback_messages": 1, "_shown_messages": 1}

    monkeypatch.setattr(server, "run_query", query)
    result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["view_status"] == "partial_unrecoverable"
    assert result["views"]["output"]["kind"] == "stored"
    assert result["views"]["output"]["shown_bytes"] == 0
    assert result["output"].startswith("[some query metadata")
    assert result["views"]["metadata"]["total_bytes"] == 30_000
    assert result["views"]["metadata"]["kind"] == "unavailable"
    handle = result["views"]["output"]["handle"]
    assert handle in store._entries
    tail = await server.rocq_find_output(handle, "REAL_QUERY_TAIL",
                                         workspace=str(ws), ctx=ctx)
    assert tail["success"] and tail["hits"]
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_storage_failure_does_not_erase_a_live_from_state_anchor(query_context, monkeypatch):
    ws, _pet, ctx, _store = query_context

    async def fake_query(**_kwargs):
        raw = "x" * 60_000
        return {"success": True, "from_state_id": 783, "output": raw,
                "_all_feedback": raw, "_shown_feedback": raw,
                "_feedback_messages": 1, "_shown_messages": 1}

    monkeypatch.setattr(server, "run_query", fake_query)

    def fail(_state, _workspace):
        raise OutputStoreError("store_failed", "injected failure")

    monkeypatch.setattr(server, "_get_output_store", fail)
    result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx)
    assert result["success"] is True and result["from_state_id"] == 783
    assert result["view_status"] == "partial_unrecoverable"
    assert result["view_error_code"] == "store_failed"
    assert "reason" not in result  # A printing failure is not a tactic failure.


@pytest.mark.asyncio
async def test_oversized_warning_cannot_escape_short_receipt(query_context, monkeypatch):
    ws, _pet, ctx, store = query_context
    original = "Y" * 30_000

    async def fake_query(**_kwargs):
        text = "Q" * 50_000
        return {"success": True, "output": text, "stale_warning": original,
                "_all_feedback": text, "_shown_feedback": text,
                "_feedback_messages": 1, "_shown_messages": 1}

    monkeypatch.setattr(server, "run_query", fake_query)
    response = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx)
    assert response["success"] and response["view_status"] == "partial_unrecoverable"
    assert response["view_error_code"] == "view_unavailable"
    assert response["views"]["stale_warning"]["kind"] == "unavailable"
    assert response["views"]["output"]["kind"] == "stored"
    assert "Y" * 300 not in str(response)
    assert server._output_view_fits(response)
    assert len(store._entries) == 1


@settings(max_examples=65, deadline=None)
@given(text=st.text(alphabet="abc🙂é\x00\n", max_size=6_000))
@pytest.mark.asyncio
async def test_property_default_query_never_stores_unchanged_short_results(tmp_path_factory, text):
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.getbasetemp()) as directory:
        root = Path(directory)
        ws = root / "ws"
        ws.mkdir()
        (ws / "_CoqProject").write_text('-Q . ""\n')
        pet = SimpleNamespace(run=lambda *_a: SimpleNamespace(feedback=[(3, text)]))

        async def run_with_pet(fn, _state, _name, **_kw):
            return fn(pet)

        with (patch.object(server, "_run_with_pet", run_with_pet),
              patch.object(interactive, "_get_or_create_import_state", lambda *args: object()),
              OutputStore(root / "private", workspace=ws, allow_test_temp_root=True) as store):
            ctx = SimpleNamespace(lifespan_context={
                "pet_timeout": 30.0, "recent_errors": deque(maxlen=20),
                "output_stdio": True, "output_principal": "client", "output_store": store,
            })
            result = await server.rocq_query("Search _.", workspace=str(ws), ctx=ctx)
            assert result["success"]
            assert len(json.dumps(result, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            if result["view_status"] == "complete":
                assert result["output"] == (text or "(no output)")
                assert not store._entries
            else:
                assert result["view_status"] == "partial_recoverable"
                handle = result["views"]["output"]["handle"]
                assert len(store._entries) == 1
                assert store._entries[handle].sha256 == hashlib.sha256(text.encode()).hexdigest()
