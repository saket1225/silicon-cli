#!/usr/bin/env python3
"""Bounded, body-free health probe for one Docker Silicon runtime."""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import time
from pathlib import Path


ROOT = Path(os.environ.get("SILICON_ROOT") or "/silicon").resolve()
MAX_HEARTBEAT_AGE = float(os.environ.get("SILICON_HEALTH_MAX_HEARTBEAT_AGE") or 30)
MAX_JSON_BYTES = 16 * 1024


def read_json(path: Path) -> dict:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
        raise ValueError(f"unsafe health file: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid health object: {path.name}")
    return value


def read_pid(path: Path) -> int:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128:
        raise ValueError(f"unsafe pid file: {path.name}")
    pid = int(path.read_text(encoding="utf-8").strip())
    if pid <= 0:
        raise ValueError(f"invalid pid: {path.name}")
    os.kill(pid, 0)
    return pid


def active_release() -> Path:
    pointer = read_json(ROOT / ".silicon" / "current.json")
    release = Path(str(pointer.get("release_path") or ""))
    if not release.is_absolute():
        release = ROOT / release
    release = release.resolve(strict=True)
    releases = (ROOT / ".silicon" / "releases").resolve(strict=True)
    if (
        pointer.get("kind") != "immutable-release"
        or releases not in release.parents
        or not (release / "main.py").is_file()
    ):
        raise ValueError("invalid active generation")
    return release


def check_main() -> None:
    supervisor = read_pid(ROOT / ".silicon.pid")
    metadata = read_json(ROOT / ".silicon.pid.meta.json")
    child = int(metadata["child_pid"])
    os.kill(child, 0)
    generation = Path(str(metadata["generation"])).resolve(strict=True)
    if (
        metadata.get("schema") != 1
        or int(metadata["supervisor_pid"]) != supervisor
        or generation != active_release()
    ):
        raise ValueError("main process metadata is stale")
    health = read_json(ROOT / ".silicon" / "runtime-health.json")
    heartbeat = float(health["heartbeat_at"])
    ready_at = float(health["ready_at"])
    age = time.time() - heartbeat
    if (
        health.get("schema") != 1
        or health.get("ready") is not True
        or int(health["pid"]) != child
        or Path(str(health["code_root"])).resolve(strict=True) != generation
        or ready_at > heartbeat
        or not 0 <= age <= MAX_HEARTBEAT_AGE
    ):
        raise ValueError("Stemcell heartbeat is stale")


def rpc_socket_path(state: Path) -> Path:
    discovery = state / "daemon-rpc.json"
    if discovery.exists():
        value = read_json(discovery)
        if value.get("version") == 1 and value.get("socket"):
            path = Path(str(value["socket"]))
            if path.is_absolute():
                return path
    return state / "daemon.sock"


def check_interface() -> None:
    state = ROOT / ".silicon-interface"
    daemon_pid = read_pid(state / "daemon.pid")
    rpc_path = rpc_socket_path(state)
    if not stat.S_ISSOCK(rpc_path.lstat().st_mode):
        raise ValueError("Interface RPC socket is unavailable")
    request_id = secrets.token_hex(8)
    request = json.dumps(
        {
            "version": 1,
            "id": request_id,
            "command": "daemon",
            "args": ["status"],
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(rpc_path))
        connection.sendall(request)
        response = b""
        while b"\n" not in response and len(response) <= MAX_JSON_BYTES:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
    value = json.loads(response.split(b"\n", 1)[0])
    result = value.get("result") if isinstance(value, dict) else None
    if (
        value.get("version") != 1
        or value.get("id") != request_id
        or value.get("ok") is not True
        or not isinstance(result, dict)
        or result.get("running") is not True
        or int(result.get("pid") or 0) != daemon_pid
    ):
        raise ValueError("Interface daemon RPC is unhealthy")


def check_optional_glass_agent() -> None:
    pid_path = ROOT / ".glass_agent.pid"
    metadata_path = ROOT / ".glass_agent.pid.meta.json"
    if pid_path.exists() or metadata_path.exists():
        read_pid(pid_path)


def main() -> int:
    try:
        check_main()
        check_interface()
        check_optional_glass_agent()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
