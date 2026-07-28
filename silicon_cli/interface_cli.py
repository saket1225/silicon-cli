"""Install the Silicon Interface CLI into a silicon folder."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from . import runtime_contract, ui
from .config import (
    SILICON_INTERFACE_CLI_PACKAGE,
    SILICON_INTERFACE_CLI_SKIP,
    SILICON_INTERFACE_CLI_SOURCE,
    SILICON_INTERFACE_CLI_TARBALL,
    SILICON_INTERFACE_DAEMON_SKIP,
)


def _node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except Exception:
        return None
    m = re.match(r"^v?(\d+)", out)
    return int(m.group(1)) if m else None


def _source_script() -> Path | None:
    if SILICON_INTERFACE_CLI_SOURCE:
        source = Path(SILICON_INTERFACE_CLI_SOURCE).expanduser().resolve()
        if source.is_file():
            return source
        candidate = source / "bin" / "silicon-interface.mjs"
        if candidate.exists():
            return candidate
        return None

    # Local dev layout: ../silicon-interface/packages/silicon-interface-cli
    repo_root = Path(__file__).resolve().parents[2]
    candidate = (
        repo_root
        / "silicon-interface"
        / "packages"
        / "silicon-interface-cli"
        / "bin"
        / "silicon-interface.mjs"
    )
    return candidate if candidate.exists() else None


def _run(cmd: list[str], target: Path, *, warn: bool = True) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as exc:
        if warn:
            ui.warn(f"Silicon Interface CLI setup skipped: {exc}")
        return False
    if proc.returncode == 0:
        return True
    if not warn:
        return False
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    suffix = f": {detail[-1]}" if detail else ""
    ui.warn(f"Silicon Interface CLI setup skipped{suffix}")
    return False


def _npm_install_command(
    target: Path,
    package_spec: str,
    *,
    start_daemon: bool,
) -> list[str] | None:
    npm = shutil.which("npm")
    if not npm:
        return None
    command = [
        npm,
        "exec",
        "--yes",
        "--package",
        package_spec,
        "--",
        "silicon-interface",
        "install",
        str(target),
    ]
    if not start_daemon:
        command.append("--no-daemon")
    return command


def _npm_install_commands(
    target: Path,
    *,
    start_daemon: bool,
) -> list[list[str]]:
    package_specs = [SILICON_INTERFACE_CLI_PACKAGE]
    if (
        SILICON_INTERFACE_CLI_TARBALL
        and SILICON_INTERFACE_CLI_TARBALL not in package_specs
    ):
        package_specs.append(SILICON_INTERFACE_CLI_TARBALL)

    commands: list[list[str]] = []
    for package_spec in package_specs:
        cmd = _npm_install_command(
            target,
            package_spec,
            start_daemon=start_daemon,
        )
        if cmd:
            commands.append(cmd)
    return commands


def _start_daemon(target: Path) -> bool:
    if SILICON_INTERFACE_DAEMON_SKIP:
        return False
    if not (target / ".glass.json").exists():
        return False
    si = target / ".silicon-interface" / "bin" / "si"
    if not si.exists():
        return False
    ok = _run([str(si), "daemon", "start"], target, warn=False)
    if ok:
        ui.success("Silicon Interface daemon running.")
    else:
        ui.warn("Silicon Interface daemon was not started; run '.silicon-interface/bin/si daemon start'.")
    return ok


def daemon_required(target: str | Path) -> bool:
    """Return whether this Glass-managed instance requires live delivery."""

    return bool(
        not SILICON_INTERFACE_DAEMON_SKIP
        and (Path(target).resolve() / ".glass.json").exists()
    )


def _daemon_pid(target: Path) -> int | None:
    path = target / ".silicon-interface" / "daemon.pid"
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > 32
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
            before, opened
        ):
            return None
        payload = os.read(descriptor, 33)
        if len(payload) > 32:
            return None
        value = payload.decode("ascii").strip()
        pid = int(value)
        return pid if value.isdigit() and pid > 0 else None
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def daemon_running(target: str | Path) -> bool:
    """Return whether the instance's recorded Interface daemon is alive."""

    pid = _daemon_pid(Path(target).resolve())
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


def _start_daemon_checked(target: Path, *, required: bool) -> bool:
    ok = _start_daemon(target)
    if required and not ok:
        raise RuntimeError(
            "Silicon Interface daemon is required but could not be started"
        )
    return ok


