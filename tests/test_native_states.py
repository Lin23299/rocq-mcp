"""跨层接线反例：删除必须真到后端，别名/临时句柄/失败均有覆盖。"""
import hashlib
import json
from types import SimpleNamespace

import pytest
import rocq_mcp.server
from rocq_mcp import interactive as inter, native_states as native


class Pet:
    def __init__(self):
        self.id = 0
        self.handles = set()
        self.releases = []
        self.reply = None
        self.fail = False
        self.process = SimpleNamespace(poll=lambda: None)

    def _send_lsp_message(self, text):
        req = json.loads(text)
        ids = req["params"]["states"]
        self.releases.append(ids)
        if self.fail:
            self.reply = {"id": req["id"], "error": {"message": "injected"}}
        else:
            before = len(self.handles)
            self.handles.difference_update(ids)
            self.reply = {"id": req["id"], "result": {
                "released": before - len(self.handles), "remaining": len(self.handles)}}

    def _read_lsp_response(self):
        return json.dumps(self.reply)

    def produced(self, st, anchor=None):
        self.handles.add(st)
        result = {"st": st, "hash": 0, "proof_finished": False, "feedback": []}
        if anchor is not None:
            result["source_anchor"] = anchor
        self.reply = {"id": 1, "result": result}
        # 经生产SDK实际接点，而不是直接填pending。
        self._read_lsp_response()
        return SimpleNamespace(st=st, proof_finished=False)


@pytest.fixture
def setup(monkeypatch):
    inter._state_invalidate_all()
    inter._invalidate_import_cache()
    monkeypatch.setenv("ROCQ_STATE_RELEASE", "1")
    monkeypatch.setenv("ROCQ_SOURCE_ANCHORS", "1")
    pet = Pet()
    life = {"pet_client": pet}
    native.begin(pet, life)
    yield pet, life
    native._local.active = None
    inter._state_invalidate_all()
    inter._invalidate_import_cache()


def add(state):
    return inter._state_add(state, "f.v", "t", "/tmp", None, None, 0)


def test_actual_release_at_lru_boundary(setup, monkeypatch):
    pet, life = setup
    monkeypatch.setattr(inter, "_MAX_STATES", 2)
    for st in range(1, 5):
        add(pet.produced(st))
    native.finish(pet, life)
    assert pet.handles == {3, 4}
    assert pet.releases == [[1, 2]]


def test_cached_import_alias_is_kept_until_last_owner(setup):
    pet, life = setup
    state = pet.produced(1)
    inter._import_cache["x"] = inter._CachedImportContext(state, "x", "/tmp", 0)
    a, b = add(state), add(state)
    inter._state_remove(a)
    native.finish(pet, life)
    assert pet.handles == {1} and not pet.releases
    native.begin(pet, life)
    inter._state_remove(b)
    inter._invalidate_import_cache()
    native.finish(pet, life)
    assert not pet.handles


def test_unregistered_candidates_are_released(setup):
    pet, life = setup
    add(pet.produced(1))
    for st in range(2, 22):
        pet.produced(st)
    native.finish(pet, life)
    assert pet.handles == {1}
    assert len(pet.releases) == 1


def test_failed_ack_preserves_pending_and_retry_is_idempotent(setup):
    pet, life = setup
    pet.produced(1)
    pet.fail = True
    with pytest.raises(RuntimeError):
        native.finish(pet, life)
    assert pet._ccv_pending == {1}
    native.begin(pet, life)
    pet.fail = False
    native.finish(pet, life)
    assert not pet.handles and not pet._ccv_pending


def test_retired_instance_never_cleans_new_pet(setup):
    pet, life = setup
    pet.produced(1)
    replacement = Pet()
    replacement.handles.add(1)
    life["pet_client"] = replacement
    with pytest.raises(RuntimeError):
        add(SimpleNamespace(st=1, proof_finished=False))
    native.finish(pet, life)
    assert replacement.handles == {1} and not replacement.releases


def test_no_pending_has_no_release_rpc(setup):
    pet, life = setup
    native.finish(pet, life)
    assert not pet.releases


def test_snapshot_uses_native_byte_boundary_and_rejects_changed_file(setup, tmp_path):
    pet, life = setup
    raw = "(* 中文😀 *)\r\nLemma t : True.\r\nProof. exact I. Qed.\r\n".encode()
    file = tmp_path / "f.v"
    file.write_bytes(raw)
    end = raw.index(b"Proof.")
    anchor = {"snapshot_md5": hashlib.md5(raw).hexdigest(),
              "snapshot_bytes": len(raw), "offset_bytes": end}
    state = pet.produced(1, anchor)
    text, offset = native.snapshot(pet, state, str(file))
    assert text == raw.decode() and offset == len(raw[:end].decode())
    state = pet.produced(2, anchor)
    file.write_bytes(raw + b" ")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        native.snapshot(pet, state, str(file))


def test_utf16_resume_conversion():
    text = "😀x\nabc"
    assert inter._offset_to_line_col(text, 2) == (0, 3)
    assert inter._line_col_to_offset(text, 0, 3) == 2


@pytest.mark.asyncio
async def test_production_exit_releases_unpublished_states(setup, monkeypatch):
    from tests.conftest import make_lifespan_state
    import rocq_mcp.server as server
    pet, _ = setup
    native._local.active = None
    life = make_lifespan_state()
    life["pet_client"] = pet
    monkeypatch.setattr(server, "_ensure_pet", lambda _: pet)
    def execute(_):
        sid = add(pet.produced(1))
        pet.produced(2)
        return {"success": True, "state_id": sid}
    result = await server._run_with_pet(execute, life, "test")
    assert result["success"] and result["state_id"] in inter._state_table
    assert pet.handles == {1} and pet.releases == [[2]]


@pytest.mark.asyncio
async def test_release_failure_does_not_reverse_execution(setup, monkeypatch):
    from tests.conftest import make_lifespan_state
    import rocq_mcp.server as server
    pet, _ = setup
    native._local.active = None
    life = make_lifespan_state()
    life["pet_client"] = pet
    monkeypatch.setattr(server, "_ensure_pet", lambda _: pet)
    pet.fail = True
    def execute(_):
        sid = add(pet.produced(1))
        pet.produced(2)
        return {"success": True, "state_id": sid, "proof_finished": True}
    result = await server._run_with_pet(execute, life, "test")
    assert result["success"] and result["proof_finished"]
    assert result["state_id"] in inter._state_table and "state_release_error" in result
    assert pet._ccv_pending == {1, 2}
