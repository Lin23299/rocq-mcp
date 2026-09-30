"""Read/find MCP tools retain the session boundary and bounded wire views."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from unittest.mock import Mock
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from rocq_mcp import server
from rocq_mcp import interactive
from rocq_mcp.output_store import OutputStore


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["start", "check", "query", "step_multi"])
@pytest.mark.parametrize("size", [100, 4000])
async def test_mainline_short_receipts_do_no_heavy_work(active, monkeypatch, kind, size):
    from mcp.types import TextContent

    _store, ctx, ws = active
    ctx.lifespan_context["output_store"] = None
    text = "H : nat\n|- " + "x" * size
    original = {"success": True, "state_id": 7, "proof_finished": False, "goals": text}
    if kind == "check":
        original.update(_raw_feedback=[], _feedback_workspace=str(ws), commands_run=1)
    elif kind == "query":
        original = {"success": True, "output": text, "from_state_id": 7,
                    "_all_feedback": text, "_shown_feedback": text,
                    "_feedback_messages": 1, "_shown_messages": 1}
    elif kind == "step_multi":
        original = {"success": True, "from_state_id": 7,
                    "results": [{"success": True, "tactic": "idtac.", "goals": text,
                                 "proof_finished": False}], "_raw_feedback": [],
                    "_raw_candidate_goals": [(0, "idtac.", text)], "_feedback_workspace": str(ws)}
    execution = []

    async def run(**_kwargs):
        execution.append(1)
        return dict(original)

    monkeypatch.setattr(server, "run_" + kind, run)
    forbidden = Mock(side_effect=AssertionError("short path entered heavy output work"))
    for name in ("_get_output_store", "_feedback_transcript", "_project_oversized_result", "_run_with_pet"):
        monkeypatch.setattr(server, name, forbidden)
    splits = Mock(wraps=server._split_text_blocks)
    monkeypatch.setattr(server, "_split_text_blocks", splits)
    if kind == "start":
        response = await server.rocq_start(workspace=str(ws), ctx=ctx)
    elif kind == "query":
        response = await server.rocq_query("Show.", from_state=7, workspace=str(ws), ctx=ctx)
    elif kind == "check":
        response = await server.rocq_check("idtac.", from_state=7, ctx=ctx)
    else:
        response = await server.rocq_step_multi(["idtac."], from_state=7, ctx=ctx)
    result = SimpleNamespace(structured_content=response, content=[TextContent(
        type="text", text=json.dumps(response, ensure_ascii=False, separators=(",", ":")))])

    async def next_call(_):
        return result

    result = await server.TextBlockOutputMiddleware().on_call_tool(None, next_call)
    assert execution == [1]
    assert ctx.lifespan_context["output_store"] is None
    forbidden.assert_not_called()
    data = result.structured_content
    actual = data["results"][0]["goals"] if kind == "step_multi" else data["output" if kind == "query" else "goals"]
    assert actual == text and data["view_status"] == "complete"
    # Producer(s) plus one final wire representation; no diagnostic replan.
    assert splits.call_count <= (3 if kind == "check" else 2)


@pytest.fixture
def active(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    with OutputStore(tmp_path / "private", workspace=ws, allow_test_temp_root=True) as store:
        ctx = SimpleNamespace(
            session_id="session-A",
            lifespan_context={"output_store": store, "recent_errors": deque(maxlen=20),
                              "output_stdio": True, "output_principal": "session-A"},
        )
        yield store, ctx, ws


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, False])
async def test_audit8_start_goal_encoding_preserves_state_and_existing_source(active, monkeypatch, bad):
    store, ctx, ws = active
    state = SimpleNamespace(proof_finished=False)
    sid = interactive._state_add(state, "Goal.v", "goal", str(ws), None, None, 0)
    saved = store.save("other feedback", owner_session="session-A", workspace=str(ws), origin="test")
    view = {"location": "feedback", "kind": "stored", "complete": False,
            "total_bytes": saved["total_bytes"], "shown_bytes": 0,
            "handle": saved["handle"], "sha256": saved["source_sha256"],
            "owner_generation": saved["owner_generation"]}
    text = "H : nat\n|- BAD\ud800" if bad else "H : nat\n|- True"

    async def start(**_kwargs):
        return {"success": True, "state_id": sid, "proof_finished": False,
                "goals": text, "views": {"feedback": view}}

    monkeypatch.setattr(server, "run_start", start)
    result = await server.rocq_start(preamble="", workspace=str(ws), ctx=ctx)
    assert result["success"] and result["state_id"] == sid and not result["proof_finished"]
    assert result["service_generation"] == "session-A"
    assert interactive._state_table[sid].state is state
    assert result["views"]["feedback"] == view
    if bad:
        assert result["view_error_code"] == "invalid_text"
        assert result["views"]["goals"] == {
            "location": "goals", "kind": "unavailable", "complete": False,
            "total_bytes": None, "shown_bytes": 0, "reason": "invalid_text",
        }
        assert "\ud800" not in result["goals"]
        assert "reason" not in result
    else:
        assert result["goals"] == text and result.get("view_error_code") is None
    json.dumps(result, ensure_ascii=False).encode()
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_audit8_start_failure_is_not_washed_into_display_success(active, monkeypatch):
    _store, ctx, ws = active

    async def start(**_kwargs):
        return {"success": False, "reason": "validation", "error": "actual prefix failure"}

    monkeypatch.setattr(server, "run_start", start)
    result = await server.rocq_start(workspace=str(ws), ctx=ctx)
    assert not result["success"] and result["reason"] == "validation"
    assert "state_id" not in result and result.get("view_error_code") != "invalid_text"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_goal", [False, True])
async def test_audit8_start_bad_metadata_is_separate_from_goal_text(active, monkeypatch, bad_goal):
    _store, ctx, ws = active
    state = SimpleNamespace(proof_finished=False)
    sid = interactive._state_add(state, "Goal.v", "goal", str(ws), None, None, 0)

    async def start(**_kwargs):
        return {"success": True, "state_id": sid, "proof_finished": False,
                "goals": "BAD\ud800" if bad_goal else "|- True",
                "metadata": "OTHER\ud800"}

    monkeypatch.setattr(server, "run_start", start)
    result = await server.rocq_start(workspace=str(ws), ctx=ctx)
    assert result["success"] and result["state_id"] == sid
    assert interactive._state_table[sid].state is state
    assert result["view_error_code"] == "invalid_text"
    assert result["views"]["metadata"]["reason"] == "invalid_text"
    if bad_goal:
        assert result["views"]["goals"]["reason"] == "invalid_text"
    else:
        assert result["views"]["goals"]["kind"] == "live_state"
        assert result["views"]["goals"].get("reason") != "invalid_text"
    json.dumps(result, ensure_ascii=False).encode()
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_audit8_start_invalid_goal_returns_utf8_receipt_through_real_stdio(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"), TEST_OUTPUT_WORKSPACE=str(ws),
               TEST_OUTPUT_INVALID_GOAL="conclusion")
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        result = await client.call_tool("rocq_start", {"workspace": str(ws)})
        assert not result.is_error
        data = result.data
        assert data["success"] and data["state_id"] and not data["proof_finished"]
        assert data["service_generation"] and data["view_error_code"] == "invalid_text"
        assert data["views"]["goals"]["kind"] == "unavailable"
        json.dumps(result.structured_content, ensure_ascii=False).encode()
        for block in result.content:
            block.text.encode()
        # The same registered state is still usable, not removed by display failure.
        roster = await client.call_tool("rocq_get_goal_roster", {
            "from_state": data["state_id"], "service_generation": data["service_generation"],
        })
        assert roster.data["success"] and roster.data["state_id"] == data["state_id"]
        assert roster.data["view_error_code"] == "invalid_text"
        rejected = await client.call_tool("rocq_start", {"workspace": str(ws), "theorem": "reject"})
        assert not rejected.data["success"] and rejected.data["reason"] == "validation"
        assert "state_id" not in rejected.data


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rocq_read_output", "rocq_find_output", "rocq_list_output_segments"])
async def test_audit8_public_retrieval_cleanup_fault_has_no_body(active, monkeypatch, operation):
    store, ctx, ws = active
    tick = [0.0]
    store._clock, store._ttl_seconds = lambda: tick[0], 1
    saved = store.save("secret", owner_session="session-A", workspace=str(ws), origin="test",
                       segments=[(0, 0, 6)])
    path = store._entries[saved["handle"]].path
    tick[0] = 2.0
    unlink = Path.unlink

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("TTL cleanup fault")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", denied)
        options = {"offset_bytes": 0} if operation == "rocq_read_output" else (
            {"literal": "secret"} if operation == "rocq_find_output" else {})
        result = await getattr(server, operation)(saved["handle"], workspace=str(ws), ctx=ctx, **options)
        assert not result["success"] and result["view_error_code"] == "store_failed"
        assert not {"text", "hits", "items"}.intersection(result)
        assert store._used_bytes == 6 and saved["handle"] in store._entries


@pytest.mark.asyncio
async def test_audit8_lifespan_cleanup_failure_still_tears_down_pet(active, monkeypatch):
    _store, _ctx, _ws = active
    events = []

    class BrokenStore:
        def close(self):
            raise server.OutputStoreError("store_failed", "cleanup fault")

    client = object()
    monkeypatch.setattr(server, "_kill_pet", lambda given: events.append(("pet", given)))
    monkeypatch.setattr(server, "_cleanup_coqc_artifacts", lambda _path: events.append(("artifacts", None)))
    with pytest.raises(server.OutputStoreError) as error:
        async with server.app_lifespan(server.mcp) as state:
            state["output_store"] = BrokenStore()
            state["pet_client"] = client
    assert error.value.code == "store_failed"
    assert events == [("pet", client), ("artifacts", None)]


@pytest.mark.asyncio
async def test_audit8_lifespan_close_fault_preserves_primary_exception(active, monkeypatch):
    events = []

    class BrokenStore:
        def close(self):
            raise server.OutputStoreError("store_failed", "cleanup fault")

    monkeypatch.setattr(server, "_kill_pet", lambda _: events.append("pet"))
    monkeypatch.setattr(server, "_cleanup_coqc_artifacts", lambda _: events.append("artifacts"))
    with pytest.raises(ValueError, match="primary") as error:
        async with server.app_lifespan(server.mcp) as state:
            state["output_store"], state["pet_client"] = BrokenStore(), object()
            raise ValueError("primary")
    assert events == ["pet", "artifacts"]
    assert any("Output cleanup" in note for note in error.value.__notes__)


@pytest.mark.asyncio
async def test_range_receipt_and_other_session_denial(active):
    store, ctx, ws = active
    text = "x" * 205_000 + "\nUNIQUE_TAIL_SENTINEL"
    saved = store.save(text, owner_session=ctx.session_id, workspace=str(ws),
                       origin="rocq_query", warning_filter="include_warnings",
                       covers_filtered_all=True)
    response = await server.rocq_read_output(saved["handle"], offset_bytes=204_990,
                                             workspace=str(ws), ctx=ctx)
    assert response["success"] and response["view_status"] == "partial_recoverable"
    assert "UNIQUE_TAIL_SENTINEL" in response["text"]
    assert response["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert response["views"]["text"]["complete"] is True
    assert response["views"]["source"]["handle"] == saved["handle"]
    assert server._output_view_fits(response)
    other = SimpleNamespace(
        session_id="session-B",
        lifespan_context={"output_store": store, "recent_errors": deque(maxlen=20),
                          "output_stdio": True, "output_principal": "session-B"},
    )
    denied = await server.rocq_read_output(saved["handle"], offset_bytes=0,
                                           workspace=str(ws), ctx=other)
    assert denied["success"] is False and denied["view_error_code"] == "unauthorized"
    assert "text" not in denied and "reason" in denied


@pytest.mark.asyncio
async def test_read_shrinks_escaped_text_to_real_wire_budget(active):
    store, ctx, ws = active
    saved = store.save("\x00" * 20_000, owner_session=ctx.session_id,
                       workspace=str(ws), origin="rocq_query",
                       warning_filter="include_warnings", covers_filtered_all=True)
    response = await server.rocq_read_output(saved["handle"], offset_bytes=0,
                                             max_bytes=8192, workspace=str(ws), ctx=ctx)
    assert response["success"] and 0 < response["end"] < 8192
    assert response["text"] == "\x00" * response["end"]
    assert server._output_view_fits(response)
    assert len(json.dumps(response, ensure_ascii=True).encode()) < server._OUTPUT_VIEW_CHANNEL_BYTES


@pytest.mark.asyncio
async def test_find_limits_hits_and_keeps_resume_cursor(active):
    store, ctx, ws = active
    text = "x" * 205_000 + "needle\n" + "needle " * 35
    saved = store.save(text, owner_session=ctx.session_id, workspace=str(ws),
                       origin="rocq_query", warning_filter="include_warnings",
                       covers_filtered_all=True)
    result = await server.rocq_find_output(saved["handle"], "needle", max_hits=20,
                                           workspace=str(ws), ctx=ctx)
    assert result["success"] and result["has_more"]
    assert result["view_status"] == "partial_recoverable"
    assert len(result["hits"]) == 20
    assert result["hits"][0]["offset_bytes"] == 205_000
    assert server._output_view_fits(result)
    next_page = await server.rocq_find_output(saved["handle"], "needle",
                                              cursor=result["next_cursor"], workspace=str(ws), ctx=ctx)
    assert next_page["success"]
    assert next_page["hits"][0]["offset_bytes"] > result["hits"][-1]["offset_bytes"]


@pytest.mark.asyncio
async def test_slow_find_is_bounded_by_outer_budget_and_leaks_no_result(active, monkeypatch):
    store, ctx, ws = active
    saved = store.save("needle", owner_session="session-A", workspace=str(ws), origin="query")
    release = threading.Event()
    entered = threading.Event()
    original = store.find

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(timeout=6)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "find", blocked)
    monkeypatch.setattr(server, "FIND_TIMEOUT_SECONDS", 0.15)
    began = time.monotonic()
    try:
        result = await server.rocq_find_output(
            saved["handle"], "needle", workspace=str(ws), ctx=ctx,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - began
    assert entered.is_set() and elapsed < 5.0
    assert result["success"] is False and result["view_error_code"] == "timeout"
    assert not result.get("hits")


@pytest.mark.asyncio
async def test_only_a_full_initial_slice_claims_complete_saved_source(active):
    store, ctx, ws = active
    saved = store.save("abc", owner_session="session-A", workspace=str(ws), origin="query")
    whole = await server.rocq_read_output(saved["handle"], offset_bytes=0,
                                          workspace=str(ws), ctx=ctx)
    assert whole["view_status"] == "complete" and not whole["has_more"]
    late = await server.rocq_read_output(saved["handle"], offset_bytes=1,
                                         workspace=str(ws), ctx=ctx)
    assert late["view_status"] == "partial_recoverable"
    assert late["views"]["source"]["handle"] == saved["handle"]


@pytest.mark.asyncio
async def test_many_json_escaped_hits_still_yield_bounded_search_page(active):
    store, ctx, ws = active
    pattern = "\x00" * 255
    saved = store.save((pattern + "\n") * 32, owner_session="session-A",
                       workspace=str(ws), origin="rocq_query",
                       warning_filter="include_warnings", covers_filtered_all=True)
    result = await server.rocq_find_output(saved["handle"], pattern,
                                           max_hits=20, workspace=str(ws), ctx=ctx)
    assert result["success"]
    assert 1 <= len(result["hits"]) <= 20
    assert result["has_more"] and result["next_cursor"] > 0
    assert server._output_view_fits(result)


@pytest.mark.asyncio
async def test_invalid_handles_preserve_error_channels(active):
    _store, ctx, ws = active
    for tool in (
        server.rocq_find_output("fake.handle", "word", workspace=str(ws), ctx=ctx),
        server.rocq_read_output("fake.handle", 0, workspace=str(ws), ctx=ctx),
    ):
        response = await tool
        assert response["success"] is False
        assert response["view_status"] == "partial_unrecoverable"
        assert response["view_error_code"] in {"expired", "not_found"}
        assert response["reason"] == "not_found"
    no_ctx = await server.rocq_read_output("fake", 0, ctx=None)
    assert no_ctx["success"] is False and no_ctx["reason"] == "validation"


@pytest.mark.asyncio
async def test_unknown_multi_client_transport_fails_closed(active):
    store, ctx, ws = active
    saved = store.save("secret", owner_session="session-A", workspace=str(ws), origin="query")
    ctx.lifespan_context["output_stdio"] = False
    denied = await server.rocq_find_output(saved["handle"], "secret", workspace=str(ws), ctx=ctx)
    assert denied["success"] is False and denied["view_error_code"] == "unauthorized"
    assert not denied.get("hits")
    with pytest.raises(server.OutputStoreError) as err:
        server._get_output_store(ctx.lifespan_context)
    assert err.value.code == "unauthorized"


def test_workspace_store_configuration_fails_before_writing_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = ws / "do-not-create"
    monkeypatch.setattr(server, "_default_root", lambda: target)
    state = {"output_stdio": True, "output_principal": "owner", "output_store": None}
    with pytest.raises(server.OutputStoreError) as err:
        server._get_output_store(state, str(ws))
    assert err.value.code == "validation" and not target.exists()


def test_reusing_a_store_checks_the_next_workspace_before_any_new_artifact(active):
    store, ctx, _ws = active
    nested = store._root / "session-existing" / "another-workspace"
    nested.mkdir(parents=True)
    with pytest.raises(server.OutputStoreError) as err:
        server._get_output_store(ctx.lifespan_context, str(nested))
    assert err.value.code == "validation"
    assert ctx.lifespan_context["output_store"] is store
    assert not store._entries


@pytest.mark.asyncio
async def test_real_mcp_client_receives_text_block_and_same_session_handle(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"),
               TEST_OUTPUT_WORKSPACE=str(ws))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        identity1 = (await client.call_tool("whoami", {})).data
        identity2 = (await client.call_tool("whoami", {})).data
        # FastMCP 4.0.3 changes session_id/Connection per stdio request;
        # only the explicitly single-client stdio process owns the result.
        assert identity1["lifespan"] == identity2["lifespan"]
        assert identity1["read_stream"] == identity2["read_stream"]
        created = (await client.call_tool("seed", {"text": "begin\n" + "x" * 72_000 + "END!"})).data
        handle = created["handle"]
        looked_up = await client.call_tool("rocq_find_output", {
            "handle": handle, "literal": "END!", "workspace": str(ws),
        })
        assert looked_up.data["success"], looked_up.data
        offset = looked_up.data["hits"][0]["offset_bytes"]
        sliced = await client.call_tool("rocq_read_output", {
            "handle": handle, "offset_bytes": offset, "workspace": str(ws),
        })
        assert sliced.data["text"] == "END!"
        assert sliced.data["schema_version"] == 1
        assert len(sliced.content) == 2
        assert json.loads(sliced.content[0].text)["text_chars"] == 4
        assert sliced.content[1].text == "END!"
        assert sliced.structured_content["text"] == "END!"

    # Reconnecting launches a new stdio server, so a printed handle cannot
    # silently resolve to a different server generation.
    restarted = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "restart.stderr.log")
    async with Client(restarted, timeout=30, init_timeout=20) as client:
        cache_before = list((tmp_path / "cache").iterdir())
        for tool, options in (
            ("rocq_read_output", {"offset_bytes": offset}),
            ("rocq_find_output", {"literal": "END!"}),
            ("rocq_list_output_segments", {}),
        ):
            old = await client.call_tool(tool, {
                "handle": handle, "workspace": str(ws), **options,
            })
            assert old.data["success"] is False
            assert old.data["view_error_code"] == "expired"
            assert not {"text", "hits", "items"}.intersection(old.data)
            malformed = await client.call_tool(tool, {
                "handle": "random-not-a-signed-handle", "workspace": str(ws), **options,
            })
            assert malformed.data["view_error_code"] == "not_found"
        # Retrieval classification must not create a store or cache directory.
        assert list((tmp_path / "cache").iterdir()) == cache_before
        await client.call_tool("seed", {"text": "unrelated"})
        again = await client.call_tool("rocq_read_output", {
            "handle": handle, "offset_bytes": offset, "workspace": str(ws),
        })
        assert again.data["view_error_code"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rocq_read_output", "rocq_find_output", "rocq_list_output_segments"])
async def test_no_store_old_handle_expired_without_initializing_storage(active, tmp_path, monkeypatch, operation):
    store, ctx, ws = active
    handle = store.save("secret", owner_session="session-A", workspace=str(ws), origin="test")["handle"]
    store.close()
    ctx.lifespan_context["output_store"] = None
    unused_root = tmp_path / "must-not-be-created"
    monkeypatch.setattr(server, "_default_root", lambda: unused_root)
    options = {"offset_bytes": 0} if operation == "rocq_read_output" else (
        {"literal": "secret"} if operation == "rocq_find_output" else {})
    result = await getattr(server, operation)(handle, workspace=str(ws), ctx=ctx, **options)
    assert not result["success"] and result["view_error_code"] == "expired"
    assert not {"text", "hits", "items"}.intersection(result)
    assert ctx.lifespan_context["output_store"] is None and not unused_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rocq_read_output", "rocq_find_output", "rocq_list_output_segments"])
@pytest.mark.parametrize("bad", [None, 1, "random", "a" * 300, "é" * 195])
async def test_no_store_bad_format_not_found_and_non_stdio_unauthorized(active, operation, bad):
    _store, ctx, ws = active
    ctx.lifespan_context["output_store"] = None
    options = {"offset_bytes": 0} if operation == "rocq_read_output" else (
        {"literal": "secret"} if operation == "rocq_find_output" else {})
    result = await getattr(server, operation)(bad, workspace=str(ws), ctx=ctx, **options)
    assert result["view_error_code"] == "not_found"
    assert not {"text", "hits", "items"}.intersection(result)
    ctx.lifespan_context["output_stdio"] = False
    result = await getattr(server, operation)(bad, workspace=str(ws), ctx=ctx, **options)
    assert result["view_error_code"] == "unauthorized"


@pytest.mark.asyncio
async def test_real_stdio_query_auto_offloads_both_channels(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"), TEST_OUTPUT_WORKSPACE=str(ws))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        result = await client.call_tool("rocq_query", {"command": "Search _.",
                                                      "workspace": str(ws)})
        data = result.data
        assert data["success"] and data["view_status"] == "partial_recoverable"
        assert "QUERY_TAIL_SYMBOL" not in data["output"]
        assert "QUERY_TAIL_SYMBOL" not in result.structured_content["output"]
        assert "QUERY_TAIL_SYMBOL" not in "\n".join(block.text for block in result.content)
        assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
        handle = data["views"]["output"]["handle"]
        found = await client.call_tool("rocq_find_output", {"handle": handle,
            "literal": "QUERY_TAIL_SYMBOL", "workspace": str(ws)})
        assert found.data["success"]
        offset = found.data["hits"][0]["offset_bytes"]
        assert offset == 139_000
        chunk = await client.call_tool("rocq_read_output", {"handle": handle,
            "offset_bytes": offset, "workspace": str(ws)})
        assert chunk.data["text"] == "QUERY_TAIL_SYMBOL"


@pytest.mark.asyncio
async def test_real_stdio_check_and_step_feedback_are_bounded_and_retrievable(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"), TEST_OUTPUT_WORKSPACE=str(ws))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        check = await client.call_tool("rocq_check", {"body": "first. second.", "from_state": 100})
        assert check.data["success"] and check.data["state_id"] == 101
        assert check.data["views"]["feedback"]["kind"] == "stored"
        assert len(json.dumps(check.structured_content, ensure_ascii=True).encode()) < server._OUTPUT_VIEW_CHANNEL_BYTES
        assert "CHECK_FIRST_TAIL_ONLY_IN_SNAPSHOT" not in "\n".join(b.text for b in check.content)
        part = check.data["views"]["feedback[1].text"]
        page = await client.call_tool("rocq_list_output_segments", {
            "handle": part["handle"], "workspace": str(ws),
        })
        assert page.data["success"] and len(page.data["items"]) == 2
        assert page.data["items"][1]["span_start"] == part["span_start"]
        second = await client.call_tool("rocq_read_output", {
            "handle": part["handle"], "offset_bytes": part["span_start"],
            "max_bytes": part["span_end"] - part["span_start"], "workspace": str(ws),
        })
        assert second.data["text"] == "CHECK_TAIL_ONLY_IN_FEEDBACK"

        step = await client.call_tool("rocq_step_multi", {"from_state": 100, "tactics": ["first."]})
        assert step.data["success"] and step.data["results"][0]["success"]
        assert step.data["views"]["feedback"]["kind"] == "stored"
        assert len(json.dumps(step.structured_content, ensure_ascii=True).encode()) < server._OUTPUT_VIEW_CHANNEL_BYTES
        found = await client.call_tool("rocq_find_output", {
            "handle": step.data["views"]["feedback"]["handle"],
            "literal": "STEP_TAIL_ONLY_IN_FEEDBACK", "workspace": str(ws),
        })
        assert found.data["success"] and found.data["hits"]


@pytest.mark.asyncio
async def test_real_stdio_toc_and_notations_return_bounded_recoverable_views(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"), TEST_OUTPUT_WORKSPACE=str(ws))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        for tool, args, marker in (
            ("rocq_toc", {"file": "Sample.v", "workspace": str(ws)}, "TOC_TAIL_FOR_STDIO"),
            ("rocq_notations", {"statement": "True", "workspace": str(ws)},
             "NOTATION_TAIL_FOR_STDIO"),
        ):
            result = await client.call_tool(tool, args)
            assert result.data["success"] and result.data["view_status"] == "partial_recoverable"
            assert marker not in "\n".join(block.text for block in result.content)
            assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            handle = result.data["views"]["output"]["handle"]
            found = await client.call_tool("rocq_find_output", {
                "handle": handle, "literal": marker, "workspace": str(ws),
            })
            assert found.data["success"] and found.data["hits"]


@pytest.mark.asyncio
async def test_real_stdio_goal_selector_and_transient_candidate_snapshot(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"),
               ROCQ_STATE_ID_SEED="12345", TEST_OUTPUT_WORKSPACE=str(ws))

    def stdio(path):
        return StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                              cwd=str(ws), env=env, log_file=path)

    async with Client(stdio(tmp_path / "server.stderr.log"), timeout=30, init_timeout=20) as client:
        seeded = (await client.call_tool("seed_goal", {})).data
        sid = seeded["state_id"]
        first = await client.call_tool("rocq_check", {"from_state": sid, "body": "goal-mode."})
        assert first.data["success"] and first.data["state_id"] == sid
        generation = first.data["service_generation"]
        assert first.data["views"]["goals"]["kind"] == "live_state"
        assert first.data["view_status"] == "partial_recoverable"
        assert "GOAL_END_FROM_LIVE_STATE" not in "".join(b.text for b in first.content)

        roster = await client.call_tool("rocq_get_goal_roster", {
            "from_state": sid, "service_generation": generation,
        })
        assert roster.data["success"] and roster.data["focused_goals"] == 1
        ref = roster.data["goals"][0]["goal_ref"]
        assert ref["service_generation"] == generation
        assert ref["goal_digest"]
        position = len("GOAL_START " + "G" * 72_000)
        part = await client.call_tool("rocq_get_goal_part", {
            "from_state": sid, "service_generation": generation,
            "group": "focused", "goal_index": 1, "part": "conclusion",
            "offset_bytes": position,
        })
        assert part.data["success"] and part.data["text"] == " GOAL_END_FROM_LIVE_STATE"
        assert part.data["goal_ref"] == ref

        step = await client.call_tool("rocq_step_multi", {
            "from_state": sid, "tactics": ["goal-mode."],
        })
        assert step.data["success"] and step.data["results"][0]["success"]
        goal_view = step.data["views"]["candidate_goals"]
        assert goal_view["kind"] == "stored" and "state_id" not in goal_view
        found = await client.call_tool("rocq_find_output", {
            "handle": goal_view["handle"], "literal": "GOAL_END_FROM_LIVE_STATE",
            "workspace": str(ws),
        })
        assert found.data["success"] and found.data["hits"]
        read = await client.call_tool("rocq_read_output", {
            "handle": goal_view["handle"], "offset_bytes": found.data["hits"][0]["offset_bytes"],
            "workspace": str(ws),
        })
        assert read.data["text"].startswith("GOAL_END_FROM_LIVE_STATE")
        for result in (first, roster, part, step, found, read):
            assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            assert sum(len(block.text.encode()) for block in result.content) <= server._OUTPUT_VIEW_CHANNEL_BYTES

    # Deterministic numeric reuse on a new process still cannot read the old reference.
    async with Client(stdio(tmp_path / "restart.stderr.log"), timeout=30, init_timeout=20) as client:
        new_sid = (await client.call_tool("seed_goal", {})).data["state_id"]
        assert new_sid == sid
        expired = await client.call_tool("rocq_get_goal_part", {
            "from_state": sid, "service_generation": generation,
            "group": "focused", "goal_index": 1, "part": "conclusion",
        })
        assert expired.data["success"] is False
        assert expired.data["view_error_code"] == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["conclusion", "hyp_name", "hidden_definition"])
async def test_real_stdio_invalid_goal_views_preserve_execution_fields_and_utf8_channels(tmp_path, corrupt):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    cache = tmp_path / "cache-must-not-be-created"
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(cache), TEST_OUTPUT_WORKSPACE=str(ws),
               TEST_OUTPUT_INVALID_GOAL=corrupt)
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        seeded = (await client.call_tool("seed_goal", {})).data
        for tool, options, location in (
            ("rocq_get_goal_part", {"group": "focused", "goal_index": 1, "part": "conclusion"}, "text"),
            ("rocq_get_goal_roster", {}, "roster"),
        ):
            result = await client.call_tool(tool, {
                "from_state": seeded["state_id"], "service_generation": seeded["service_generation"],
                **options,
            })
            assert not result.is_error
            for data in (result.data, result.structured_content):
                assert data["success"] and data["state_id"] == seeded["state_id"]
                assert data["proof_finished"] is False
                assert data["service_generation"] == seeded["service_generation"]
                assert data["view_error_code"] == "invalid_text"
                assert data["view_status"] == "partial_unrecoverable"
                assert data["views"][location]["kind"] == "unavailable"
                assert not {"text", "goals", "goal_ref", "digest", "reason"}.intersection(data)
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            assert sum(len(block.text.encode("utf-8")) for block in result.content) <= server._OUTPUT_VIEW_CHANNEL_BYTES
        assert not cache.exists()


@pytest.mark.asyncio
async def test_real_stdio_unmodelled_large_error_is_explicitly_incomplete_in_both_channels(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               TEST_OUTPUT_WORKSPACE=str(ws), ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    async with Client(transport, timeout=30, init_timeout=20) as client:
        result = await client.call_tool("giant_unmodelled_diagnostic", {"state_id": 871})
        assert result.data["success"] is False
        assert result.data["reason"] == "tactic_failed"
        assert result.data["state_id"] == 871 and result.data["proof_finished"] is False
        assert result.data["view_status"] == "partial_unrecoverable"
        assert "DIAGNOSTIC_TAIL" not in json.dumps(result.structured_content)
        assert "DIAGNOSTIC_TAIL" not in "".join(block.text for block in result.content)
        assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
        assert sum(len(block.text.encode()) for block in result.content) <= server._OUTPUT_VIEW_CHANNEL_BYTES


@pytest.mark.asyncio
async def test_real_pet_through_real_stdio_keeps_goal_selector_and_execution_truth():
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    from tests.conftest import PET_AVAILABLE

    if not PET_AVAILABLE:
        pytest.skip("real Pet is not installed")
    repo = Path(__file__).resolve().parents[1]
    # Unlike disposable unit fixtures, the production store forbids /tmp.
    # A self-cleaning test root under the user's home exercises that rule.
    with tempfile.TemporaryDirectory(prefix="rocq-mcp-stdio-pet-", dir=Path.home()) as directory:
        root = Path(directory)
        ws = root / "proof"
        ws.mkdir()
        (ws / "_CoqProject").write_text('-Q . ""\n')
        (ws / "Goal.v").write_text(
            "Theorem goal_sample : forall n : nat, n = n.\n"
            "Proof.\nintros n.\nreflexivity.\nQed.\n",
        )
        env = dict(os.environ, PYTHONPATH=str(repo / "src"),
                   ROCQ_WORKSPACE=str(ws), ROCQ_MCP_OUTPUT_ROOT=str(root / "private"))
        transport = StdioTransport(
            sys.executable, ["-c", "from rocq_mcp.server import main; main()"],
            cwd=str(ws), env=env, log_file=root / "server.stderr.log",
        )
        async with Client(transport, timeout=60, init_timeout=20) as client:
            start = await client.call_tool("rocq_start", {
                "file": "Goal.v", "theorem": "goal_sample", "workspace": str(ws),
            })
            assert start.data["success"], start.data
            gen = start.data["service_generation"]
            stepped = await client.call_tool("rocq_check", {
                "body": "intros n.", "from_state": start.data["state_id"],
            })
            assert stepped.data["success"], stepped.data
            sid = stepped.data["state_id"]
            roster = await client.call_tool("rocq_get_goal_roster", {
                "from_state": sid, "service_generation": gen,
            })
            assert roster.data["success"] and roster.data["focused_goals"] == 1
            assert roster.data["goals"][0]["hypotheses_total"] >= 1
            conclusion = await client.call_tool("rocq_get_goal_part", {
                "from_state": sid, "service_generation": gen,
                "group": "focused", "goal_index": 1, "part": "conclusion",
            })
            assert conclusion.data["success"] and "n" in conclusion.data["text"]
            assert conclusion.data["goal_ref"] == roster.data["goals"][0]["goal_ref"]
            for result in (start, stepped, roster, conclusion):
                assert len(json.dumps(result.structured_content, ensure_ascii=True).encode()) <= server._OUTPUT_VIEW_CHANNEL_BYTES
                assert sum(len(block.text.encode()) for block in result.content) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            finished = await client.call_tool("rocq_check", {
                "body": "reflexivity. Qed.", "from_state": sid,
            })
            assert finished.data["success"] and finished.data["proof_finished"]


@pytest.mark.asyncio
async def test_final_guard_catches_large_second_content_block_even_with_small_structured_result():
    from mcp.types import TextContent

    result = SimpleNamespace(
        structured_content={"success": True, "state_id": 7},
        content=[TextContent(type="text", text="A" * 9_000),
                 TextContent(type="text", text="B" * 9_000)],
    )

    async def next_call(_context):
        return result

    projected = await server.TextBlockOutputMiddleware().on_call_tool(None, next_call)
    assert projected.structured_content["success"] is True
    assert projected.structured_content["state_id"] == 7
    assert projected.structured_content["view_status"] == "partial_unrecoverable"
    assert projected.structured_content["views"]["content"]["kind"] == "unavailable"
    assert len(projected.content) == 1
    assert "B" * 9_000 not in projected.content[0].text


def test_final_guard_does_not_call_a_legacy_clipped_goal_complete():
    payload = {"success": True, "state_id": 7,
               "goals": "|- T\n…[clipped 3 of 12 chars; pass goals_max_chars=-1]…"}
    checked = server._normalize_output_receipt(payload)
    assert checked["success"] and checked["state_id"] == 7
    assert checked["view_status"] == "partial_unrecoverable"
    assert checked["views"]["goals"]["reason"] == "source_clipped"
    assert checked["views"]["goals"]["total_bytes"] is None
    assert payload.get("view_status") is None


def test_short_receipt_cannot_claim_complete_with_unavailable_or_saved_fields():
    unavailable = {"success": True, "view_status": "complete", "views": {
        "output": {"location": "output", "kind": "unavailable", "complete": False,
                   "total_bytes": 90, "shown_bytes": 0, "reason": "store_failed"},
    }}
    rejected = server._normalize_output_receipt(unavailable)
    assert rejected["success"] and rejected["view_status"] == "partial_unrecoverable"
    assert rejected["view_error_code"] == "view_unavailable"
    assert unavailable["view_status"] == "complete"
    saved = {"success": True, "view_status": "complete", "views": {
        "output": {"location": "output", "kind": "stored", "complete": False,
                   "total_bytes": 90, "shown_bytes": 0, "sha256": "a" * 64,
                   "handle": "opaque", "owner_generation": "owner"},
    }}
    assert server._normalize_output_receipt(saved)["view_status"] == "partial_recoverable"
    malformed = {"success": True, "view_status": "complete", "views": {
        "output": {"location": "output", "kind": "inline", "complete": False,
                   "total_bytes": 3, "shown_bytes": 1},
    }}
    corrected = server._normalize_output_receipt(malformed)
    assert corrected["view_status"] == "partial_unrecoverable"
    assert corrected["views"]["output"]["kind"] == "unavailable"
    missing_identity = {"success": True, "view_status": "partial_recoverable", "views": {
        "output": {"location": "output", "kind": "stored", "complete": False,
                   "total_bytes": 3, "shown_bytes": 0, "sha256": "a" * 64,
                   "owner_generation": "owner"},
    }}
    refused = server._normalize_output_receipt(missing_identity)
    assert refused["view_status"] == "partial_unrecoverable"
    assert refused["views"]["output"]["kind"] == "unavailable"


@settings(max_examples=70, deadline=None)
@given(
    success=st.booleans(),
    fields=st.dictionaries(
        st.sampled_from(["diagnostics", "hints", "metadata", "output"]),
        st.lists(st.dictionaries(
            st.sampled_from(["error", "text", "nested"]),
            st.text(alphabet="abc🙂é\n", max_size=12_000), max_size=3,
        ), max_size=5), max_size=4,
    ),
)
def test_unknown_nested_diagnostics_never_escape_either_wire_budget(success, fields):
    payload = {"success": success, "state_id": 17, **fields}
    if not success:
        payload["reason"] = "tactic_failed"
    projected = server._project_oversized_result(payload)
    assert projected["success"] is success and projected["state_id"] == 17
    if not success:
        assert projected["reason"] == "tactic_failed"
    assert server._output_view_fits(projected)
    assert projected["view_status"] == "partial_unrecoverable"


def test_many_unicode_candidate_errors_keep_statuses_under_final_budget():
    payload = {"success": True, "state_id": 999,
               "results": [{"success": False, "reason": "🙂" * 500,
                            "proof_finished": False} for _ in range(20)],
               "metadata": "🤯" * 30_000}
    projected = server._project_oversized_result(payload)
    assert projected["success"] and projected["state_id"] == 999
    assert len(projected["results"]) == 20
    assert all(row["success"] is False for row in projected["results"])
    assert projected["view_status"] == "partial_unrecoverable"
    assert server._output_view_fits(projected)


def test_structured_wire_budget_counts_json_separators_conservatively():
    borderline = None
    for count in range(600, 1500, 10):
        payload = {"success": True, "metadata": {f"key_{index}": "x"
                                                 for index in range(count)}}
        compact = len(json.dumps(payload, ensure_ascii=True,
                                 separators=(",", ":")).encode())
        normal = len(json.dumps(payload, ensure_ascii=True).encode())
        if compact <= server._OUTPUT_VIEW_CHANNEL_BYTES < normal:
            borderline = payload
            break
    assert borderline is not None  # A distinguishing fixture, not a vacuous limit check.
    assert not server._output_view_fits(borderline)


@pytest.mark.parametrize("text", ['"' * 4096, "\n" * 6000], ids=["quotes", "newlines"])
def test_content_budget_counts_json_escaping_of_complete_block_value(text):
    from mcp.types import TextContent

    payload = {"success": True, "metadata": text}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    block = TextContent(type="text", text=raw)
    structured = len(json.dumps(payload, ensure_ascii=True).encode())
    body = len(raw.encode())
    complete = len(json.dumps([block.model_dump(mode="json", by_alias=True, exclude_none=True)],
                             ensure_ascii=True, separators=(",", ":")).encode())
    assert body <= server._OUTPUT_VIEW_CHANNEL_BYTES and structured <= server._OUTPUT_VIEW_CHANNEL_BYTES
    assert complete > server._OUTPUT_VIEW_CHANNEL_BYTES  # Distinguishes old body-only accounting.
    assert not server._output_view_fits(payload)


@pytest.mark.asyncio
async def test_final_content_budget_includes_blocks_and_optional_metadata_at_exact_body_limit():
    from mcp.types import TextContent

    response = {"success": True, "state_id": 7, "schema_version": 1,
                "view_status": "complete", "views": {}}
    original = TextContent(type="text", text="A" * server._OUTPUT_VIEW_CHANNEL_BYTES,
                           _meta={"source": "independent"})
    result = SimpleNamespace(structured_content=response, content=[original])

    async def next_call(_context):
        return result

    projected = await server.TextBlockOutputMiddleware().on_call_tool(None, next_call)
    wire = json.dumps([b.model_dump(mode="json", by_alias=True, exclude_none=True)
                       for b in projected.content], ensure_ascii=True, separators=(",", ":")).encode()
    assert len(wire) <= server._OUTPUT_VIEW_CHANNEL_BYTES
    assert projected.structured_content["success"] and projected.structured_content["state_id"] == 7
    assert projected.structured_content["views"]["content"]["kind"] == "unavailable"


@pytest.mark.asyncio
async def test_real_stdio_control_character_snapshot_read_has_full_channel_budget_and_exact_source(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "_CoqProject").write_text('-Q . ""\n')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo) + os.pathsep + str(repo / "src"),
               ROCQ_MCP_OUTPUT_ROOT=str(tmp_path / "cache"), TEST_OUTPUT_WORKSPACE=str(ws))
    transport = StdioTransport(sys.executable, ["-m", "tests._output_stdio_server"],
                               cwd=str(ws), env=env, log_file=tmp_path / "server.stderr.log")
    original = "\x00\n\"🙂" * 3000 + "CONTROL_TAIL"
    async with Client(transport, timeout=30, init_timeout=20) as client:
        handle = (await client.call_tool("seed", {"text": original})).data["handle"]
        offset, chunks = 0, []
        while True:
            result = await client.call_tool("rocq_read_output", {
                "handle": handle, "offset_bytes": offset, "max_bytes": 8192, "workspace": str(ws),
            })
            data = result.data
            assert data["success"] and data["start"] == offset and data["end"] > offset
            content = json.dumps([b.model_dump(mode="json", by_alias=True, exclude_none=True)
                                  for b in result.content], ensure_ascii=True, separators=(",", ":")).encode()
            structured = json.dumps(result.structured_content, ensure_ascii=True).encode()
            assert len(content) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            assert len(structured) <= server._OUTPUT_VIEW_CHANNEL_BYTES
            assert len(content) + len(structured) <= server._OUTPUT_VIEW_TOTAL_BYTES
            assert data["source_sha256"] == hashlib.sha256(original.encode()).hexdigest()
            chunks.append(data["text"].encode())
            offset = data["end"]
            if not data["has_more"]:
                break
        assert b"".join(chunks) == original.encode()


def test_final_projection_keeps_stored_feedback_handle_and_uses_canonical_list_bytes():
    items = [{"i": number} for number in range(21)]
    root_view = {"location": "feedback", "kind": "stored", "complete": False,
                 "total_bytes": 33, "shown_bytes": 0, "handle": "trusted-handle",
                 "sha256": "a" * 64, "owner_generation": "owner"}
    result = server._project_oversized_result({
        "success": True, "state_id": 7, "views": {"feedback": root_view},
        "feedback": items, "metadata": "M" * 100_000,
    })
    assert result["success"] and result["state_id"] == 7
    assert result["views"]["feedback"] == root_view
    omitted = result["views"]["feedback_display"]
    assert omitted["kind"] == "unavailable"
    assert omitted["total_bytes"] == len(json.dumps(items, ensure_ascii=True,
                                                    separators=(",", ":")).encode("utf-8"))
    assert omitted["shown_bytes"] == 0
    assert server._output_view_fits(result)


@pytest.mark.parametrize("count", [32, 33, 90])
def test_unknown_view_index_overflow_is_counted_not_silently_lost(count):
    source = {"success": True, "state_id": 7}
    source.update({f"diagnostic_{index:02d}": "X" * 750 + f"TAIL_{index}"
                   for index in range(count)})
    result = server._project_oversized_result(source)
    assert result["success"] and result["state_id"] == 7
    assert result["view_status"] == "partial_unrecoverable"
    assert server._output_view_fits(result)
    metadata = result["views"].get("__omitted_fields__")
    visible = {key for key in result["views"] if key.startswith("diagnostic_")}
    if count > 32:
        assert metadata is not None and metadata["kind"] == "unavailable"
        assert metadata["omitted_count"] >= count - 32
        assert metadata["unlisted_count"] == metadata["omitted_count"] - len(metadata["paths_preview"])
        if count == 33:
            assert "diagnostic_32" in visible or "diagnostic_32" in metadata["paths_preview"]
    assert len(visible) + (metadata["omitted_count"] if metadata else 0) >= count


@pytest.mark.parametrize("field,kind", [("output", "stored"), ("goals", "live_state")])
@pytest.mark.parametrize("unit", ["A", "🙂é"])
def test_final_recompression_updates_existing_source_bytes_without_mutating_input(field, kind, unit):
    prefix = unit * 1024
    view = {"location": field, "kind": kind, "complete": False,
            "total_bytes": 100_000 if kind == "stored" else None,
            "shown_bytes": len(prefix.encode()), "owner_generation": "owner"}
    if kind == "stored":
        view.update(handle="trusted-handle", sha256="a" * 64)
    before = dict(view)
    payload = {"success": True, "state_id": 7, "proof_finished": False,
               field: prefix + "\n... [source incomplete]", "views": {field: view},
               "metadata": "M" * 100_000}
    assert not server._output_view_fits(payload)
    result = server._project_oversized_result(payload)
    retained = result[field].split("\n", 1)[0]
    assert retained == prefix[:len(retained)]
    assert result["views"][field] == {**before, "shown_bytes": len(retained.encode())}
    assert result["views"][field + "_display"]["shown_bytes"] == len(retained.encode())
    assert "full source unavailable" not in result[field]
    assert view == before
    assert result["success"] and result["state_id"] == 7 and not result["proof_finished"]
    assert server._output_view_fits(result)


def test_final_recompression_does_not_count_old_notice_as_source_bytes():
    view = {"location": "output", "kind": "stored", "complete": False,
            "total_bytes": 1000, "shown_bytes": 30, "handle": "trusted-handle",
            "sha256": "a" * 64, "owner_generation": "owner"}
    result = server._project_oversized_result({
        "success": True, "output": "A" * 30 + "\n[notice]" * 100,
        "views": {"output": view}, "metadata": "M" * 100_000,
    })
    assert result["views"]["output"]["shown_bytes"] == 30
    assert view["shown_bytes"] == 30


def test_final_deletion_clears_source_display_bytes_but_keeps_handle(monkeypatch):
    view = {"location": "output", "kind": "stored", "complete": False,
            "total_bytes": 100_000, "shown_bytes": 2048, "handle": "trusted-handle",
            "sha256": "a" * 64, "owner_generation": "owner"}
    # Force the later deletion branch independently of the earlier prefix limit.
    monkeypatch.setattr(server, "_output_view_fits", lambda value: "output" not in value)
    result = server._project_oversized_result({
        "success": True, "state_id": 7, "output": "A" * 2048,
        "views": {"output": view},
    })
    assert "output" not in result
    assert result["views"]["output"] == {**view, "shown_bytes": 0}
    assert view["shown_bytes"] == 2048


def test_final_recompression_does_not_leave_inline_view_complete():
    view = {"location": "output", "kind": "inline", "complete": True,
            "total_bytes": 2048, "shown_bytes": 2048}
    result = server._project_oversized_result({
        "success": True, "output": "A" * 2048, "views": {"output": view},
    })
    assert result["views"]["output"]["kind"] == "unavailable"
    assert result["views"]["output"]["complete"] is False
    assert result["views"]["output"]["shown_bytes"] == 384
    assert view["complete"] is True


def test_final_list_truncation_clears_only_removed_child_display():
    child = lambda index: {"location": f"results[{index}].goals", "kind": "live_state",
                           "complete": False, "total_bytes": None, "shown_bytes": 3,
                           "owner_generation": "owner"}
    result = server._project_oversized_result({
        "success": True, "results": [{"success": True, "goals": "|-T"} for _ in range(21)],
        "views": {f"results[{index}].goals": child(index) for index in (0, 20)},
    })
    assert len(result["results"]) == 20
    assert result["views"]["results[0].goals"]["shown_bytes"] == 3
    assert result["views"]["results[20].goals"]["shown_bytes"] == 0