def start_daemon(
    target: str | Path,
    *,
    required: bool = False,
) -> bool:
    """Start this instance's Interface daemon, optionally failing closed."""

    return _start_daemon_checked(Path(target).resolve(), required=required)


def _stop_daemon(target: Path) -> bool:
    si = target / ".silicon-interface" / "bin" / "si"
    if si.exists():
        return _run([str(si), "daemon", "stop"], target, warn=False)
    return not (target / ".silicon-interface" / "daemon.pid").exists()


def stop_daemon(
    target: str | Path,
    *,
    required: bool = False,
) -> bool:
    """Stop this instance's Interface daemon.

    Transactional full stops use ``required=True`` so protected Interface
    state is never checkpointed or restored while its listener may still be
    writing to it.
    """

    target_path = Path(target).resolve()
    ok = _stop_daemon(target_path)
    if required and not ok:
        raise RuntimeError(
            "Silicon Interface daemon could not be stopped safely"
        )
    return ok


def _unavailable(message: str, *, required: bool) -> bool:
    if required:
        raise RuntimeError(f"Silicon Interface CLI is required: {message}")
    ui.warn(f"Silicon Interface CLI setup skipped: {message}")
    return False


def _installation_ready(target: Path) -> bool:
    try:
        runtime_contract.verify_local_interface_install(target)
    except RuntimeError:
        return False
    return True


def setup(
    target: str | Path,
    *,
    required: bool = False,
    start_daemon: bool = True,
    force: bool = False,
    source_script: str | Path | None = None,
) -> bool:
    """Install local si/silicon-interface wrappers into ``target``.

    Normal hydration remains best-effort. Transactional pulls pass
    ``required=True`` so a claim cannot commit without a working Interface CLI.
    Package updates pass their checksum-verified global package script so
    per-instance installation cannot perform a second unverified download.
    """
    if SILICON_INTERFACE_CLI_SKIP:
        return _unavailable(
            "setup was disabled by SILICON_INTERFACE_CLI_SKIP",
            required=required,
        )

    target_path = Path(target).resolve()
    major = _node_major()
    if major is None:
        return _unavailable("node was not found", required=required)
    if major < 22:
        return _unavailable(
            f"Node 22+ is required (found Node {major})",
            required=required,
        )

    if not force and _installation_ready(target_path):
        ui.success(
            "Silicon Interface CLI ready: "
            f"{target_path}/.silicon-interface/bin/si"
        )
        if start_daemon:
            _start_daemon_checked(
                target_path,
                required=required and daemon_required(target_path),
            )
        return True

    if force:
        stop_daemon(target_path, required=True)

    ui.info("Setting up Silicon Interface CLI...")
    if source_script is not None:
        command = [
            shutil.which("node") or "node",
            str(source_script),
            "install",
            str(target_path),
        ]
        if not start_daemon:
            command.append("--no-daemon")
        ok = _run(command, target_path)
    else:
        script = _source_script()
        if script:
            command = [
                shutil.which("node") or "node",
                str(script),
                "install",
                str(target_path),
            ]
            if not start_daemon:
                command.append("--no-daemon")
            ok = _run(command, target_path)
        else:
            commands = _npm_install_commands(
                target_path,
                start_daemon=start_daemon,
            )
            if not commands:
                return _unavailable("npm was not found", required=required)
            ok = False
            for index, cmd in enumerate(commands):
                final_attempt = index == len(commands) - 1
                ok = _run(cmd, target_path, warn=final_attempt)
                if ok:
                    break
                if not final_attempt:
                    ui.warn(
                        "Silicon Interface CLI package lookup failed; "
                        "retrying with published tarball."
                    )

    if ok:
        try:
            runtime_contract.verify_local_interface_install(target_path)
        except RuntimeError as exc:
            if required:
                raise
            ui.warn(str(exc))
            return False
        ui.success(
            "Silicon Interface CLI ready: "
            f"{target_path}/.silicon-interface/bin/si"
        )
        if start_daemon:
            _start_daemon_checked(
                target_path,
                required=required and daemon_required(target_path),
            )
        return True
    if required:
        raise RuntimeError(
            "Silicon Interface CLI installation failed. Check npm registry "
            "access or set SILICON_INTERFACE_CLI_SOURCE, then rerun the same "
            "pull."
        )
    return ok
