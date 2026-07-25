"""Runtime dependency contracts enforced before a Silicon pull commits."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


MINIMUM_PYTHON_PACKAGES = {
    "silicon-cli": "1.0.22",
    "silicon-browser": "1.0.8",
    "silicon-extend": "0.1.1",
}
EXACT_PYTHON_PACKAGES = {
    "silicon-extend": "0.1.1",
}
MINIMUM_COMMAND_VERSIONS = {
    "silicon-browser": "1.0.8",
    "silicon-extend": "0.1.1",
    "silicon-interface": "2.0.1",
    "claude": "2.1.219",
    "codex": "0.145.0",
    "node": "22.0.0",
}
REQUIRED_COMMANDS = (
    "silicon",
    "silicon-browser",
    "silicon-extend",
    "silicon-interface",
    "si",
    "claude",
    "codex",
    "node",
    "npm",
    "python3",
    "git",
)
VERSION_ARGUMENTS = {
    "silicon-browser": ["--version"],
    "silicon-extend": ["--version"],
    "silicon-interface": ["--version"],
    "claude": ["--version"],
    "codex": ["--version"],
    "node": ["--version"],
    "npm": ["--version"],
    "python3": ["--version"],
    "git": ["--version"],
}

HOST_BASE_COMMANDS = (
    "silicon-browser",
    "silicon-extend",
    "node",
    "npm",
    "python3",
    "git",
)
HOST_BASE_MINIMUMS = {
    "silicon-browser": MINIMUM_COMMAND_VERSIONS["silicon-browser"],
    "silicon-extend": MINIMUM_COMMAND_VERSIONS["silicon-extend"],
    "node": MINIMUM_COMMAND_VERSIONS["node"],
}


def _numeric_version(value: str) -> tuple[int, ...] | None:
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){0,3})(?!\d)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _version_at_least(actual: str, minimum: str) -> bool:
    actual_parts = _numeric_version(actual)
    minimum_parts = _numeric_version(minimum)
    if actual_parts is None or minimum_parts is None:
        return False
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= (
        minimum_parts + (0,) * (width - len(minimum_parts))
    )


def version_at_least(actual: str, minimum: str) -> bool:
    """Return whether ``actual`` satisfies the numeric minimum version."""

    return _version_at_least(actual, minimum)


def _run_version(command: str) -> tuple[bool, str]:
    executable = shutil.which(command)
    if not executable:
        return False, "not found on PATH"
    arguments = VERSION_ARGUMENTS.get(command)
    if not arguments:
        return True, executable
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    if result.returncode != 0:
        return False, output or f"exited with {result.returncode}"
    return True, output


def _format_host_failure(failures: list[str]) -> RuntimeError:
    joined = "; ".join(failures)
    return RuntimeError(
        "host-local pull prerequisites are incomplete: "
        f"{joined}. The default Docker runtime pulls these tools automatically. "
        "Otherwise update the host with Node 22+, Git, "
        "`python3 -m pip install --upgrade silicon-browser silicon-extend`, "
        "and the selected Claude Code/Codex CLI, then rerun the same pull."
    )


def verify_host_pull_runtime() -> None:
    """Verify the shared host tools required by the explicit local mode."""

    failures: list[str] = []
    for command in HOST_BASE_COMMANDS:
        ok, output = _run_version(command)
        if not ok:
            failures.append(f"{command}: {output}")
            continue
        minimum = HOST_BASE_MINIMUMS.get(command)
        if minimum and not _version_at_least(output, minimum):
            failures.append(
                f"{command}: found {output!r}, require {minimum} or newer"
            )
    if failures:
        raise _format_host_failure(failures)


def verify_host_providers(providers: set[str]) -> None:
    """Verify only the model CLIs selected by the pulled Silicon configs."""

    failures: list[str] = []
    for provider in sorted(providers & {"claude", "codex"}):
        ok, output = _run_version(provider)
        if not ok:
            failures.append(f"{provider}: {output}")
            continue
        minimum = MINIMUM_COMMAND_VERSIONS[provider]
        if not _version_at_least(output, minimum):
            failures.append(
                f"{provider}: found {output!r}, require {minimum} or newer"
            )
    if failures:
        raise _format_host_failure(failures)


def verify_local_interface_install(target: str | Path) -> None:
    """Require both per-instance Interface CLI wrappers to be functional."""

    root = Path(target).resolve()
    failures: list[str] = []
    for name in ("si", "silicon-interface"):
        command = root / ".silicon-interface" / "bin" / name
        if not command.is_file():
            failures.append(f"{name}: wrapper is missing")
            continue
        try:
            result = subprocess.run(
                [str(command), "--version"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )
        if result.returncode != 0:
            failures.append(
                f"{name}: {output or f'exited with {result.returncode}'}"
            )
        elif not _version_at_least(
            output, MINIMUM_COMMAND_VERSIONS["silicon-interface"]
        ):
            failures.append(
                f"{name}: found {output!r}, require "
                f"{MINIMUM_COMMAND_VERSIONS['silicon-interface']} or newer"
            )
    if failures:
        raise RuntimeError(
            "Silicon Interface CLI installation is incomplete: "
            + "; ".join(failures)
        )


def docker_contract() -> dict[str, object]:
    return {
        "commands": list(REQUIRED_COMMANDS),
        "version_arguments": VERSION_ARGUMENTS,
        "minimum_command_versions": MINIMUM_COMMAND_VERSIONS,
        "minimum_python_packages": MINIMUM_PYTHON_PACKAGES,
        "exact_python_packages": EXACT_PYTHON_PACKAGES,
        "minimum_python": [3, 10],
    }


def docker_contract_json() -> str:
    return json.dumps(docker_contract(), sort_keys=True, separators=(",", ":"))


DOCKER_PROBE_SCRIPT = r"""
import json
import re
import shutil
import subprocess
import sys
from importlib import metadata

