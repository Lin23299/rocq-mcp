"""Saved tool output: byte-exact retrieval, bounded literal search, ownership."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from rocq_mcp.output_store import OutputStore, OutputStoreError
import rocq_mcp.output_store as storage


@pytest.fixture
def saved(tmp_path):
    root = tmp_path / "private"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = OutputStore(root, workspace=workspace, allow_test_temp_root=True)
    try:
        yield store, workspace
    finally:
        store.close()


def _put(store, workspace, text):
    return store.save(text, owner_session="worker-one", workspace=str(workspace),
                      origin="rocq_query", warning_filter="include_warnings",
                      covers_filtered_all=True)


def _read(store, workspace, handle, **kwargs):
    return store.read(handle, owner_session="worker-one", workspace=str(workspace), **kwargs)


def _find(store, workspace, handle, text, **kwargs):
    return store.find(handle, text, owner_session="worker-one", workspace=str(workspace), **kwargs)


def test_production_store_rejects_tmp_before_creating_directories(tmp_path):
    target = tmp_path / "production-root-should-not-exist"
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert not target.exists()
    with pytest.raises(OutputStoreError) as err:
        OutputStore(target, workspace=ws)
    assert err.value.code == "store_failed" and not target.exists()


def test_byte_exact_unicode_roundtrip_and_eof(saved):
    store, workspace = saved
    text = ("甲🙂\r\n" * 49) + "finished\n"
    entry = _put(store, workspace, text)
    original = text.encode("utf-8")
    offset = 0
    chunks = []
    while offset < len(original):
        result = _read(store, workspace, entry["handle"], offset_bytes=offset, max_bytes=10)
        assert result["status"] == "data"
        assert result["end"] > offset
        assert hashlib.sha256(result["text"].encode()).hexdigest() == result["chunk_sha256"]
        chunks.append(result["text"].encode())
        offset = result["end"]
    assert b"".join(chunks) == original
    assert entry["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert _read(store, workspace, entry["handle"], offset_bytes=offset)["status"] == "eof"
    with pytest.raises(OutputStoreError, match="Offset splits") as err:
        _read(store, workspace, entry["handle"], offset_bytes=1)
    assert err.value.code == "invalid_boundary"
    with pytest.raises(OutputStoreError) as err:
        _read(store, workspace, entry["handle"], offset_bytes=0, max_bytes=1)
    assert err.value.code == "invalid_range"


def test_long_line_tail_cross_buffer_and_overlapping_hits(saved):
    store, workspace = saved
    text = "x" * (1024 * 1024 - 3) + "_TARGET_尾" + "x" * 205_000
    entry = _put(store, workspace, text)
    results = _find(store, workspace, entry["handle"], "TARGET", cursor=0)
    assert results["has_more"]
    assert len(results["hits"]) == 1
    pos = results["hits"][0]["offset_bytes"]
    assert pos > 0 and pos < 1024 * 1024
    assert _read(store, workspace, entry["handle"], offset_bytes=pos)["text"].startswith("TARGET")
    assert not _find(store, workspace, entry["handle"], "TARGET",
                     cursor=results["next_cursor"])["hits"]

    repeated = _put(store, workspace, "aaaaaa")
    cursor, positions = 0, []
    while True:
        found = _find(store, workspace, repeated["handle"], "aaa", cursor=cursor, max_hits=2)
        positions.extend(hit["offset_bytes"] for hit in found["hits"])
        if not found["has_more"]:
            break
        cursor = found["next_cursor"]
    assert positions == [0, 1, 2, 3]


def test_search_utf8_context_and_unicode_cursor(saved):
    store, workspace = saved
    text = "中" * 50 + "🙂" * 2 + "_尾_TOKEN"
    entry = _put(store, workspace, text)
    first = _find(store, workspace, entry["handle"], "🙂", max_hits=1)
    assert first["hits"][0]["offset_bytes"] == 150
    assert "🙂" in first["hits"][0]["snippet"]
    second = _find(store, workspace, entry["handle"], "🙂", cursor=first["next_cursor"])
    assert [x["offset_bytes"] for x in second["hits"]] == [154]


def test_owner_workspace_generation_and_expiry(saved, tmp_path):
    store, workspace = saved
    entry = _put(store, workspace, "secret")
    with pytest.raises(OutputStoreError) as err:
        store.read(entry["handle"], owner_session="worker-two", workspace=str(workspace),
                   offset_bytes=0)
    assert err.value.code == "unauthorized"
    other_ws = tmp_path / "other"
    other_ws.mkdir()
    with pytest.raises(OutputStoreError) as err:
        store.find(entry["handle"], "secret", owner_session="worker-one",
                   workspace=str(other_ws))
    assert err.value.code == "unauthorized"
    with OutputStore(tmp_path / "other-root", workspace=workspace,
                     allow_test_temp_root=True) as other:
        with pytest.raises(OutputStoreError) as err:
            other.read(entry["handle"], owner_session="worker-one",
                       workspace=str(workspace), offset_bytes=0)
        assert err.value.code == "expired"


def test_quota_ttl_tamper_and_invalid_parameters(tmp_path):
    tick = [10.0]
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = OutputStore(tmp_path / "store", workspace=ws, clock=lambda: tick[0],
                        allow_test_temp_root=True,
                        max_snapshot_bytes=20, max_store_bytes=6, ttl_seconds=10)
    try:
        entry = _put(store, ws, "abc")
        with pytest.raises(OutputStoreError) as err:
            _put(store, ws, "z" * 21)
        assert err.value.code == "quota_exceeded"
        with pytest.raises(OutputStoreError) as err:
            _put(store, ws, "more")
        assert err.value.code == "quota_exceeded"
        with pytest.raises(OutputStoreError) as err:
            _read(store, ws, entry["handle"], offset_bytes=True)
        assert err.value.code == "invalid_range"
        with pytest.raises(OutputStoreError) as err:
            _find(store, ws, entry["handle"], "")
        assert err.value.code == "invalid_range"
        with pytest.raises(OutputStoreError) as err:
            _find(store, ws, entry["handle"], "a", max_hits=21)
        assert err.value.code == "invalid_range"
        tick[0] += 10
        with pytest.raises(OutputStoreError) as err:
            _read(store, ws, entry["handle"], offset_bytes=0)
        assert err.value.code == "expired"
        assert not store._entries
        with pytest.raises(OutputStoreError) as err:
            store.read(entry["handle"], owner_session="other", workspace=str(ws), offset_bytes=0)
        assert err.value.code == "unauthorized"
        forged = entry["handle"][:-1] + ("0" if entry["handle"][-1] != "0" else "1")
        with pytest.raises(OutputStoreError) as err:
            _read(store, ws, forged, offset_bytes=0)
        # A changed owner tag is a bad signed handle, not a different caller.
        assert err.value.code == "not_found"
        parts = entry["handle"].split(".")
        parts[2] = "0" * len(parts[2])
        with pytest.raises(OutputStoreError) as err:
            _read(store, ws, ".".join(parts), offset_bytes=0)
        assert err.value.code == "not_found"
        assert _put(store, ws, "fresh")["total_bytes"] == 5
    finally:
        store.close()


def test_file_swap_is_not_read_as_original(saved):
    store, workspace = saved
    entry = _put(store, workspace, "original")
    item = store._entries[entry["handle"]]
    item.path.unlink()
    item.path.write_bytes(b"replacement")
    with pytest.raises(OutputStoreError) as err:
        _read(store, workspace, entry["handle"], offset_bytes=0)
    assert err.value.code == "stale"


def test_same_inode_same_size_restored_mtime_still_cannot_reuse_old_sha(saved):
    store, workspace = saved
    saved_result = _put(store, workspace, "secret")
    path = store._entries[saved_result["handle"]].path
    before = path.stat()
    time.sleep(0.02)  # Distinguish filesystems with coarse ctime resolution.
    with path.open("r+b") as stream:
        os.pwrite(stream.fileno(), b"public", 0)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns
    for operation in (
        lambda: _read(store, workspace, saved_result["handle"], offset_bytes=0),
        lambda: _find(store, workspace, saved_result["handle"], "public"),
    ):
        with pytest.raises(OutputStoreError) as err:
            operation()
        assert err.value.code == "stale"


def test_store_never_uses_workspace_or_exposes_path(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    nested = ws / "within-workspace"
    with pytest.raises(OutputStoreError) as err:
        OutputStore(nested, workspace=ws, allow_test_temp_root=True)
    assert err.value.code == "validation" and not nested.exists()
    with OutputStore(tmp_path / "outside", workspace=ws, allow_test_temp_root=True) as store:
        entry = _put(store, ws, "hello")
        assert all("/" not in str(value) for value in entry.values() if isinstance(value, str))


def test_store_rejects_both_workspace_containment_directions_and_physical_symlink(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    alias = tmp_path / "indirect"
    alias.symlink_to(ws, target_is_directory=True)
    with pytest.raises(OutputStoreError) as err:
        OutputStore(alias / "cache", workspace=ws, allow_test_temp_root=True)
    assert err.value.code == "validation" and not (ws / "cache").exists()

    root = tmp_path / "cache-parent"
    nested_ws = root / "project"
    nested_ws.mkdir(parents=True)
    with pytest.raises(OutputStoreError) as err:
        OutputStore(root, workspace=nested_ws, allow_test_temp_root=True)
    assert err.value.code == "validation"
    assert list(root.iterdir()) == [nested_ws]  # No session-/cache directory was created.

    outside = tmp_path / "outside"
    with OutputStore(outside, workspace=ws, allow_test_temp_root=True) as store:
        saved_result = _put(store, ws, "safe")
        before = list(store._entries)
        with pytest.raises(OutputStoreError) as err:
            store.save("blocked", owner_session="owner", workspace=str(outside / "session-old"),
                       origin="test")
        # Even if a later caller brings a valid workspace beneath the root,
        # no second workspace can be silently adopted by an already-open store.
        assert err.value.code == "validation"
        nested = outside / "session-old" / "project"
        nested.mkdir(parents=True)
        with pytest.raises(OutputStoreError) as err:
            store.save("blocked", owner_session="owner", workspace=str(nested), origin="test")
        assert err.value.code == "validation" and list(store._entries) == before
        with pytest.raises(OutputStoreError) as err:
            store.read(saved_result["handle"], owner_session="worker-one",
                       workspace=str(nested), offset_bytes=0)
        assert err.value.code == "validation"


def test_explicit_store_failure_does_not_publish(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with OutputStore(tmp_path / "outside", workspace=ws, allow_test_temp_root=True) as store:
        def fail(*args, **kwargs):
            raise OSError("injected write failure")

        monkeypatch.setattr(os, "replace", fail)
        with pytest.raises(OutputStoreError) as err:
            _put(store, ws, "never exposed")
        assert err.value.code == "store_failed"
        assert not store._entries
        assert not list(store._directory.iterdir())


@pytest.mark.parametrize("operation", ["save", "read", "find", "list_segments"])
def test_audit8_ttl_unlink_failure_is_atomic_and_retryable(tmp_path, monkeypatch, operation):
    tick = [0.0]
    ws = tmp_path / "workspace"
    ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, clock=lambda: tick[0], ttl_seconds=1,
                     max_store_bytes=6, allow_test_temp_root=True) as store:
        saved = store.save("secret", owner_session="owner", workspace=str(ws), origin="test",
                           segments=[(0, 0, 6)])
        path = store._entries[saved["handle"]].path
        before = (dict(store._entries), list(store._deadlines), store._used_bytes)
        tick[0] = 2.0
        unlink = Path.unlink

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError("injected TTL unlink failure")
            return unlink(target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", denied)
            options = {"owner_session": "owner", "workspace": str(ws)}
            if operation == "save":
                call = lambda: store.save("new", origin="test", **options)
            elif operation == "read":
                call = lambda: store.read(saved["handle"], offset_bytes=0, **options)
            elif operation == "find":
                call = lambda: store.find(saved["handle"], "secret", **options)
            else:
                call = lambda: store.list_segments(saved["handle"], **options)
            with pytest.raises(OutputStoreError) as error:
                call()
            assert error.value.code == "store_failed"
            assert (store._entries, store._deadlines, store._used_bytes) == before
            assert path.read_bytes() == b"secret" and list(store._directory.iterdir()) == [path]
        with pytest.raises(OutputStoreError) as error:
            store.read(saved["handle"], owner_session="owner", workspace=str(ws), offset_bytes=0)
        assert error.value.code == "expired" and not path.exists() and store._used_bytes == 0
        assert store.save("new", owner_session="owner", workspace=str(ws), origin="test")["total_bytes"] == 3


@pytest.mark.parametrize("cause,expected", [("quota", "quota_exceeded"), ("unicode", "validation"),
                                            ("io", "store_failed"), ("segment", "invalid_range"),
                                            ("post_replace", "store_failed")])
def test_audit8_rollback_failure_keeps_original_error_and_tracks_residual(tmp_path, monkeypatch, cause, expected):
    ws = tmp_path / "workspace"
    ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, max_snapshot_bytes=4,
                     max_store_bytes=4, allow_test_temp_root=True) as store:
        unlink, stat = Path.unlink, Path.stat
        stat_failed = [False]

        def denied(target, *args, **kwargs):
            if target.parent == store._directory and target.exists():
                raise PermissionError("injected rollback failure")
            return unlink(target, *args, **kwargs)

        def bad_stat(target, *args, **kwargs):
            if (cause == "post_replace" and target.parent == store._directory
                    and target.name.startswith("result-") and not stat_failed[0]):
                stat_failed[0] = True
                raise OSError("injected post-replace stat failure")
            return stat(target, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", denied)
            fault.setattr(Path, "stat", bad_stat)
            fault.setattr(storage, "_ENCODE_CHARS", 1)
            if cause == "io":
                fault.setattr(os, "fsync", lambda _: (_ for _ in ()).throw(OSError("write fault")))
            text = "abcde" if cause == "quota" else "a\ud800" if cause == "unicode" else "abc"
            options = {"segments": [(0, 0, 99)]} if cause == "segment" else {}
            with pytest.raises(OutputStoreError) as error:
                store.save(text, owner_session="owner", workspace=str(ws), origin="test", **options)
            assert error.value.code == expected
            leftovers = list(store._directory.iterdir())
            assert len(leftovers) == 1 and not store._entries and not store._deadlines
            assert store._used_bytes == sum(path.stat().st_size for path in leftovers)
            assert set(store._pending_cleanup) == set(leftovers)
        fresh = store.save("ok", owner_session="owner", workspace=str(ws), origin="test")
        assert not store._pending_cleanup and store._used_bytes == 2
        assert len(list(store._directory.iterdir())) == 1
        assert store.read(fresh["handle"], owner_session="owner", workspace=str(ws), offset_bytes=0)["text"] == "ok"


def test_audit8_close_failure_denies_reads_retains_accounting_and_retries(saved, monkeypatch):
    store, ws = saved
    result = _put(store, ws, "secret")
    with monkeypatch.context() as fault:
        fault.setattr(storage.shutil, "rmtree", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("close fault")))
        with pytest.raises(OutputStoreError) as error:
            store.close()
        assert error.value.code == "store_failed" and store._closed
        assert result["handle"] in store._entries and store._used_bytes == 6
        with pytest.raises(OutputStoreError) as error:
            _read(store, ws, result["handle"], offset_bytes=0)
        assert error.value.code == "expired"
    store.close()
    assert not store._directory.exists() and not store._entries and store._used_bytes == 0
    store.close()  # Idempotent only once physical cleanup really completed.


def test_audit8_context_cleanup_does_not_replace_primary_exception(saved, monkeypatch):
    store, ws = saved
    _put(store, ws, "secret")
    with monkeypatch.context() as fault:
        fault.setattr(storage.shutil, "rmtree", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("close fault")))
        with pytest.raises(ValueError, match="primary") as error:
            with store:
                raise ValueError("primary")
        assert any("Output cleanup" in note for note in error.value.__notes__)
        assert store._used_bytes == 6 and store._entries
    store.close()


def test_audit8_constructor_cleanup_scan_failure_is_stable(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    root = tmp_path / "private"
    original = Path.iterdir

    def denied(path):
        if path == root:
            raise PermissionError("scan fault")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(OutputStoreError) as error:
        OutputStore(root, workspace=ws, allow_test_temp_root=True)
    assert error.value.code == "store_failed"


def test_audit8_unreadable_rollback_size_reserves_upper_bound_until_retry(saved, monkeypatch):
    store, ws = saved
    store._max_snapshot_bytes = 4
    stat = Path.stat
    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", lambda _: (_ for _ in ()).throw(OSError("write fault")))

        def denied_unlink(path, *args, **kwargs):
            if path.parent == store._directory and path.name.startswith(".part-"):
                raise PermissionError("cannot remove")
            return unlink(path, *args, **kwargs)

        def denied_stat(path, *args, **kwargs):
            if path.parent == store._directory and path.name.startswith(".part-"):
                raise PermissionError("cannot inspect size")
            return stat(path, *args, **kwargs)

        unlink = Path.unlink
        fault.setattr(Path, "unlink", denied_unlink)
        fault.setattr(Path, "stat", denied_stat)
        with pytest.raises(OutputStoreError) as error:
            _put(store, ws, "abc")
        assert error.value.code == "store_failed" and store._used_bytes == 4
        assert list(store._pending_cleanup.values()) == [4]
    assert _put(store, ws, "ok")["total_bytes"] == 2
    assert store._used_bytes == 2 and not store._pending_cleanup


def test_crashed_generation_cleanup_without_reauthorizing_old_handle(tmp_path):
    root = tmp_path / "private"
    ws = tmp_path / "workspace"
    ws.mkdir()
    old = OutputStore(root, workspace=ws, ttl_seconds=5, allow_test_temp_root=True)
    handle = _put(old, ws, "disposable")["handle"]
    abandoned = old._directory
    past = time.time() - 30
    os.utime(abandoned, (past, past))
    with OutputStore(root, workspace=ws, ttl_seconds=5, allow_test_temp_root=True) as current:
        assert not abandoned.exists()
        with pytest.raises(OutputStoreError) as err:
            _read(current, ws, handle, offset_bytes=0)
        assert err.value.code == "expired"
    old.close()


def test_symlink_store_root_is_rejected(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    target = tmp_path / "actual"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(OutputStoreError) as err:
        OutputStore(alias, workspace=ws, allow_test_temp_root=True)
    assert err.value.code == "store_failed"


def test_oversized_utf8_stream_cleans_partial_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, max_snapshot_bytes=1000,
                     allow_test_temp_root=True) as store:
        with pytest.raises(OutputStoreError) as err:
            _put(store, ws, "中" * 10_000)
        assert err.value.code == "quota_exceeded"
        assert not store._entries
        assert not list(store._directory.iterdir())
        assert _put(store, ws, "still usable")["total_bytes"] == 12


def test_expiry_lookup_work_does_not_grow_with_live_results(tmp_path):
    calls = [0]

    def clock():
        calls[0] += 1
        return 0.0

    ws = tmp_path / "ws"
    ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, clock=clock,
                     allow_test_temp_root=True) as store:
        handles = [_put(store, ws, f"result{i}")["handle"] for i in range(100)]
        calls[0] = 0
        for _ in range(20):
            assert _read(store, ws, handles[-1], offset_bytes=0)["status"] == "data"
        assert calls[0] == 20  # Heap-head check, not 100 entries per request.


def test_feedback_segment_index_is_bounded_owned_and_atomically_validated(saved):
    store, workspace = saved
    saved_batch = store.save("head\nbody", owner_session="worker-one",
                              workspace=str(workspace), origin="rocq_check",
                              warning_filter="include_warnings", covers_filtered_all=True,
                              segments=[(0, 0, 4), (5, 5, 9)])
    first = store.list_segments(saved_batch["handle"], owner_session="worker-one",
                                workspace=str(workspace), max_entries=1)
    assert first["items"] == [{"index": 0, "span_start": 0, "span_end": 4}]
    assert first["next_cursor"] == 1 and first["has_more"]
    second = store.list_segments(saved_batch["handle"], owner_session="worker-one",
                                 workspace=str(workspace), cursor=1)
    assert second["items"] == [{"index": 5, "span_start": 5, "span_end": 9}]
    assert not second["has_more"]
    with pytest.raises(OutputStoreError) as err:
        store.list_segments(saved_batch["handle"], owner_session="other",
                            workspace=str(workspace))
    assert err.value.code == "unauthorized"
    plain = _put(store, workspace, "ordinary query")
    with pytest.raises(OutputStoreError) as err:
        store.list_segments(plain["handle"], owner_session="worker-one",
                            workspace=str(workspace))
    assert err.value.code == "validation"
    before = len(store._entries)
    with pytest.raises(OutputStoreError) as err:
        store.save("short", owner_session="worker-one", workspace=str(workspace),
                   origin="rocq_check", warning_filter="include_warnings",
                   covers_filtered_all=True, segments=[(0, 0, 999)])
    assert err.value.code == "invalid_range"
    with pytest.raises(OutputStoreError) as err:
        store.save("short", owner_session="worker-one", workspace=str(workspace),
                   origin="rocq_check", warning_filter="include_warnings",
                   covers_filtered_all=True, segments=[(0, 0, 3), (1, 2, 5)])
    assert err.value.code == "invalid_range"
    assert len(store._entries) == before
    assert not any(p.name.startswith(".part-") for p in store._directory.iterdir())


def test_feedback_snapshot_requires_explicit_filter_and_whole_filtered_scope(saved):
    store, workspace = saved
    before = len(store._entries)
    for options in ({}, {"warning_filter": "include_warnings"},
                    {"warning_filter": "not_applicable", "covers_filtered_all": True}):
        with pytest.raises(OutputStoreError) as err:
            store.save("same", owner_session="worker-one", workspace=str(workspace),
                       origin="rocq_query", **options)
        assert err.value.code == "validation"
    assert len(store._entries) == before
    first = store.save("same", owner_session="worker-one", workspace=str(workspace),
                       origin="rocq_query", warning_filter="include_warnings",
                       covers_filtered_all=True)
    other = store.save("same", owner_session="worker-one", workspace=str(workspace),
                       origin="rocq_query", warning_filter="exclude_warnings",
                       covers_filtered_all=True)
    assert first["handle"] != other["handle"]
    assert first["source_sha256"] == other["source_sha256"]
    assert _read(store, workspace, other["handle"], offset_bytes=0)["warning_filter"] == "exclude_warnings"
    assert _find(store, workspace, first["handle"], "same")["covers_filtered_all"] is True


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("operation", ["read", "find", "list_segments"])
@pytest.mark.parametrize("changed_part", [1, 2, 3])
def test_signed_handle_tamper_is_not_found_before_caller_check(tmp_path, expired, operation, changed_part):
    tick = [0.0]
    ws = tmp_path / "workspace"
    ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, clock=lambda: tick[0],
                     ttl_seconds=10, allow_test_temp_root=True) as store:
        handle = store.save("secret", owner_session="owner", workspace=str(ws),
                            origin="test", segments=[(0, 0, 6)])["handle"]
        if expired:
            tick[0] = 10.0
            store._purge_expired()
            assert not store._entries
        parts = handle.split(".")
        parts[changed_part] = ("0" if parts[changed_part][0] != "0" else "1") + parts[changed_part][1:]
        forged = ".".join(parts)
        options = {"owner_session": "owner", "workspace": str(ws)}
        if operation == "read":
            options["offset_bytes"] = 0
        elif operation == "find":
            options["literal"] = "secret"
        # Both the correct owner and an unrelated caller must see a bad MAC,
        # not unauthorized.  Authentication of H precedes caller comparison.
        for caller in ("owner", "wrong-owner"):
            with pytest.raises(OutputStoreError) as err:
                getattr(store, operation)(forged, **{**options, "owner_session": caller})
            assert err.value.code == "not_found"


@pytest.mark.parametrize("operation", ["read", "find", "list_segments"])
def test_expired_valid_handle_distinguishes_owner_workspace_and_tag_splicing(tmp_path, operation):
    tick = [0.0]
    ws = tmp_path / "workspace"
    other_ws = tmp_path / "other-workspace"
    ws.mkdir()
    other_ws.mkdir()
    with OutputStore(tmp_path / "private", workspace=ws, clock=lambda: tick[0],
                     ttl_seconds=10, allow_test_temp_root=True) as store:
        first = store.save("secret", owner_session="owner", workspace=str(ws),
                           origin="test", segments=[(0, 0, 6)])["handle"]
        second = store.save("second", owner_session="other", workspace=str(ws), origin="test")["handle"]
        tick[0] = 10.0
        store._purge_expired()
        assert not store._entries and not list(store._directory.iterdir())
        options = {"offset_bytes": 0} if operation == "read" else (
            {"literal": "secret"} if operation == "find" else {})
        for owner, workspace, expected in (
            ("owner", ws, "expired"), ("other", ws, "unauthorized"),
            ("owner", other_ws, "unauthorized"),
        ):
            with pytest.raises(OutputStoreError) as err:
                getattr(store, operation)(first, owner_session=owner, workspace=str(workspace), **options)
            assert err.value.code == expected
        mixed = first.split(".")
        mixed[3] = second.split(".")[3]
        with pytest.raises(OutputStoreError) as err:
            getattr(store, operation)(".".join(mixed), owner_session="owner", workspace=str(ws), **options)
        assert err.value.code == "not_found"


@pytest.mark.parametrize("bad", [None, 1, "random", "a" * 300, "é" * 195])
def test_unknown_handle_format_never_looks_like_expired_generation(saved, bad):
    store, ws = saved
    with pytest.raises(OutputStoreError) as err:
        _read(store, ws, bad, offset_bytes=0)
    assert err.value.code == "not_found"


@pytest.mark.parametrize("operation", ["read", "find", "list_segments"])
@pytest.mark.parametrize("bad_character", ["é", "\ud800", "\n"])
def test_same_generation_invalid_mac_text_does_not_escape_as_encoding_error(saved, operation, bad_character):
    store, ws = saved
    handle = _put(store, ws, "secret")["handle"]
    # Keep the genuine generation/nonce/issued prefix and the exact length.
    parts = handle.split(".")
    parts[3] = bad_character + parts[3][1:]
    invalid = ".".join(parts)
    assert len(invalid) == len(handle) and invalid.startswith(store.generation + ".")
    options = {"offset_bytes": 0} if operation == "read" else (
        {"literal": "secret"} if operation == "find" else {})
    with pytest.raises(OutputStoreError) as err:
        getattr(store, operation)(invalid, owner_session="worker-one", workspace=str(ws), **options)
    assert err.value.code == "not_found"


@settings(max_examples=90, deadline=None)
@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=250),
       limit=st.integers(min_value=4, max_value=127))
def test_property_chunks_roundtrip_any_utf8(tmp_path_factory, text, limit):
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.getbasetemp()) as test_dir:
        root = Path(test_dir)
        ws = root / "ws"
        ws.mkdir()
        with OutputStore(root / "private", workspace=ws, allow_test_temp_root=True) as store:
            entry = _put(store, ws, text)
            source = text.encode("utf-8")
            offset, parts = 0, []
            while offset < len(source):
                part = _read(store, ws, entry["handle"], offset_bytes=offset, max_bytes=limit)
                assert part["end"] > offset
                parts.append(part["text"].encode("utf-8"))
                offset = part["end"]
            assert b"".join(parts) == source
            assert entry["source_sha256"] == hashlib.sha256(source).hexdigest()


@settings(max_examples=75, deadline=None)
@given(left=st.text(alphabet="ab🙂", max_size=95),
       right=st.text(alphabet="ab🙂", max_size=95),
       literal=st.sampled_from(["aba", "🙂a"]),
       page=st.integers(min_value=1, max_value=5))
def test_property_literal_pages_cover_every_offset(tmp_path_factory, left, right, literal, page):
    with tempfile.TemporaryDirectory(dir=tmp_path_factory.getbasetemp()) as test_dir:
        root = Path(test_dir)
        ws = root / "ws"
        ws.mkdir()
        source = (left + literal + right).encode("utf-8")
        needle = literal.encode("utf-8")
        expected, offset = [], 0
        while True:
            pos = source.find(needle, offset)
            if pos < 0:
                break
            expected.append(pos)
            offset = pos + 1
        with OutputStore(root / "private", workspace=ws, allow_test_temp_root=True) as store:
            entry = _put(store, ws, source.decode("utf-8"))
            cursor, found = 0, []
            while True:
                result = _find(store, ws, entry["handle"], literal,
                               cursor=cursor, max_hits=page)
                found.extend(hit["offset_bytes"] for hit in result["hits"])
                if not result["has_more"]:
                    break
                assert result["next_cursor"] > cursor
                cursor = result["next_cursor"]
            assert found == expected
