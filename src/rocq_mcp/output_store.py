"""Bounded snapshots of *printed* Rocq tool output, with explicit owners.

Nothing in this module is an executable proof state.  A handle is deliberately
not a file path: the only way to inspect a saved result is through the bounded
read and literal-search operations below.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

from rocq_mcp.output_contract import (
    RANGE_STATUS_DATA, RANGE_STATUS_EOF,
    WARNING_FILTER_MODES, WARNING_FILTER_NOT_APPLICABLE,
)


MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_STORE_BYTES = 256 * 1024 * 1024
SNAPSHOT_TTL_SECONDS = 24 * 60 * 60
MAX_READ_BYTES = 8 * 1024
MAX_FIND_HITS = 20
MAX_FIND_SCAN_BYTES = 1024 * 1024
FIND_TIMEOUT_SECONDS = 5
MAX_LITERAL_BYTES = 256
_CONTEXT_BYTES = 64
_ENCODE_CHARS = 16 * 1024
_MAX_FIND_PREVIEW_BYTES = 8 * 1024


class OutputStoreError(Exception):
    """Expected retrieval failure, with a stable, non-path diagnostic code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _handle_parts(handle: str) -> tuple[str, str, str, str]:
    """Bounded syntax check, independent of cache creation or caller identity."""
    if not isinstance(handle, str) or len(handle) != 195:
        raise OutputStoreError("not_found", "Output handle is not valid.")
    match = re.fullmatch(
        r"([0-9a-f]{32})\.([A-Za-z0-9_-]{32})\.([0-9a-f]{64})\.([0-9a-f]{64})", handle,
    )
    if match is None:
        raise OutputStoreError("not_found", "Output handle is not valid.")
    return match.group(1), match.group(2), match.group(3), match.group(4)


def reject_unregistered_handle(handle: str) -> NoReturn:
    """No store has issued H here; reject old-format handles without disk I/O.

    Another process's MAC cannot be authenticated with a process-local key.
    Expired therefore means unavailable here, not proof of prior issuance.
    Malformed guesses are still not_found.
    """
    _handle_parts(handle)
    raise OutputStoreError("expired", "This output handle is unavailable in this server generation.")


@dataclass(frozen=True)
class _Saved:
    path: Path
    owner: str
    workspace: str
    origin: str
    warning_filter: str
    covers_filtered_all: bool | None
    total_bytes: int
    sha256: str
    created_at: float
    device: int
    inode: int
    modified_ns: int
    changed_ns: int
    segments: tuple[tuple[int, int, int], ...] | None


def _private_root(root: Path, *, allow_test_temp_root: bool = False) -> Path:
    if not root.is_absolute() or root.is_symlink():
        raise OutputStoreError("store_failed", "Output store root must be a real absolute directory.")
    try:
        physical = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise OutputStoreError("store_failed", "Output store root cannot be resolved.") from exc
    if physical.is_relative_to(Path("/tmp").resolve()) and not allow_test_temp_root:
        raise OutputStoreError("store_failed", "Output store cannot live under /tmp.")
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = root.stat()
    except OSError as exc:
        raise OutputStoreError("store_failed", "Cannot open private output store.") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OutputStoreError("store_failed", "Output store is not private to this user.")
    physical = root.resolve()
    if physical.is_relative_to(Path("/tmp").resolve()) and not allow_test_temp_root:
        raise OutputStoreError("store_failed", "Output store cannot live under /tmp.")
    return physical


