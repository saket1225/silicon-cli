"""Paths + endpoints. Everything is env-overridable so this CLI can point at
either the original Glass or your own."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

HOME = Path.home()
REGISTRY_DIR = Path(os.environ.get("SILICON_HOME", HOME / ".silicon"))
REGISTRY_FILE = REGISTRY_DIR / "registry.json"

# Glass sync server (pull/push). Override with GLASS_SERVER_URL to point elsewhere.
GLASS_SERVER_URL = os.environ.get("GLASS_SERVER_URL", "https://glass.teamofsilicons.com").rstrip("/")

# Stemcell — stable SemVer tags are the source for new and updated Silicons.
STEMCELL_REPO = os.environ.get("SILICON_STEMCELL_REPO", "teamofsilicons/silicon-stemcell")
_stemcell_repository_parts = STEMCELL_REPO.split("/")
if (
    re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", STEMCELL_REPO)
    is None
    or len(_stemcell_repository_parts) != 2
    or any(
        part in {".", ".."} or len(part) > 100
        for part in _stemcell_repository_parts
    )
):
    raise RuntimeError(
        "SILICON_STEMCELL_REPO must be one GitHub owner/repository pair"
    )
STEMCELL_GIT_URL = f"https://github.com/{STEMCELL_REPO}.git"

# Silicon Interface CLI. During local development, silicon-cli will auto-detect
# a sibling silicon-interface checkout. Production uses the immutable GitHub
# release asset because npm publishing is not part of the runtime release path.
SILICON_INTERFACE_CLI_VERSION = "2.0.2"
SILICON_INTERFACE_CLI_RELEASE_URL = (
    "https://github.com/teamofsilicons/silicon-interface-web/releases/download/"
    "interface-cli-v2.0.2/"
    "teamofsilicons-silicon-interface-cli-2.0.2.tgz"
)
SILICON_INTERFACE_CLI_RELEASE_SHA256 = (
    "5f594958e8165dfaf87e19a71781a628"
    "012b5debe0482dcdc24f28b308e710b2"
)
SILICON_INTERFACE_CLI_PACKAGE = os.environ.get(
    "SILICON_INTERFACE_CLI_PACKAGE",
    SILICON_INTERFACE_CLI_RELEASE_URL,
)
SILICON_INTERFACE_CLI_TARBALL = os.environ.get(
    "SILICON_INTERFACE_CLI_TARBALL",
    SILICON_INTERFACE_CLI_RELEASE_URL,
)
SILICON_INTERFACE_CLI_SOURCE = os.environ.get("SILICON_INTERFACE_CLI_SOURCE", "")
SILICON_INTERFACE_CLI_SKIP = os.environ.get("SILICON_INTERFACE_CLI_SKIP", "").lower() in {
    "1", "true", "yes", "on",
}
SILICON_INTERFACE_DAEMON_SKIP = os.environ.get("SILICON_INTERFACE_DAEMON_SKIP", "").lower() in {
    "1", "true", "yes", "on",
}


def venv_python(path: str | os.PathLike) -> str | None:
    """The silicon's own .venv interpreter, if one exists."""
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    cand = Path(path) / ".venv" / sub
    return str(cand) if cand.exists() else None


def active_release_root(path: str | os.PathLike) -> Path:
    """Resolve the atomically selected code generation, or the legacy root."""

    root = Path(path).resolve()
    pointer = root / ".silicon" / "current.json"
    from .updater.generation import GenerationError, GenerationStore

    try:
        return GenerationStore(root).active_root()
    except (OSError, GenerationError) as exc:
        raise RuntimeError(
            f"invalid Silicon generation pointer at {pointer}: {exc}"
        ) from exc


def active_environment_python(path: str | os.PathLike) -> str | None:
    root = Path(path).resolve()
    pointer = root / ".silicon" / "current.json"
    from .updater.generation import GenerationError, GenerationStore

    try:
        store = GenerationStore(root)
        generation = store.current()
        environment = store.resolve_environment(generation)
        if environment is None:
            return None
        cache_environments = (REGISTRY_DIR / "cache" / "environments").resolve()
        instance_environments = (root / ".silicon" / "environments").resolve()
        if (
            not environment.is_dir()
            or not any(
                allowed == environment or allowed in environment.parents
                for allowed in (cache_environments, instance_environments)
            )
        ):
            raise RuntimeError(
                "active Silicon environment escaped its trusted stores"
            )
        release = store.resolve_release(generation)
        lockfile = release / "requirements.lock"
        marker = environment / ".silicon-environment.json"
        if (
            not lockfile.is_file()
            or lockfile.is_symlink()
            or marker.is_symlink()
        ):
            raise RuntimeError(
                "active Silicon environment has no trusted lockfile marker"
            )
        marker_stat = marker.stat()
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or marker_stat.st_size <= 0
            or marker_stat.st_size > 64 * 1024
        ):
            raise RuntimeError("active Silicon environment marker is unsafe")
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
        from .updater.cache import runtime_platform_identity
        from .updater.io import sha256_file

        if (
            not isinstance(marker_value, dict)
            or set(marker_value)
            != {
                "requirements_sha256",
                "requirements_file",
                "require_hashes",
                "runtime",
            }
            or marker_value.get("requirements_sha256")
            != sha256_file(lockfile)
            or marker_value.get("requirements_file") != "requirements.lock"
            or marker_value.get("require_hashes") is not True
            or marker_value.get("runtime") != runtime_platform_identity()
        ):
            raise RuntimeError(
                "active Silicon environment does not match its lockfile"
            )
        candidate = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(
                "active Silicon environment has no trusted Python executable"
            )
        return str(candidate)
    except (OSError, GenerationError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"invalid Silicon generation environment at {pointer}: {exc}"
        ) from exc


def runtime_environment(path: str | os.PathLike) -> dict[str, str]:
    environment = dict(os.environ)
    root = Path(path).resolve()
    environment["SILICON_DATA_ROOT"] = str(root)
    environment["SILICON_RELEASE_ROOT"] = str(active_release_root(path))
    python = active_environment_python(root) or venv_python(root)
    if python:
        active_bin = str(Path(python).resolve().parent)
        entries = [
            entry
            for entry in environment.get("PATH", "").split(os.pathsep)
            if entry and entry != active_bin
        ]
        environment["PATH"] = os.pathsep.join([active_bin, *entries])
    return environment


def legacy_offline_update_fenced(path: str | os.PathLike) -> bool:
    """Whether a stopped legacy instance is reserved for an updater."""

    marker = (
        Path(path)
        / ".silicon"
        / "maintenance"
        / "legacy-offline.json"
    )
    # A malformed or linked marker still fails closed until `update resume`
    # validates and clears it.
    return marker.exists() or marker.is_symlink()


def base_python_cmd() -> str:
    """The interpreter used to CREATE a silicon's venv (not this CLI's venv)."""
    return os.environ.get("SILICON_PYTHON") or shutil.which("python3") or shutil.which("python") or "python3"


def python_run_cmd(path: str | os.PathLike | None = None) -> str:
    """The interpreter used to RUN a silicon's code (not this CLI's venv).

    SILICON_PYTHON always wins; otherwise prefer the silicon's own .venv —
    system interpreters are often externally managed (PEP 668) and never
    received the silicon's dependencies.
    """
    if os.environ.get("SILICON_PYTHON"):
        return os.environ["SILICON_PYTHON"]
    if path:
        active = active_environment_python(path)
        if active:
            return active
        venv = venv_python(path)
        if venv:
            return venv
    return shutil.which("python3") or shutil.which("python") or "python3"
