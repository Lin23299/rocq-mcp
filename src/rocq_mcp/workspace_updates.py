"""Small CCV update notifications; native Pet still owns environment refresh.

No dependency graph, polling daemon or printed-output cache lives here.
Writers must follow CCV's single-writer / stopped-worker update discipline.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


class WorkspaceUpdateError(Exception):
    pass


def signal_path(workspace: str) -> Path:
    root = Path(os.environ.get("ROCQ_WORKSPACE_UPDATE_ROOT", str(
        Path.home() / ".local/share/rocq-mcp/workspace-updates"))).expanduser().resolve()
    ws = Path(workspace).resolve(strict=True)
    if not ws.is_dir() or root.is_relative_to(ws) or ws.is_relative_to(root):
        raise WorkspaceUpdateError("Update signals must be outside the proof workspace.")
    return root / (hashlib.sha256(str(ws).encode()).hexdigest() + ".json")


def read_signal(workspace: str) -> dict:
    """Read only a bounded record; no mkdir, directory scan or Pet operation."""
    ws = str(Path(workspace).resolve(strict=True))
    path = signal_path(ws)
    try:
        if path.is_symlink():
            raise WorkspaceUpdateError("Update signal cannot be a symlink.")
        with path.open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return {"schema": 1, "workspace": ws, "token": "initial", "updating": False}
    except OSError as exc:
        raise WorkspaceUpdateError("Cannot read workspace update signal.") from exc
    try:
        data = json.loads(raw)
        if (len(raw) > 4096 or not isinstance(data, dict) or data.get("schema") != 1
                or data.get("workspace") != ws or type(data.get("updating")) is not bool
                or not isinstance(data.get("token"), str) or not 1 <= len(data["token"]) <= 64):
            raise ValueError("invalid update record")
    except (ValueError, UnicodeError) as exc:
        raise WorkspaceUpdateError("Malformed workspace update signal.") from exc
    return data


def publish(workspace: str, token: str, *, updating: bool) -> None:
    ws = str(Path(workspace).resolve(strict=True))
    path = signal_path(ws)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".update-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema": 1, "workspace": ws, "token": token, "updating": updating}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise WorkspaceUpdateError("Cannot publish workspace update; do not resume old states.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def artifact_snapshot(workspace: str) -> dict:
    """Metadata scan ONLY at a compiler/copy boundary, including partial failures."""
    ws = Path(workspace).resolve(strict=True)
    result = {}
    def onerror(exc):
        raise exc
    try:
        for base, _dirs, files in os.walk(ws, followlinks=False, onerror=onerror):
            for name in files:
                path = Path(base) / name
                if path.suffix not in {".vo", ".vos"} and name not in {"_CoqProject", "_RocqProject"}:
                    continue
                info = path.stat()
                result[str(path.relative_to(ws))] = (hashlib.sha256(path.read_bytes()).hexdigest()
                    if name in {"_CoqProject", "_RocqProject"}
                    else (info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    except OSError as exc:
        raise WorkspaceUpdateError("Cannot inspect build outputs for environment notification.") from exc
    return result


def run_update(workspace: str, command: list[str], *, recover: bool = False) -> int:
    """Preserve command rc; publish even after a failed/timeout partial build.

    --recover is explicit acknowledgement that the previous writer stopped.
    It invalidates unconditionally, never restores the old token after a crash.
    """
    if not command:
        raise WorkspaceUpdateError("An update command is required.")
    ws = str(Path(workspace).resolve(strict=True))
    before = read_signal(ws)
    if before["updating"] and not recover:
        raise WorkspaceUpdateError("An update is pending; stop its writer before explicit recovery.")
    snapshot = artifact_snapshot(ws)
    publish(ws, before["token"], updating=True)
    env = dict(os.environ, CCV_MCP_UPDATE_ACTIVE=ws)
    try:
        return subprocess.run(command, env=env).returncode
    finally:
        # If observation fails, leave pending and fail closed instead of
        # publishing a guessed no-op. A killed wrapper likewise leaves pending.
        after = artifact_snapshot(ws)
        token = uuid.uuid4().hex if recover or after != snapshot else before["token"]
        publish(ws, token, updating=False)
