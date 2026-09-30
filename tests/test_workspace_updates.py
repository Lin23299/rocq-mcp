"""Native refresh wiring: signal scopes, Python states, and failure boundaries."""
from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from rocq_mcp import server, interactive
from rocq_mcp import workspace_updates as updates


@pytest.fixture
def scope(tmp_path, monkeypatch):
    ws = tmp_path / "proof"
    ws.mkdir()
    monkeypatch.setenv("ROCQ_WORKSPACE_UPDATE_ROOT", str(tmp_path / "signals"))
    monkeypatch.setenv("ROCQ_WORKSPACE_UPDATES", "1")
    return ws


def test_noop_and_failed_partial_update(scope):
    initial = updates.read_signal(str(scope))
    assert not updates.signal_path(str(scope)).parent.exists()
    assert updates.run_update(str(scope), [sys.executable, "-c", "pass"]) == 0
    assert updates.read_signal(str(scope)) == initial
    cmd = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'new'); sys.exit(3)", str(scope / "Dep.vo")]
    assert updates.run_update(str(scope), cmd) == 3
    changed = updates.read_signal(str(scope))
    assert changed["token"] != initial["token"] and not changed["updating"]


def test_pending_and_bad_signal_fail_closed(scope):
    updates.publish(str(scope), "old", updating=True)
    with pytest.raises(updates.WorkspaceUpdateError, match="pending"):
        updates.run_update(str(scope), [sys.executable, "-c", "pass"])
    assert updates.run_update(str(scope), [sys.executable, "-c", "pass"], recover=True) == 0
    assert updates.read_signal(str(scope))["token"] != "old"
    updates.signal_path(str(scope)).write_text('{"schema":1}')
    with pytest.raises(updates.WorkspaceUpdateError, match="Malformed"):
        updates.read_signal(str(scope))


@pytest.fixture
def backend(scope, monkeypatch):
    calls = []
    class Pet:
        id = 0
        def set_workspace(self, **kwargs):
            calls.append(("set", kwargs["dir"]))
        def _send_lsp_message(self, text):
            calls.append(("update", json.loads(text)))
        def _read_lsp_response(self):
            return json.dumps({"jsonrpc": "2.0", "id": self.id, "result": None})
    pet = Pet()
    state = {"pet_timeout": 10, "current_workspace": None, "recent_errors": deque(maxlen=20)}
    monkeypatch.setattr(server, "_ensure_pet", lambda _: pet)
    monkeypatch.setattr(server, "_parse_project_flags", lambda _: None)
    return pet, state, calls


@pytest.mark.asyncio
async def test_native_refresh_clears_python_and_imports_keeps_process(scope, backend):
    pet, state, calls = backend
    result = await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
    assert result["success"] and not any(k == "update" for k, _ in calls)
    sid = interactive._state_add(SimpleNamespace(proof_finished=False), "x.v", "t", str(scope), None, None, 0)
    interactive._import_cache["old"] = object()
    updates.publish(str(scope), "new", updating=False)
    invoked = []
    rejected = await server._run_with_pet(lambda _: invoked.append(1), state, "test", workspace=str(scope), from_state=sid)
    assert rejected["reason"] == "state_invalidated" and not invoked
    assert not interactive._state_table and not interactive._import_cache
    assert sum(k == "update" for k, _ in calls) == 1
    next_result = await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
    assert next_result["success"] and sum(k == "update" for k, _ in calls) == 1
    assert interactive._invalidated_response(sid, state, "test")["invalidation_cause"] == "workspace_updated"
    old_again = await server._run_with_pet(lambda _: pytest.fail("old id reused"), state, "test",
                                           workspace=str(scope.parent), from_state=sid)
    assert old_again["reason"] == "state_invalidated"
    assert sum(k == "update" for k, _ in calls) == 1