def _default_root() -> Path:
    configured = os.environ.get("ROCQ_MCP_OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return data / "rocq-mcp" / "output"


def _disjoint_workspace(root: Path, workspace: str | Path) -> str:
    """Check both physical containment directions before creating any cache path."""
    try:
        physical_root = root.resolve(strict=False)
        ws = Path(workspace).resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise OutputStoreError("validation", "Proof workspace cannot be resolved.") from exc
    if not ws.is_dir() or physical_root.is_relative_to(ws) or ws.is_relative_to(physical_root):
        raise OutputStoreError("validation", "Output store must be disjoint from the proof workspace.")
    return str(ws)


def _strict_positive(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise OutputStoreError("invalid_range", f"{name} must be between 1 and {maximum}.")


def _strict_offset(value: int, total: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise OutputStoreError("invalid_range", f"{name} must be a nonnegative integer.")
    if value > total:
        raise OutputStoreError("out_of_range", f"{name} exceeds the saved result length.")


class OutputStore:
    """An ephemeral output store scoped to one MCP server generation."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        workspace: str | Path,
        clock: Callable[[], float] = time.monotonic,
        max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES,
        max_store_bytes: int = MAX_STORE_BYTES,
        ttl_seconds: int = SNAPSHOT_TTL_SECONDS,
        allow_test_temp_root: bool = False,
    ) -> None:
        self.generation = secrets.token_hex(16)
        self._handle_secret = secrets.token_bytes(32)
        target = root if root is not None else _default_root()
        _disjoint_workspace(target, workspace)
        self._root = _private_root(target,
                                   allow_test_temp_root=allow_test_temp_root)
        self._clock = clock
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_store_bytes = max_store_bytes
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _Saved] = {}
        self._deadlines: list[tuple[float, str]] = []
        self._used_bytes = 0
        self._pending_cleanup: dict[Path, int] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._cleanup_complete = False
        try:
            self._prune_crashed_generations()
            self._directory = Path(tempfile.mkdtemp(prefix="session-", dir=self._root))
        except OSError as exc:
            raise OutputStoreError("store_failed", "Could not initialize output storage.") from exc

    def _prune_crashed_generations(self) -> None:
        """Crash remnants have no valid handle in this new server generation."""
        cutoff = time.time() - self._ttl_seconds
        for child in self._root.iterdir():
            if not child.name.startswith("session-") or child.is_symlink() or not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
            except OSError:
                # A stale directory left behind cannot authorize a read: the
                # in-memory generation/handle registry starts empty.
                continue

    @staticmethod
    def _context(raw: bytes, start: int, end: int) -> str:
        """A bounded readable preview, even when the scan starts mid-codepoint."""
        while start < end and raw[start] & 0xC0 == 0x80:
            start += 1
        try:
            return raw[start:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                raise OutputStoreError("stale", "Saved output is no longer UTF-8.") from exc
            return raw[start:start + exc.start].decode("utf-8")

    def close(self) -> None:
        with self._lock:
            if self._cleanup_complete:
                return
            self._closed = True
            try:
                shutil.rmtree(self._directory)
            except FileNotFoundError:
                # A later generation may already have pruned an expired one.
                pass
            except OSError as exc:
                raise OutputStoreError("store_failed", "Could not remove output storage; cleanup can be retried.") from exc
            self._entries.clear()
            self._deadlines.clear()
            self._pending_cleanup.clear()
            self._used_bytes = 0
            self._cleanup_complete = True

    def __enter__(self) -> OutputStore:
        return self

    def __exit__(self, _kind: object, error: BaseException | None, _trace: object) -> None:
        try:
            self.close()
        except OutputStoreError:
            if error is None:
                raise
            error.add_note("Output cleanup failed; residual storage remains tracked for cleanup retry.")

    def _active(self) -> None:
        if self._closed:
            raise OutputStoreError("expired", "The output store has closed.")

    def _purge_expired(self) -> None:
        # Rollback remnants are never issued handles. Do not permit new writes
        # to forget their physical cost just because the initial save failed.
        for path, reserved in list(self._pending_cleanup.items()):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise OutputStoreError("store_failed", "Could not clean failed output snapshot.") from exc
            del self._pending_cleanup[path]
            self._used_bytes -= reserved
        now = self._clock()
        while self._deadlines and self._deadlines[0][0] <= now:
            _deadline, handle = self._deadlines[0]
            entry = self._entries.get(handle)
            if entry is None:
                heapq.heappop(self._deadlines)
                continue
            try:
                entry.path.unlink(missing_ok=True)
            except OSError as exc:
                raise OutputStoreError("store_failed", "Could not clean expired output snapshot.") from exc
            heapq.heappop(self._deadlines)
            del self._entries[handle]
            self._used_bytes -= entry.total_bytes

    def _rollback_snapshot(self, paths: tuple[Path, ...], error: OutputStoreError) -> None:
        """Keep the original save error and reserve any unremoved private file."""
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                try:
                    reserved = path.stat().st_size
                except OSError:
                    # All writes were quota checked before reaching the file.
                    # An unreadable size is reserved conservatively, not zero.
                    reserved = self._max_snapshot_bytes
                self._pending_cleanup[path] = reserved
                self._used_bytes += reserved
        if self._pending_cleanup:
            error.add_note("Output cleanup failed; residual files remain charged until a successful retry.")

    def _sign_handle(self, label: str, *parts: str) -> str:
        payload = "\0".join((label, self.generation, *parts)).encode("utf-8")
        return hmac.new(self._handle_secret, payload, hashlib.sha256).hexdigest()

    def save(self, text: str, *, owner_session: str, workspace: str, origin: str,
             segments: list[tuple[int, int, int]] | None = None,
             warning_filter: str = WARNING_FILTER_NOT_APPLICABLE,
             covers_filtered_all: bool | None = None) -> dict:
        """Atomically register the full, untruncated UTF-8 output."""
        if not owner_session or not origin:
            raise OutputStoreError("validation", "An owner and origin are required.")
        if (warning_filter not in WARNING_FILTER_MODES
                or (origin in {"rocq_query", "rocq_check", "rocq_step_multi"}
                    and (warning_filter == WARNING_FILTER_NOT_APPLICABLE
                         or covers_filtered_all is not True))
                or (warning_filter == WARNING_FILTER_NOT_APPLICABLE
                    and covers_filtered_all is not None)
                or (warning_filter != WARNING_FILTER_NOT_APPLICABLE
                    and covers_filtered_all is not True)):
            raise OutputStoreError("validation", "Snapshot warning filter/coverage is inconsistent.")
        ws = _disjoint_workspace(self._root, workspace)
        if not isinstance(text, str):
            raise OutputStoreError("validation", "Output must be UTF-8 text.")
        with self._lock:
            self._active()
            self._purge_expired()
            # Step P3: Publish only after a private, fully written file exists.
            nonce = secrets.token_urlsafe(24)
            owner_tag = self._sign_handle("owner", nonce, owner_session, str(ws))
            issued = self._sign_handle("issued", nonce, owner_tag)
            handle = f"{self.generation}.{nonce}.{issued}.{owner_tag}"
            final = self._directory / f"result-{nonce}"
            tmp = self._directory / f".part-{nonce}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            digest_state = hashlib.sha256()
            total = 0
            try:
                fd = os.open(tmp, flags, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    for start in range(0, len(text), _ENCODE_CHARS):
                        chunk = text[start:start + _ENCODE_CHARS].encode("utf-8")
                        total += len(chunk)
                        if total > self._max_snapshot_bytes or self._used_bytes + total > self._max_store_bytes:
                            raise OutputStoreError("quota_exceeded", "Output snapshot quota is full.")
                        stream.write(chunk)
                        digest_state.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if segments is not None:
                    last_index, last_end = -1, 0
                    for segment in segments:
                        if (not isinstance(segment, (tuple, list)) or len(segment) != 3
                                or any(type(value) is not int for value in segment)):
                            raise OutputStoreError("invalid_range", "Feedback segment index is invalid.")
                        index, start, stop = segment
                        if index <= last_index or start < last_end or stop < start or stop > total:
                            raise OutputStoreError("invalid_range", "Feedback spans overlap or exceed the source.")
                        last_index, last_end = index, stop
                os.replace(tmp, final)
                info = final.stat()
            except OutputStoreError as exc:
                self._rollback_snapshot((tmp, final), exc)
                raise
            except UnicodeError as exc:
                error = OutputStoreError("validation", "Output contains invalid Unicode.")
                self._rollback_snapshot((tmp, final), error)
                raise error from exc
            except OSError as exc:
                error = OutputStoreError("store_failed", "Could not save complete output.")
                self._rollback_snapshot((tmp, final), error)
                raise error from exc

            digest = digest_state.hexdigest()
            created = self._clock()
            self._entries[handle] = _Saved(
                path=final, owner=owner_session, workspace=ws, origin=origin,
                warning_filter=warning_filter, covers_filtered_all=covers_filtered_all,
                total_bytes=total, sha256=digest, created_at=created,
                device=info.st_dev, inode=info.st_ino, modified_ns=info.st_mtime_ns,
                changed_ns=info.st_ctime_ns,
                segments=tuple(segments) if segments is not None else None,
            )
            heapq.heappush(self._deadlines, (created + self._ttl_seconds, handle))
            self._used_bytes += total
            return {"handle": handle, "source_sha256": digest, "total_bytes": total,
                    "owner_generation": self.generation,
                    "warning_filter": warning_filter,
                    "covers_filtered_all": covers_filtered_all}

    def _lookup(self, handle: str, owner_session: str, workspace: str) -> _Saved:
        generation, nonce, issued, owner_tag = _handle_parts(handle)
        if generation != self.generation:
            raise OutputStoreError("expired", "This output handle is from another server generation.")
        # Authenticate the whole handle before comparing it with this caller.
        # Binding the owner tag into issued distinguishes a damaged tag from
        # an intact handle legitimately presented by the wrong owner.
        if not hmac.compare_digest(issued, self._sign_handle("issued", nonce, owner_tag)):
            raise OutputStoreError("not_found", "Output handle was never issued by this service.")
        ws = _disjoint_workspace(self._root, workspace)
        if not hmac.compare_digest(owner_tag, self._sign_handle("owner", nonce, owner_session, ws)):
            raise OutputStoreError("unauthorized", "Output belongs to a different session or workspace.")
        self._active()
        self._purge_expired()
        entry = self._entries.get(handle)
        if entry is None:
            raise OutputStoreError("expired", "This issued output handle has expired or been removed.")
        if owner_session != entry.owner or ws != entry.workspace:
            raise OutputStoreError("unauthorized", "Output belongs to a different session or workspace.")
        return entry

    def _open_saved(self, entry: _Saved):
        try:
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise OutputStoreError("expired", "Saved output was removed.") from exc
        except OSError as exc:
            raise OutputStoreError("stale", "Saved output cannot be opened.") from exc
        try:
            info = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            raise OutputStoreError("stale", "Saved output cannot be inspected.") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != entry.device
                or info.st_ino != entry.inode or info.st_size != entry.total_bytes
                or info.st_mtime_ns != entry.modified_ns
                or info.st_ctime_ns != entry.changed_ns):
            os.close(fd)
            raise OutputStoreError("stale", "Saved output changed after registration.")
        return os.fdopen(fd, "rb")

    def read(self, handle: str, *, owner_session: str, workspace: str,
             offset_bytes: int, max_bytes: int = MAX_READ_BYTES) -> dict:
        _strict_positive(max_bytes, MAX_READ_BYTES, "max_bytes")
        with self._lock:
            # Step P6: Check owner and generation before opening any saved bytes.
            entry = self._lookup(handle, owner_session, workspace)
            _strict_offset(offset_bytes, entry.total_bytes, "offset_bytes")
            with self._open_saved(entry) as stream:
                if offset_bytes == entry.total_bytes:
                    chunk = b""
                    status = RANGE_STATUS_EOF
                    end = offset_bytes
                else:
                    stream.seek(offset_bytes)
                    first = stream.read(1)
                    if not first:
                        raise OutputStoreError("stale", "Saved output changed during the read.")
                    if first[0] & 0xC0 == 0x80:
                        raise OutputStoreError("invalid_boundary", "Offset splits a UTF-8 character.")
                    stream.seek(offset_bytes)
                    raw = stream.read(min(max_bytes, entry.total_bytes - offset_bytes))
                    try:
                        raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        if exc.reason != "unexpected end of data":
                            raise OutputStoreError("stale", "Saved output is no longer UTF-8.") from exc
                        raw = raw[:exc.start]
                    if not raw:
                        raise OutputStoreError("invalid_range", "Range is too small for the next UTF-8 character.")
                    chunk = raw
                    status = RANGE_STATUS_DATA
                    end = offset_bytes + len(chunk)
                after = os.fstat(stream.fileno())
                if (after.st_size != entry.total_bytes or after.st_mtime_ns != entry.modified_ns
                        or after.st_ctime_ns != entry.changed_ns):
                    raise OutputStoreError("stale", "Saved output changed during the read.")
            return {
                "status": status, "start": offset_bytes, "end": end,
                "total_bytes": entry.total_bytes, "text": chunk.decode("utf-8"),
                "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
                "source_sha256": entry.sha256, "has_more": end < entry.total_bytes,
                "warning_filter": entry.warning_filter,
                "covers_filtered_all": entry.covers_filtered_all,
            }

    def find(self, handle: str, literal: str, *, owner_session: str,
             workspace: str, cursor: int = 0, max_hits: int = MAX_FIND_HITS) -> dict:
        _strict_positive(max_hits, MAX_FIND_HITS, "max_hits")
        if not isinstance(literal, str):
            raise OutputStoreError("invalid_range", "literal must be UTF-8 text.")
        try:
            needle = literal.encode("utf-8")
        except UnicodeError as exc:
            raise OutputStoreError("invalid_range", "literal is not UTF-8 text.") from exc
        if not 1 <= len(needle) <= MAX_LITERAL_BYTES:
            raise OutputStoreError("invalid_range", "literal must contain 1..256 UTF-8 bytes.")

        with self._lock:
            # Step P5: Never search a caller-provided path, only an owned handle.
            entry = self._lookup(handle, owner_session, workspace)
            _strict_offset(cursor, entry.total_bytes, "cursor")
            began = time.monotonic()
            with self._open_saved(entry) as stream:
                scan_end = min(entry.total_bytes, cursor + MAX_FIND_SCAN_BYTES)
                stream.seek(cursor)
                raw = stream.read(min(entry.total_bytes - cursor,
                                      scan_end - cursor + len(needle) - 1))
                hits = []
                preview_bytes = 0
                next_cursor = scan_end
                pos = 0
                while True:
                    if time.monotonic() - began > FIND_TIMEOUT_SECONDS:
                        raise OutputStoreError("timeout", "Literal search exceeded its time budget.")
                    match = raw.find(needle, pos)
                    if match < 0 or cursor + match >= scan_end:
                        break
                    absolute = cursor + match
                    left = max(0, match - _CONTEXT_BYTES)
                    right = min(len(raw), match + len(needle) + _CONTEXT_BYTES)
                    while right < len(raw) and raw[right] & 0xC0 == 0x80:
                        right -= 1
                    snippet = self._context(raw, left, right)
                    hit = {"offset_bytes": absolute, "length_bytes": len(needle),
                           "snippet": snippet}
                    hit_bytes = len(json.dumps(hit, ensure_ascii=True).encode("utf-8"))
                    if preview_bytes + hit_bytes > _MAX_FIND_PREVIEW_BYTES:
                        if not hits:
                            raise OutputStoreError("view_unavailable", "One match exceeds the preview budget.")
                        next_cursor = absolute  # This candidate belongs on the next page.
                        break
                    hits.append(hit)
                    preview_bytes += hit_bytes
                    pos = match + 1
                    while pos < len(raw) and raw[pos] & 0xC0 == 0x80:
                        pos += 1
                    if len(hits) == max_hits:
                        next_cursor = cursor + pos
                        break
                after = os.fstat(stream.fileno())
                if (after.st_size != entry.total_bytes or after.st_mtime_ns != entry.modified_ns
                        or after.st_ctime_ns != entry.changed_ns):
                    raise OutputStoreError("stale", "Saved output changed during the search.")
            result = {"hits": hits, "has_more": next_cursor < entry.total_bytes,
                      "cursor": cursor, "total_bytes": entry.total_bytes, "scope": "literal",
                      "source_sha256": entry.sha256,
                      "warning_filter": entry.warning_filter,
                      "covers_filtered_all": entry.covers_filtered_all}
            if result["has_more"]:
                result["next_cursor"] = next_cursor
            return result

    def list_segments(self, handle: str, *, owner_session: str, workspace: str,
                      cursor: int = 0, max_entries: int = 20) -> dict:
        _strict_positive(max_entries, 20, "max_entries")
        with self._lock:
            entry = self._lookup(handle, owner_session, workspace)
            if entry.segments is None:
                raise OutputStoreError("validation", "This output has no feedback segment index.")
            _strict_offset(cursor, len(entry.segments), "cursor")
            with self._open_saved(entry):
                pass
            end = min(len(entry.segments), cursor + max_entries)
            result = {
                "cursor": cursor,
                "items": [{"index": index, "span_start": start, "span_end": stop}
                          for index, start, stop in entry.segments[cursor:end]],
                "has_more": end < len(entry.segments),
                "total_entries": len(entry.segments),
                "total_bytes": entry.total_bytes,
                "source_sha256": entry.sha256,
                "warning_filter": entry.warning_filter,
                "covers_filtered_all": entry.covers_filtered_all,
            }
            if result["has_more"]:
                result["next_cursor"] = end
            return result
