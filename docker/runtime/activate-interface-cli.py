#!/usr/bin/env python3
"""Atomically activate an image-owned Interface CLI for one Silicon.

Docker runtime images already contain the exact, checksum-verified Interface
package selected by the Stemcell release.  Persisting another package copy
under ``.silicon-interface`` makes upgrades non-atomic and prevents an image
rollback from selecting its own older CLI.  Instead, this helper atomically
writes per-instance launchers that export the durable instance root and execute
the selected image's absolute CLI path.

The launcher path is stable across runtime generations.  Once an instance has
booted one fixed image, even a rollback to the immediately preceding pre-fix
image selects that image's bundled CLI without requiring the old entrypoint to
understand the new activation scheme.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


VERSION_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)(?!\d)"
)
COMMAND_TIMEOUT_SECONDS = 30


class ActivationError(RuntimeError):
    """The selected runtime CLI could not be activated safely."""


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_real_directory(path: Path, *, mode: int = 0o700) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=mode)
        _fsync_directory(path.parent)
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ActivationError(f"unsafe Interface activation directory: {path}")


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _version(command: Path, *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            [str(command), "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ActivationError(
            f"could not execute Silicon Interface CLI: {exc}"
        ) from exc
    output = "\n".join(
        value.strip()
        for value in (result.stdout, result.stderr)
        if value.strip()
    )
    match = VERSION_RE.search(output)
    if result.returncode != 0 or match is None:
        detail = output or f"exited with {result.returncode}"
        raise ActivationError(f"invalid Silicon Interface CLI: {detail}")
    return match.group(1)


def _launcher(runtime_executable: Path) -> bytes:
    executable = str(runtime_executable)
    if "\n" in executable or "'" in executable:
        raise ActivationError("runtime Interface CLI path is unsafe")
    return f"""#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
export SILICON_INTERFACE_ROOT="$ROOT"
exec '{executable}' "$@"
""".encode("utf-8")


def _activate_locked(
    root: Path,
    runtime_executable: Path,
) -> dict[str, str]:
    interface_root = root / ".silicon-interface"
    binaries = interface_root / "bin"
    _ensure_real_directory(binaries)

    expected_version = _version(runtime_executable, cwd=root)
    launcher = _launcher(runtime_executable)

    # Replace the secondary spelling first. The canonical `si` path used by
    # Stemcell changes only after every prerequisite is durable. Each rename is
    # atomic, and both the old and new launcher remain independently runnable.
    _atomic_write(
        binaries / "silicon-interface",
        launcher,
        mode=0o755,
    )
    _atomic_write(binaries / "si", launcher, mode=0o755)

    for name in ("silicon-interface", "si"):
        actual = _version(binaries / name, cwd=root)
        if actual != expected_version:
            raise ActivationError(
                f"activated {name} does not match the runtime image"
            )
    return {
        "version": expected_version,
        "runtime_executable": str(runtime_executable),
    }


def activate(
    root: Path,
    runtime_executable: Path,
) -> dict[str, str]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ActivationError(f"Silicon root is not a directory: {root}")
    if not runtime_executable.is_absolute():
        raise ActivationError("runtime Interface CLI path must be absolute")
    resolved_executable = runtime_executable.resolve(strict=True)
    if not resolved_executable.is_file() or not os.access(
        resolved_executable, os.X_OK
    ):
        raise ActivationError(
            "runtime Silicon Interface CLI is not executable: "
            f"{runtime_executable}"
        )

    interface_root = root / ".silicon-interface"
    _ensure_real_directory(interface_root)
    lock_path = interface_root / ".activation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _activate_locked(root, runtime_executable)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically activate the runtime's Silicon Interface CLI"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--executable", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = activate(arguments.root, arguments.executable)
    except (ActivationError, OSError) as exc:
        print(f"Silicon Interface activation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