@pytest.mark.asyncio
async def test_pending_does_not_run_pet_and_inflight_change_discards_ids(scope, backend):
    _pet, state, calls = backend
    updates.publish(str(scope), "one", updating=True)
    result = await server._run_with_pet(lambda _: pytest.fail("ran while updating"), state, "test", workspace=str(scope))
    assert result["reason"] == "workspace_updating" and not calls
    updates.publish(str(scope), "one", updating=False)
    def action(_):
        updates.publish(str(scope), "two", updating=False)
        return {"success": True, "state_id": 999, "proof_finished": True}
    result = await server._run_with_pet(action, state, "test", workspace=str(scope))
    assert result["reason"] == "state_invalidated" and result["execution_completed"]
    assert result["execution_success"] and "state_id" not in result
    assert result["proof_finished"] is True


@pytest.mark.asyncio
async def test_inflight_failure_keeps_coq_diagnostic_and_progress(scope, backend):
    _pet, state, _calls = backend
    def action(_):
        updates.publish(str(scope), "changed", updating=False)
        return {"success": False, "reason": "tactic_failed", "error": "actual Coq failure",
                "proof_finished": False, "failed_command": "bad.", "command_index": 1,
                "partial": True, "last_valid_state_id": 999}
    result = await server._run_with_pet(action, state, "test", workspace=str(scope))
    assert result["reason"] == "state_invalidated" and result["execution_success"] is False
    assert result["execution_reason"] == "tactic_failed" and result["execution_error"] == "actual Coq failure"
    assert result["failed_command"] == "bad." and result["command_index"] == 1 and result["partial"]
    assert result["proof_finished"] is False and "last_valid_state_id" not in result


@pytest.mark.asyncio
async def test_unsupported_update_never_acknowledges_or_advances_token(scope, backend, monkeypatch):
    pet, state, _calls = backend
    await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
    old = state["workspace_signal"]
    updates.publish(str(scope), "changed", updating=False)
    monkeypatch.setattr(pet, "_read_lsp_response", lambda: json.dumps({"id": pet.id, "error": {"code": -32601}}))
    response = await server._run_with_pet(lambda _: pytest.fail("unsupported refreshed"), state, "test", workspace=str(scope))
    assert response["reason"] == "unavailable" and state["workspace_signal"] == old


def test_tracked_missing_backing_invalidates_only_its_session(scope):
    path = scope / "Gone.v"
    path.write_text("Goal True.")
    sid = interactive._state_add(SimpleNamespace(proof_finished=False), str(path), "t", str(scope), None, None, 0, consumed=10)
    entry = interactive._state_table[sid]
    entry.session_root = sid
    interactive._session_snapshots[sid] = interactive._FileSnapshot(str(path), path.read_text())
    other = interactive._state_add(SimpleNamespace(proof_finished=False), "other", "t", str(scope), None, None, 0)
    path.unlink()
    interactive._refresh_session_for(entry)
    assert sid not in interactive._state_table and other in interactive._state_table
    assert interactive._invalidated_response(sid, None, "test")["invalidation_cause"] == "file_unavailable"


@pytest.mark.asyncio
async def test_hot_requests_do_not_scan_artifacts_or_send_extra_pet_rpc(scope, backend, monkeypatch):
    _pet, state, calls = backend
    monkeypatch.setattr(updates, "artifact_snapshot", lambda _: pytest.fail("scan on hot request"))
    for _ in range(30):
        response = await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
        assert response["success"]
    assert [key for key, _ in calls] == ["set"]


def test_two_workspaces_and_restored_project_config_do_not_alias(scope):
    other = scope.parent / "other"
    other.mkdir()
    updates.publish(str(scope), "changed", updating=False)
    assert updates.read_signal(str(other))["token"] == "initial"
    path = scope / "_CoqProject"
    path.write_text('-Q . ""\n')
    before = updates.artifact_snapshot(str(scope))
    path.write_text('-Q . ""\n')
    assert updates.artifact_snapshot(str(scope)) == before


def test_publish_failures_never_run_unannounced_or_acknowledge_update(scope, monkeypatch):
    output = scope / "Dep.vo"
    command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'new')", str(output)]
    original = updates.publish
    with monkeypatch.context() as fault:
        fault.setattr(updates, "publish", lambda *_a, **_kw: (_ for _ in ()).throw(updates.WorkspaceUpdateError("publish failed")))
        with pytest.raises(updates.WorkspaceUpdateError):
            updates.run_update(str(scope), command)
        assert not output.exists()
    def finish_fails(workspace, token, *, updating):
        if not updating:
            raise updates.WorkspaceUpdateError("completion failed")
        original(workspace, token, updating=updating)
    with monkeypatch.context() as fault:
        fault.setattr(updates, "publish", finish_fails)
        with pytest.raises(updates.WorkspaceUpdateError):
            updates.run_update(str(scope), command)
    assert output.exists() and updates.read_signal(str(scope))["updating"] is True
    assert updates.run_update(str(scope), [sys.executable, "-c", "pass"], recover=True) == 0
    assert not updates.read_signal(str(scope))["updating"]


