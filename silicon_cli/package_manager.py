"""Fleet package inventory and safe, ownership-aware update orchestration."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import certifi

from . import (
    docker_runtime,
    interface_cli,
    process,
    registry,
    runtime_contract,
    update,
)
from .config import (
    REGISTRY_DIR,
    SILICON_RELEASE_MANIFEST_URL,
    active_release_root,
    active_environment_python,
)
from .host_lock import HostFileLock

INVENTORY_MARKER = "SILICON_PACKAGE_INVENTORY="
UPDATE_MARKER = "SILICON_PACKAGE_UPDATE="
MAX_HTTP_BYTES = 2 * 1024 * 1024
VERSION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?)(?!\d)")


@dataclass(frozen=True)
class PackageSpec:
    key: str
    label: str
    manager: str
    package: str
    command: str
    latest_source: str


PACKAGE_SPECS = (
    PackageSpec(
        "silicon",
        "Silicon",
        "signed-release",
        "silicon-stemcell",
        "silicon update <target>",
        "glass",
    ),
    PackageSpec(
        "silicon-cli",
        "Silicon CLI",
        "pip",
        "silicon-cli",
        "silicon script update",
        "pypi",
    ),
    PackageSpec(
        "silicon-browser",
        "Silicon Browser",
        "pip",
        "silicon-browser",
        "python -m pip install --upgrade silicon-browser",
        "pypi",
    ),
    PackageSpec(
        "silicon-extend",
        "Silicon Extend",
        "pip",
        "silicon-extend",
        "python -m pip install --upgrade silicon-extend",
        "pypi",
    ),
    PackageSpec(
        "silicon-interface",
        "Silicon Interface CLI",
        "npm",
        "@teamofsilicons/silicon-interface-cli",
        "npm install -g @teamofsilicons/silicon-interface-cli@latest",
        "npm",
    ),
    PackageSpec(
        "claude",
        "Claude Code",
        "npm",
        "@anthropic-ai/claude-code",
        "npm install -g @anthropic-ai/claude-code@latest",
        "npm",
    ),
    PackageSpec(
        "codex",
        "Codex",
        "npm",
        "@openai/codex",
        "npm install -g @openai/codex@latest",
        "npm",
    ),
)
PACKAGE_BY_KEY = {spec.key: spec for spec in PACKAGE_SPECS}
DOCKER_VERSION_KEYS = {
    "silicon-cli": "silicon-cli",
    "silicon-browser": "silicon-browser",
    "silicon-extend": "silicon-extend",
    "silicon-interface": "silicon-interface",
    "claude": "claude",
    "codex": "codex",
}


def _http_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "silicon-cli-package-inventory/1",
        },
    )
    context = __import__("ssl").create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=12, context=context) as response:
        raw = response.read(MAX_HTTP_BYTES + 1)
    if len(raw) > MAX_HTTP_BYTES:
        raise RuntimeError("package registry response exceeded the size limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("package registry returned a non-object response")
    return value


def _latest_version(spec: PackageSpec) -> tuple[str, str]:
    try:
        if spec.latest_source == "pypi":
            encoded = urllib.parse.quote(spec.package, safe="")
            body = _http_json(f"https://pypi.org/pypi/{encoded}/json")
            return str((body.get("info") or {}).get("version") or ""), ""
        if spec.latest_source == "npm":
            encoded = urllib.parse.quote(spec.package, safe="")
            body = _http_json(
                f"https://registry.npmjs.org/{encoded}/latest"
            )
            return str(body.get("version") or ""), ""
        body = _http_json(SILICON_RELEASE_MANIFEST_URL)
        signed = body.get("signed") if isinstance(body, dict) else {}
        identity = signed.get("identity") if isinstance(signed, dict) else {}
        return str((identity or {}).get("version") or ""), ""
    except Exception as exc:
        return "", str(exc)[:300]


def _latest_versions() -> tuple[dict[str, str], list[str]]:
    latest: dict[str, str] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(PACKAGE_SPECS))) as pool:
        futures = {
            pool.submit(_latest_version, spec): spec
            for spec in PACKAGE_SPECS
        }
        for future in as_completed(futures):
            spec = futures[future]
            version, error = future.result()
            latest[spec.key] = version
            if error:
                errors.append(f"{spec.key}: {error}")
    return latest, errors


def _numeric_version(value: str) -> str:
    match = VERSION_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _status(installed: str, latest: str) -> str:
    if not installed:
        return "missing"
    if not latest:
        return "unknown"
    return (
        "current"
        if runtime_contract.version_at_least(installed, latest)
        else "outdated"
    )


def _command_version(command: str | Path) -> str:
    executable = str(command)
    if os.path.sep not in executable:
        executable = shutil.which(executable) or ""
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _numeric_version(result.stdout or result.stderr or "")


def _python_package_version(python: str, package: str) -> str:
    code = (
        "from importlib.metadata import PackageNotFoundError,version\n"
        f"try: print(version({package!r}))\n"
        "except PackageNotFoundError: pass\n"
    )
    try:
        result = subprocess.run(
            [python, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _numeric_version(result.stdout) if result.returncode == 0 else ""


def _glass_identity(install: registry.Install) -> dict[str, str]:
    path = Path(install.path) / ".glass.json"
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        "silicon_id": str(value.get("silicon_id") or ""),
        "team_slug": str(value.get("team_slug") or ""),
    }


def _selected_installs(silicon_ids: set[str]) -> list[registry.Install]:
    rows = []
    for install in registry.installs():
        identity = _glass_identity(install)
        if silicon_ids and identity.get("silicon_id") not in silicon_ids:
            continue
        rows.append(install)
    return rows


def _row(
    spec: PackageSpec,
    installed: str,
    latest: str,
    *,
    location: str,
    strategy: str,
    install: registry.Install | None = None,
    detail: str = "",
) -> dict:
    identity = _glass_identity(install) if install is not None else {}
    return {
        "key": spec.key,
        "label": spec.label,
        "package": spec.package,
        "manager": spec.manager,
        "installed_version": installed,
        "latest_version": latest,
        "status": _status(installed, latest),
        "location": location,
        "update_strategy": strategy,
        "update_command": (
            f"silicon package update {spec.key}"
            if strategy != "system"
            else ""
        ),
        "instance_name": install.name if install is not None else "",
        "silicon_id": identity.get("silicon_id", ""),
        "team_slug": identity.get("team_slug", ""),
        "runtime": install.runtime if install is not None else "host",
        "image": install.image if install is not None else "",
        "detail": detail,
    }


def _host_rows(latest: dict[str, str]) -> list[dict]:
    installed_cli = ""
    try:
        installed_cli = metadata.version("silicon-cli")
    except metadata.PackageNotFoundError:
        pass
    values = {
        "silicon-cli": installed_cli,
        "silicon-browser": _command_version("silicon-browser"),
        "silicon-extend": _command_version("silicon-extend"),
        "silicon-interface": _command_version("silicon-interface"),
        "claude": _command_version("claude"),
        "codex": _command_version("codex"),
    }
    return [
        _row(
            PACKAGE_BY_KEY[key],
            value,
            latest.get(key, ""),
            location="host",
            strategy="host-pip" if PACKAGE_BY_KEY[key].manager == "pip" else "host-npm",
            detail="Shared host installation outside Docker",
        )
        for key, value in values.items()
    ]


def _docker_rows(
    install: registry.Install,
    latest: dict[str, str],
    cache: dict[str, dict],
) -> list[dict]:
    config = docker_runtime.config_for_install(install)
    image = str(install.image or config.get("image") or "")
    if image not in cache:
        try:
            cache[image] = docker_runtime.inspect_runtime_contract(
                config,
                image,
            )
        except RuntimeError as exc:
            cache[image] = {
                "versions": {},
                "failures": [str(exc)],
                "returncode": 1,
            }
    payload = cache[image]
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        versions = {}
    failures = payload.get("failures")
    detail = "; ".join(str(item) for item in failures or [])[:1000]
    rows = []
    for key, version_key in DOCKER_VERSION_KEYS.items():
        rows.append(
            _row(
                PACKAGE_BY_KEY[key],
                _numeric_version(str(versions.get(version_key) or "")),
                latest.get(key, ""),
                location="docker-image",
                strategy="signed-runtime",
                install=install,
                detail=detail,
            )
        )
    silicon_version = ""
    try:
        silicon_version = (
            active_release_root(install.path) / "silicon.info"
        ).read_text(encoding="utf-8").strip()[:64]
    except (OSError, RuntimeError):
        pass
    rows.append(
        _row(
            PACKAGE_BY_KEY["silicon"],
            _numeric_version(silicon_version) or silicon_version,
            latest.get("silicon", ""),
            location="signed-generation",
            strategy="signed-runtime",
            install=install,
        )
    )
    return rows


def _local_rows(install: registry.Install, latest: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    silicon_version = ""
    try:
        silicon_version = (
            active_release_root(install.path) / "silicon.info"
        ).read_text(encoding="utf-8").strip()[:64]
    except (OSError, RuntimeError):
        pass
    rows.append(
        _row(
            PACKAGE_BY_KEY["silicon"],
            _numeric_version(silicon_version) or silicon_version,
            latest.get("silicon", ""),
            location="signed-generation",
            strategy="signed-release",
            install=install,
        )
    )
    python = active_environment_python(install.path)
    rows.append(
        _row(
            PACKAGE_BY_KEY["silicon-extend"],
            (
                _python_package_version(python, "silicon-extend")
                if python
                else ""
            ),
            latest.get("silicon-extend", ""),
            location="generation-environment",
            strategy="signed-release",
            install=install,
        )
    )
    interface = (
        Path(install.path)
        / ".silicon-interface"
        / "bin"
        / "silicon-interface"
    )
    rows.append(
        _row(
            PACKAGE_BY_KEY["silicon-interface"],
            _command_version(interface),
            latest.get("silicon-interface", ""),
            location="instance",
            strategy="local-interface",
            install=install,
        )
    )
    return rows


def inventory(*, silicon_ids: set[str] | None = None) -> dict:
    selected_ids = set(silicon_ids or ())
    latest, errors = _latest_versions()
    rows = _host_rows(latest)
    docker_cache: dict[str, dict] = {}
    installs = _selected_installs(selected_ids)
    for install in installs:
        if install.is_docker:
            rows.extend(_docker_rows(install, latest, docker_cache))
        else:
            rows.extend(_local_rows(install, latest))
    summary = {
        "total": len(rows),
        "current": 0,
        "outdated": 0,
        "missing": 0,
        "unknown": 0,
    }
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    return {
        "schema": 1,
        "packages": rows,
        "latest": latest,
        "summary": summary,
        "errors": errors,
        "installations": len(installs),
    }


def _run_step(command: list[str], label: str) -> dict:
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        return {"step": label, "status": "failed", "detail": str(exc)}
    return {
        "step": label,
        "status": "done" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "detail": "",
    }


def _update_host_package(spec: PackageSpec) -> dict:
    if spec.manager == "pip":
        return _run_step(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                spec.package,
            ],
            f"host:{spec.key}",
        )
    npm = shutil.which("npm")
    if not npm:
        return {
            "step": f"host:{spec.key}",
            "status": "failed",
            "detail": "npm is not installed on the host",
        }
    return _run_step(
        [npm, "install", "-g", f"{spec.package}@latest"],
        f"host:{spec.key}",
    )


def _update_package_unlocked(
    package_key: str,
    *,
    silicon_ids: set[str] | None = None,
) -> dict:
    spec = PACKAGE_BY_KEY.get(package_key)
    if spec is None:
        raise RuntimeError(
            "unknown package; choose one of: "
            + ", ".join(sorted(PACKAGE_BY_KEY))
        )
    installs = _selected_installs(set(silicon_ids or ()))
    if not installs:
        raise RuntimeError("no matching registered Silicon installations")
    steps: list[dict] = []
    docker_installs = [install for install in installs if install.is_docker]
    local_installs = [install for install in installs if not install.is_docker]

    signed_targets = list(docker_installs)
    if package_key in {"silicon", "silicon-extend"}:
        signed_targets.extend(local_installs)
    if signed_targets:
        names = ",".join(dict.fromkeys(install.name for install in signed_targets))
        try:
            update.update_instance(names)
        except SystemExit as exc:
            code = int(exc.code or 1)
            steps.append(
                {
                    "step": "signed-release",
                    "status": "done" if code == 0 else "failed",
                    "returncode": code,
                    "detail": "",
                }
            )
        except Exception as exc:
            steps.append(
                {
                    "step": "signed-release",
                    "status": "failed",
                    "detail": str(exc),
                }
            )
        else:
            steps.append({"step": "signed-release", "status": "done"})

    if package_key != "silicon":
        running_local = [
            install.name
            for install in registry.installs()
            if not install.is_docker and process.install_is_running(install)
        ]
        if running_local:
            steps.append(
                {
                    "step": f"host:{package_key}",
                    "status": "blocked",
                    "detail": (
                        "shared host package was not mutated while local "
                        "Silicons are running: " + ", ".join(running_local)
                    ),
                }
            )
        else:
            steps.append(_update_host_package(spec))

    if package_key == "silicon-interface":
        for install in local_installs:
            if process.install_is_running(install):
                steps.append(
                    {
                        "step": f"interface:{install.name}",
                        "status": "blocked",
                        "detail": "stop this local Silicon before replacing its Interface CLI",
                    }
                )
                continue
            try:
                interface_cli.setup(
                    install.path,
                    required=True,
                    force=True,
                )
            except Exception as exc:
                steps.append(
                    {
                        "step": f"interface:{install.name}",
                        "status": "failed",
                        "detail": str(exc),
                    }
                )
            else:
                steps.append(
                    {
                        "step": f"interface:{install.name}",
                        "status": "done",
                    }
                )

    failed = [step for step in steps if step["status"] in {"failed", "blocked"}]
    return {
        "schema": 1,
        "package": package_key,
        "status": "succeeded" if not failed else "partial",
        "steps": steps,
        "installations": [install.name for install in installs],
    }


def update_package(
    package_key: str,
    *,
    silicon_ids: set[str] | None = None,
) -> dict:
    """Serialize host mutations even across Glass teams and manual CLI runs."""

    with HostFileLock(REGISTRY_DIR / "package-update.lock", timeout=2):
        return _update_package_unlocked(
            package_key,
            silicon_ids=silicon_ids,
        )


def _parse_silicon_ids(arguments: list[str]) -> set[str]:
    values: set[str] = set()
    index = 0
    while index < len(arguments):
        if arguments[index] == "--silicon-id" and index + 1 < len(arguments):
            value = arguments[index + 1].strip()
            if (
                not value
                or len(value) > 128
                or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
            ):
                raise RuntimeError("invalid --silicon-id")
            values.add(value)
            index += 1
        index += 1
    return values


def package_command(arguments: list[str]) -> None:
    action = arguments[0] if arguments else ""
    if action == "inventory":
        result = inventory(silicon_ids=_parse_silicon_ids(arguments[1:]))
        if "--json" in arguments:
            print(INVENTORY_MARKER + json.dumps(result, sort_keys=True))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return
    if action == "update" and len(arguments) >= 2:
        result = update_package(
            arguments[1],
            silicon_ids=_parse_silicon_ids(arguments[2:]),
        )
        if "--json" in arguments:
            print(UPDATE_MARKER + json.dumps(result, sort_keys=True))
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        if result["status"] != "succeeded":
            raise SystemExit(2)
        return
    raise RuntimeError(
        "Usage: silicon package inventory [--json] [--silicon-id ID] | "
        "silicon package update <package> [--json] [--silicon-id ID]"
    )
