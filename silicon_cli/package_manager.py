"""Fleet package inventory and safe, ownership-aware update orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
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
    SILICON_INTERFACE_CLI_RELEASE_SHA256,
    SILICON_INTERFACE_CLI_RELEASE_URL,
    SILICON_INTERFACE_CLI_VERSION,
    STEMCELL_GIT_URL,
    active_environment_python,
    active_release_root,
)
from .host_lock import HostFileLock
from .updater.release import resolve_latest_published_git_release

INVENTORY_MARKER = "SILICON_PACKAGE_INVENTORY="
UPDATE_MARKER = "SILICON_PACKAGE_UPDATE="
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_INTERFACE_CLI_BYTES = 16 * 1024 * 1024
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
        "git-release",
        "silicon-stemcell",
        "silicon update <target>",
        "git",
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
        f"npm install -g {SILICON_INTERFACE_CLI_RELEASE_URL}",
        "embedded",
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
    context = ssl.create_default_context(cafile=certifi.where())
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
        if spec.latest_source == "git":
            return (
                resolve_latest_published_git_release(
                    STEMCELL_GIT_URL
                ).version,
                "",
            )
        if spec.latest_source == "embedded":
            return SILICON_INTERFACE_CLI_VERSION, ""
        raise RuntimeError(f"unknown latest-version source: {spec.latest_source}")
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
    direct_update = strategy != "published-runtime"
    update_package = spec.key if direct_update else "silicon"
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
            f"silicon package update {update_package}"
            if strategy != "system"
            else ""
        ),
        "direct_update": direct_update,
        "update_package": update_package,
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
                strategy="published-runtime",
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
            location="immutable-generation",
            strategy="git-release",
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
            location="immutable-generation",
            strategy="git-release",
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
            strategy="git-release",
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
        try:
            if install.is_docker:
                rows.extend(_docker_rows(install, latest, docker_cache))
            else:
                rows.extend(_local_rows(install, latest))
        except (OSError, RuntimeError) as exc:
            errors.append(f"{install.name}: {str(exc)[:300]}")
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


def _download_verified_interface_cli(destination: Path) -> None:
    request = urllib.request.Request(
        SILICON_INTERFACE_CLI_RELEASE_URL,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "silicon-cli-package-update/1",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    digest = hashlib.sha256()
    size = 0
    with (
        urllib.request.urlopen(request, timeout=30, context=context) as response,
        destination.open("wb") as output,
    ):
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_INTERFACE_CLI_BYTES:
                raise RuntimeError(
                    "Silicon Interface CLI release asset exceeded the size limit"
                )
            digest.update(chunk)
            output.write(chunk)
    actual = digest.hexdigest()
    if actual != SILICON_INTERFACE_CLI_RELEASE_SHA256:
        raise RuntimeError(
            "Silicon Interface CLI release checksum mismatch: "
            f"expected {SILICON_INTERFACE_CLI_RELEASE_SHA256}, got {actual}"
        )


def _verified_global_interface_script(npm: str) -> Path:
    try:
        root_result = subprocess.run(
            [npm, "root", "-g"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not locate the installed Interface CLI: {exc}"
        ) from exc
    roots = [
        line.strip()
        for line in root_result.stdout.splitlines()
        if line.strip()
    ]
    if root_result.returncode != 0 or len(roots) != 1:
        detail = (root_result.stderr or root_result.stdout).strip()
        raise RuntimeError(
            "could not locate the installed Interface CLI"
            + (f": {detail}" if detail else "")
        )
    package_root = Path(roots[0]).expanduser().resolve(strict=True)
    script = (
        package_root
        / "@teamofsilicons"
        / "silicon-interface-cli"
        / "bin"
        / "silicon-interface.mjs"
    )
    if script.is_symlink() or not script.is_file():
        raise RuntimeError(
            "the checksum-verified Interface CLI package script is missing"
        )
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not installed on the host")
    try:
        version_result = subprocess.run(
            [node, str(script), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"could not verify the installed Interface CLI: {exc}"
        ) from exc
    installed = _numeric_version(
        version_result.stdout or version_result.stderr
    )
    if (
        version_result.returncode != 0
        or installed != SILICON_INTERFACE_CLI_VERSION
    ):
        raise RuntimeError(
            "installed Interface CLI does not match the verified release: "
            f"found {installed or 'unknown'}, "
            f"expected {SILICON_INTERFACE_CLI_VERSION}"
        )
    return script


def _update_host_interface(npm: str, spec: PackageSpec) -> dict:
    label = f"host:{spec.key}"
    try:
        with tempfile.TemporaryDirectory(
            prefix="silicon-interface-update-"
        ) as temporary:
            artifact = Path(temporary) / (
                f"teamofsilicons-silicon-interface-cli-"
                f"{SILICON_INTERFACE_CLI_VERSION}.tgz"
            )
            _download_verified_interface_cli(artifact)
            step = _run_step(
                [
                    npm,
                    "install",
                    "-g",
                    "--no-audit",
                    "--no-fund",
                    str(artifact),
                ],
                label,
            )
            if step["status"] != "done":
                return step
            step["_verified_source_script"] = str(
                _verified_global_interface_script(npm)
            )
            return step
    except Exception as exc:
        return {
            "step": label,
            "status": "failed",
            "detail": str(exc),
        }


def _update_host_package(spec: PackageSpec) -> dict:
    if spec.manager == "pip":
        step = _run_step(
            [
                sys.executable,
                "-m",
                "pip",
                "--disable-pip-version-check",
                "--no-input",
                "install",
                "--upgrade",
                spec.package,
            ],
            f"host:{spec.key}",
        )
        if step["status"] == "done" and spec.key == "silicon-cli":
            try:
                step["installed_version"] = metadata.version(spec.package)
            except metadata.PackageNotFoundError:
                return {
                    "step": f"host:{spec.key}",
                    "status": "failed",
                    "detail": (
                        "pip reported success but silicon-cli is not installed"
                    ),
                }
        return step
    npm = shutil.which("npm")
    if not npm:
        return {
            "step": f"host:{spec.key}",
            "status": "failed",
            "detail": "npm is not installed on the host",
        }
    if spec.key == "silicon-interface":
        return _update_host_interface(npm, spec)
    return _run_step(
        [npm, "install", "-g", f"{spec.package}@latest"],
        f"host:{spec.key}",
    )


def _update_package_unlocked(
    package_key: str,
    *,
    silicon_ids: set[str] | None = None,
) -> dict:
    package_started = time.monotonic()
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
    local_installs = [install for install in installs if not install.is_docker]

    # Docker-image package copies are immutable runtime contents. Updating a
    # host tool must never turn into a full drain/checkpoint/restart of every
    # selected Docker Silicon. Runtime copies move only with an explicit
    # Silicon release; silicon-extend remains generation-owned for local
    # installs.
    release_targets: list[registry.Install] = []
    if package_key == "silicon":
        release_targets.extend(installs)
    elif package_key == "silicon-extend":
        release_targets.extend(local_installs)
    if release_targets:
        names = ",".join(
            dict.fromkeys(install.name for install in release_targets)
        )
        update_started = time.monotonic()
        try:
            update_result = update.update_instance(names)
        except SystemExit as exc:
            code = int(exc.code or 1)
            steps.append(
                {
                    "step": "git-release",
                    "status": "done" if code == 0 else "failed",
                    "returncode": code,
                    "detail": "",
                    "timings_seconds": {
                        "total": round(time.monotonic() - update_started, 3)
                    },
                }
            )
        except Exception as exc:
            steps.append(
                {
                    "step": "git-release",
                    "status": "failed",
                    "detail": str(exc),
                    "timings_seconds": {
                        "total": round(time.monotonic() - update_started, 3)
                    },
                }
            )
        else:
            steps.append(
                {
                    "step": "git-release",
                    "status": "done",
                    "timings_seconds": (
                        update_result.get("timings_seconds", {})
                        if isinstance(update_result, dict)
                        else {
                            "total": round(
                                time.monotonic() - update_started,
                                3,
                            )
                        }
                    ),
                }
            )

    interface_source_script = ""
    if package_key != "silicon":
        running_local = [
            install.name
            for install in registry.installs()
            if not install.is_docker and process.install_is_running(install)
        ]
        if running_local and package_key != "silicon-cli":
            host_step = {
                "step": f"host:{package_key}",
                "status": "blocked",
                "detail": (
                    "shared host package was not mutated while local "
                    "Silicons are running: " + ", ".join(running_local)
                ),
            }
        else:
            host_step = _update_host_package(spec)
        if package_key == "silicon-interface":
            interface_source_script = str(
                host_step.pop("_verified_source_script", "")
            )
        steps.append(host_step)

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
            if not interface_source_script:
                steps.append(
                    {
                        "step": f"interface:{install.name}",
                        "status": "blocked",
                        "detail": (
                            "the checksum-verified host Interface CLI was not "
                            "installed; the per-instance copy was left intact"
                        ),
                    }
                )
                continue
            try:
                interface_cli.setup(
                    install.path,
                    required=True,
                    force=True,
                    source_script=interface_source_script,
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
        "timings_seconds": {
            "total": round(time.monotonic() - package_started, 3),
        },
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
