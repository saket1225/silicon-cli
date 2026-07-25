"""The per-silicon Glass agent (glass_agent.py) — remote control / backups.

Only relevant when the silicon dir has a .glass.json. Tracked via .glass_agent.pid.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import ui
from .config import (
    active_release_root,
    legacy_offline_update_fenced,
    python_run_cmd,
    runtime_environment,
)
from .updater.io import atomic_write_bytes, atomic_write_json

_PID_MAX_BYTES = 32
_IDENTITY_MAX_BYTES = 16 * 1024
_LAUNCH_GATE = (
    "import os, sys\n"
    "ready = sys.stdin.buffer.readline()\n"
    "if ready != b'GO\\n':\n"
    "    raise SystemExit(75)\n"
    "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n"
)


def _pid_file(path: str) -> Path:
    return Path(path) / ".glass_agent.pid"


def _identity_file(path: str) -> Path:
    return Path(path) / ".glass_agent.pid.meta.json"


def _read_pid(path: str) -> int | None:
    """Read a small, regular PID file without ever following a symlink."""

    target = _pid_file(path)
    descriptor = -1
    try:
        before = target.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _PID_MAX_BYTES
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
            before, opened
        ):
            return None
        payload = os.read(descriptor, _PID_MAX_BYTES + 1)
        if len(payload) > _PID_MAX_BYTES:
            return None
        value = payload.decode("ascii").strip()
        return int(value) if value.isdigit() and int(value) > 0 else None
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def pid(path: str) -> int | None:
    """Return the safely parsed Glass-agent PID for display/diagnostics."""

    return _read_pid(path)


def _validated_control_path(path: str, name: str) -> Path:
    try:
        root = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Glass agent instance root does not exist") from exc
    if not root.is_dir():
        raise RuntimeError("Glass agent instance root is not a directory")
    target = root / name
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Glass agent control path is unsafe: {target}")
    return target


def _publish_pid(path: str, process_id: int) -> None:
    target = _validated_control_path(path, ".glass_agent.pid")
    atomic_write_bytes(target, f"{process_id}\n".encode("ascii"), mode=0o600)


def _publish_identity(path: str, value: dict) -> None:
    target = _validated_control_path(path, ".glass_agent.pid.meta.json")
    atomic_write_json(target, value, mode=0o600)


def _remove_control_files(path: str) -> None:
    # unlink() removes a link itself rather than following it.  A non-file
    # entry is left in place so the next start fails closed.
    for target in (_pid_file(path), _identity_file(path)):
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass


def _read_identity(path: str) -> dict | None:
    target = _identity_file(path)
    descriptor = -1
    try:
        before = target.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _IDENTITY_MAX_BYTES
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
            before, opened
        ):
            return None
        payload = os.read(descriptor, _IDENTITY_MAX_BYTES + 1)
        if len(payload) > _IDENTITY_MAX_BYTES:
            return None
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _identity(pid: int) -> str:
    # Imported lazily because process.py owns supervision and imports this
    # module for sidecar lifecycle operations.
    from .process import _process_identity

    return _process_identity(pid)


def _legacy_agent_matches(path: str, pid: int) -> bool:
    if not _alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    script = str((active_release_root(path) / "glass_agent.py").resolve())
    return result.returncode == 0 and script in result.stdout


def status(path: str) -> bool:
    process_id = _read_pid(path)
    if not process_id:
        return False
    metadata = _identity_file(path)
    if not metadata.exists() and not metadata.is_symlink():
        return _legacy_agent_matches(path, process_id)
    try:
        value = _read_identity(path)
        if value is None:
            return False
        return bool(
            value.get("schema") == 1
            and int(value["pid"]) == process_id
            and str(value["identity"]) == _identity(process_id)
            and bool(value["identity"])
        )
    except (ValueError, KeyError, TypeError):
        return False


def _terminate_spawned(proc: subprocess.Popen) -> None:
    stream = getattr(proc, "stdin", None)
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _spawn(path: str, log) -> tuple[subprocess.Popen, bool]:
    agent_script = active_release_root(path) / "glass_agent.py"
    command = [python_run_cmd(path), "-u", str(agent_script)]
    gated = os.name != "nt"
    if gated:
        # The launcher cannot exec the real sidecar until both identity files
        # are durable. If this CLI dies first, pipe EOF makes it exit instead
        # of leaving an undiscoverable orphan.
        command = [sys.executable, "-c", _LAUNCH_GATE, *command]
    proc = subprocess.Popen(
        command,
        cwd=path,
        env=runtime_environment(path),
        stdin=subprocess.PIPE if gated else subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, gated


def start(path: str) -> None:
    if not (Path(path) / ".glass.json").exists():
        return
    if legacy_offline_update_fenced(path):
        ui.error(
            "Glass agent start is fenced while a legacy offline update is in "
            "progress; run `silicon update resume` first."
        )
        return
    if status(path):
        return
    # Validate both publication targets before launching anything.  In
    # particular, never "clean up" an attacker-controlled symlink and then
    # silently continue.
    _validated_control_path(path, ".glass_agent.pid")
    _validated_control_path(path, ".glass_agent.pid.meta.json")
    _remove_control_files(path)
    log_path = _validated_control_path(path, ".glass_agent.log")
    log = open(log_path, "a", encoding="utf-8")
    proc = None
    try:
        proc, gated = _spawn(path, log)
    finally:
        log.close()
    try:
        identity = _identity(proc.pid)
        if not identity:
            raise RuntimeError("could not establish Glass agent process identity")
        # Metadata is published first and the compatibility PID file last.
        # status() therefore cannot observe a new PID without its birth
        # identity. Both paths are atomic, mode 0600, and directory-fsynced.
        _publish_identity(
            path,
            {
                "schema": 1,
                "pid": proc.pid,
                "identity": identity,
                "created_at": time.time(),
            },
        )
        _publish_pid(path, proc.pid)
        if gated:
            if proc.stdin is None:
                raise RuntimeError("Glass agent launch gate is unavailable")
            proc.stdin.write(b"GO\n")
            proc.stdin.flush()
            proc.stdin.close()
    except BaseException:
        _remove_control_files(path)
        _terminate_spawned(proc)
        raise
    ui.info(f"Glass agent started (PID {proc.pid})")


def stop(path: str) -> None:
    process_id = _read_pid(path)
    if process_id and status(path):
        try:
            os.kill(process_id, signal.SIGTERM)
            time.sleep(1)
            if _alive(process_id):
                os.kill(process_id, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        ui.info("Glass agent stopped")
    _remove_control_files(path)
