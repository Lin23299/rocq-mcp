"""CCV原生位置/句柄接线；仅在明确启用的匹配后端使用。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

_local = threading.local()


def enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def begin(pet: Any, lifespan: dict) -> None:
    if not (enabled("ROCQ_STATE_RELEASE") or enabled("ROCQ_SOURCE_ANCHORS")):
        return
    if not getattr(pet, "_ccv_native_attached", False):
        original = pet._read_lsp_response
        pet._ccv_pending = set()
        pet._ccv_anchors = {}

        def read():
            raw = original()
            reply = json.loads(raw)
            result = reply.get("result") if isinstance(reply, dict) else None
            if isinstance(result, dict) and type(result.get("st")) is int:
                st = result["st"]
                pet._ccv_pending.add(st)
                anchor = result.pop("source_anchor", None)
                if anchor is not None:
                    pet._ccv_anchors[st] = anchor
                raw = json.dumps(reply, ensure_ascii=False)
            return raw

        pet._read_lsp_response = read
        pet._ccv_native_attached = True
    _local.active = (pet, lifespan)


def assert_current() -> None:
    active = getattr(_local, "active", None)
    if active and active[1].get("pet_client") is not active[0]:
        raise RuntimeError("Native state belongs to a retired Pet instance")


def retire(state: Any) -> None:
    active = getattr(_local, "active", None)
    st = getattr(state, "st", None)
    if active and type(st) is int:
        # 只写本次捕获的实例，绝不把旧线程清理转发给新进程。
        active[0]._ccv_pending.add(st)


def rpc(pet: Any, method: str, params: dict) -> Any:
    pet.id += 1
    request_id = pet.id
    pet._send_lsp_message(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                    "method": method, "params": params}))
    reply = json.loads(pet._read_lsp_response())
    if reply.get("id") != request_id or "error" in reply or "result" not in reply:
        raise RuntimeError(f"Native {method} failed: {reply.get('error', 'invalid reply')}")
    return reply["result"]


def finish(pet: Any, lifespan: dict) -> None:
    try:
        if lifespan.get("pet_client") is not pet:
            return
        if enabled("ROCQ_STATE_RELEASE") and pet._ccv_pending:
            from rocq_mcp.interactive import _state_table, _import_cache
            live = {getattr(e.state, "st", None) for e in _state_table.values()}
            live.update(getattr(e.state, "st", None) for e in _import_cache.values())
            dead = sorted(pet._ccv_pending - live)
            if dead:
                result = rpc(pet, "petanque/release_states", {"states": dead})
                if (not isinstance(result, dict) or type(result.get("released")) is not int
                        or type(result.get("remaining")) is not int
                        or not 0 <= result["released"] <= len(dead)):
                    raise RuntimeError("Invalid native release acknowledgement")
                lifespan["native_release_stats"] = result
            pet._ccv_pending.clear()
            lifespan.pop("native_release_error", None)
        pet._ccv_anchors.clear()
    finally:
        _local.active = None


def snapshot(pet: Any, state: Any, file: str) -> tuple[str, int] | None:
    if not enabled("ROCQ_SOURCE_ANCHORS"):
        return None
    anchor = pet._ccv_anchors.pop(state.st, None)
    raw = Path(file).read_bytes()
    if (not isinstance(anchor, dict) or anchor.get("snapshot_bytes") != len(raw)
            or anchor.get("snapshot_md5") != hashlib.md5(raw).hexdigest()):
        raise RuntimeError("Native source anchor is absent or its snapshot changed during start")
    offset = anchor.get("offset_bytes")
    if type(offset) is not int or not 0 <= offset <= len(raw):
        raise RuntimeError("Invalid native source boundary")
    text = raw.decode("utf-8", errors="strict")
    consumed = len(raw[:offset].decode("utf-8", errors="strict"))
    return text, consumed