contract = json.loads(sys.argv[1])
failures = []
versions = {}

def numeric(value):
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){0,3})(?!\d)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None

def at_least(actual, minimum):
    actual_parts = numeric(actual)
    minimum_parts = numeric(minimum)
    if actual_parts is None or minimum_parts is None:
        return False
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= (
        minimum_parts + (0,) * (width - len(minimum_parts))
    )

if list(sys.version_info[:2]) < contract["minimum_python"]:
    failures.append(
        "python: found %s, require %s+"
        % (
            ".".join(str(part) for part in sys.version_info[:3]),
            ".".join(str(part) for part in contract["minimum_python"]),
        )
    )

for package, minimum in contract["minimum_python_packages"].items():
    try:
        installed = metadata.version(package)
    except metadata.PackageNotFoundError:
        failures.append("%s: Python package is missing" % package)
        continue
    versions[package] = installed
    if not at_least(installed, minimum):
        failures.append(
            "%s: found %s, require %s or newer" % (package, installed, minimum)
        )

for package, expected in contract["exact_python_packages"].items():
    installed = versions.get(package)
    if installed is not None and installed != expected:
        failures.append(
            "%s: found %s, require exactly %s" % (package, installed, expected)
        )

for command in contract["commands"]:
    executable = shutil.which(command)
    if not executable:
        failures.append("%s: command is missing" % command)
        continue
    versions.setdefault(command, executable)
    arguments = contract["version_arguments"].get(command)
    if not arguments:
        continue
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except Exception as exc:
        failures.append("%s: %s" % (command, exc))
        continue
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    if result.returncode != 0:
        failures.append(
            "%s: %s"
            % (command, output or "exited with %s" % result.returncode)
        )
        continue
    versions[command] = output
    minimum = contract["minimum_command_versions"].get(command)
    if minimum and not at_least(output, minimum):
        failures.append(
            "%s: found %r, require %s or newer" % (command, output, minimum)
        )

pip_check = subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    capture_output=True,
    text=True,
    check=False,
    timeout=60,
)
if pip_check.returncode != 0:
    failures.append(
        "pip check: "
        + (pip_check.stderr or pip_check.stdout or "dependency conflict").strip()
    )

print(json.dumps({"failures": failures, "versions": versions}, sort_keys=True))
raise SystemExit(1 if failures else 0)
"""