@pytest.mark.asyncio
async def test_update_protocol_failure_keeps_token_and_retries_before_new_state(scope, backend, monkeypatch):
    pet, state, _calls = backend
    await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
    before = state["workspace_signal"]
    sid = interactive._state_add(SimpleNamespace(proof_finished=False), "t.v", "t", str(scope), None, None, 0)
    updates.publish(str(scope), "new", updating=False)
    with monkeypatch.context() as fault:
        fault.setattr(pet, "_read_lsp_response", lambda: (_ for _ in ()).throw(TimeoutError("protocol timeout")))
        result = await server._run_with_pet(lambda _: pytest.fail("ran after failed refresh"), state, "test", workspace=str(scope))
        assert result["reason"] == "unavailable" and state["workspace_signal"] == before
        assert not interactive._state_table and state["workspace_refresh_failed"]
    fresh = await server._run_with_pet(lambda _: {"success": True}, state, "test", workspace=str(scope))
    assert fresh["success"] and fresh["workspace_refreshed"] and not state["workspace_refresh_failed"]
    assert state["workspace_signal"][1] == "new" and sid not in interactive._state_table


def test_completion_scan_failure_leaves_pending(scope, monkeypatch):
    original, calls = updates.artifact_snapshot, []
    def snapshot(ws):
        calls.append(1)
        if len(calls) == 2:
            raise updates.WorkspaceUpdateError("end scan failed")
        return original(ws)
    monkeypatch.setattr(updates, "artifact_snapshot", snapshot)
    with pytest.raises(updates.WorkspaceUpdateError):
        updates.run_update(str(scope), [sys.executable, "-c", "pass"])
    assert updates.read_signal(str(scope))["updating"]


@pytest.mark.asyncio
async def test_post_execution_signal_io_failure_keeps_execution_facts(scope, backend, monkeypatch):
    _pet, state, _calls = backend
    original, calls = server.read_signal, []
    def read(ws):
        calls.append(1)
        if len(calls) == 2:
            raise updates.WorkspaceUpdateError("read failed")
        return original(ws)
    monkeypatch.setattr(server, "read_signal", read)
    result = await server._run_with_pet(lambda _: {"success": True, "state_id": 1, "proof_finished": True},
                                        state, "test", workspace=str(scope))
    assert result["reason"] == "unavailable" and result["execution_completed"]
    assert result["execution_success"] and result["proof_finished"] and "state_id" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["results", "partial_results"])
@pytest.mark.parametrize("read_failure", [False, True])
async def test_inflight_candidate_outcomes_are_retained_without_live_ids(scope, backend, monkeypatch, collection, read_failure):
    _pet, state, _calls = backend
    original, reads = server.read_signal, []
    def read(ws):
        reads.append(1)
        if read_failure and len(reads) == 2:
            raise updates.WorkspaceUpdateError("read failed")
        return original(ws)
    monkeypatch.setattr(server, "read_signal", read)
    rows = [{"success": True, "proof_finished": True, "tactic": "exact I.", "state_id": 777},
            {"success": False, "proof_finished": False, "reason": "tactic_failed", "error": "bad type", "tactic": "exact 0.", "last_valid_state_id": 778}]
    def action(_):
        if not read_failure:
            updates.publish(str(scope), "changed", updating=False)
        return {"success": collection == "results", collection: rows}
    result = await server._run_with_pet(action, state, "test", workspace=str(scope))
    assert result["execution_" + collection] == [
        {key: value for key, value in row.items() if key not in {"state_id", "last_valid_state_id"}}
        for row in rows]
    assert result["reason"] == ("unavailable" if read_failure else "state_invalidated")
