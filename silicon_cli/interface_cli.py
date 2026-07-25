"""Install the Silicon Interface CLI into a silicon folder."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import runtime_contract, ui
from .config import (
    SILICON_INTERFACE_DAEMON_SKIP,
    SILICON_INTERFACE_CLI_PACKAGE,
    SILICON_INTERFACE_CLI_SKIP,
    SILICON_INTERFACE_CLI_SOURCE,
    SILICON_INTERFACE_CLI_TARBALL,
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


def _stop_daemon(target: Path) -> None:
    si = target / ".silicon-interface" / "bin" / "si"
    if si.exists():
        _run([str(si), "daemon", "stop"], target, warn=False)


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
) -> bool:
    """Install local si/silicon-interface wrappers into ``target``.

    Normal hydration remains best-effort. Transactional pulls pass
    ``required=True`` so a claim cannot commit without a working Interface CLI.
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
            _start_daemon(target_path)
        return True

    if force:
        _stop_daemon(target_path)

    script = _source_script()
    ui.info("Setting up Silicon Interface CLI...")
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
            _start_daemon(target_path)
        return True
    if required:
        raise RuntimeError(
            "Silicon Interface CLI installation failed. Check npm registry "
            "access or set SILICON_INTERFACE_CLI_SOURCE, then rerun the same "
            "pull."
        )
    return ok
