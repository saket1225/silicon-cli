"""Create / hydrate a silicon from the silicon-stemcell base.

`silicon new <dir>` downloads the stemcell, copies in any files the target is
missing (never clobbering env.py / silicon.json / .glass.json), seeds config +
env keys, prompts for the one brain provider order, installs requirements, and
registers the instance.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import interface_cli, registry, ui
from .config import REGISTRY_DIR
from .updater.cache import ReleaseCache
from .updater.channel import fetch_latest_release
from .updater.generation import GenerationStore, ManagedPointerMissing
from .updater.io import fsync_dir, hash_tree
from .updater.overlay import OverlayStore
from .updater.release import (
    FetchedRelease,
    release_identity_is_authoritative,
)

SKIP_NAMES = {".git", "__pycache__", ".DS_Store"}
PRESERVE_ROOT = {"env.py", "silicon.json", ".glass.json"}
ALLOWED_PROVIDERS = {"claude", "codex", "chatgpt"}
DATA_ROOT_CAPABILITY = ".silicon-data-root-v1"
DATA_SEED_ROOT_FILES = ("silicon.json", "env.py", ".backupsilicon")
DATA_SEED_DIRECTORIES = (
    "core/cron",
    "core/interface_state",
    "logs",
    "prompts/advertising",
    "prompts/memory/carbons",
    "prompts/memory/projects",
    "prompts/memory/silicons",
    "sessions",
    "worker/outputs",
)
LEGACY_FLAT_MARKERS = ("main.py", "manager.py", "glass_agent.py")


@dataclass(frozen=True)
class PreparedStemcell:
    """One verified release shared by every Silicon in a pull operation."""

    cache: ReleaseCache
    release: FetchedRelease
    source: Path
    environment: Path | None


@contextmanager
def prepare_hydration(
    *,
    install_deps: bool = True,
    expected_tree_sha256: str | None = None,
    bind_docker_runtime: bool = False,
) -> Iterator[PreparedStemcell]:
    """Fetch, verify, materialize, and prepare a Stemcell exactly once.

    The yielded source is read-only input for one or many calls to
    :func:`hydrate`.  No candidate code is imported or executed.  Dependency
    preparation consumes only the authenticated hash-pinned lockfile.
    """

    cache = ReleaseCache(REGISTRY_DIR / "cache")
    if expected_tree_sha256:
        ui.info("Loading the published Silicon release for pull recovery...")
        release = cache.load(expected_tree_sha256)
    else:
        release = fetch_latest_release(
            cache,
            info=ui.info,
        )
    # Runtime binding is caller-scoped. A local hydration must never inherit
    # an unrelated Docker setting from the operator's ambient CLI home.
    if bind_docker_runtime:
        from . import docker_runtime

        if not docker_runtime.enabled():
            raise RuntimeError(
                "Docker hydration requested before the Docker runtime was "
                "initialized"
            )
        docker_config = docker_runtime.bind_release_runtime(
            release.manifest.runtime_image
        )
        docker_runtime.maybe_prompt_login(docker_config)
    temporary = Path(tempfile.mkdtemp(prefix="silicon-hydration-release-"))
    try:
        source = temporary / "source"
        cache.materialize(release, source)
        if not (source / "main.py").is_file():
            raise RuntimeError("verified Silicon release has no main.py")
        if not (source / DATA_ROOT_CAPABILITY).is_file():
            raise RuntimeError(
                "verified Silicon release does not support a separate durable "
                "data root"
            )
        if (
            (source / "requirements.txt").is_file()
            and not (source / "requirements.lock").is_file()
        ):
            raise RuntimeError(
                "verified Silicon release has requirements.txt but no "
                "hash-pinned requirements.lock"
            )
        environment = None
        if install_deps:
            ui.info("Preparing one shared, hash-pinned Python environment...")
            environment = cache.prepare_environment(
                source,
                runner=lambda command: subprocess.run(
                    command, check=False
                ).returncode,
            )
        yield PreparedStemcell(cache, release, source, environment)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _env_value(env_path: Path, key: str) -> str:
    if not env_path.exists():
        return ""
    m = re.search(rf'^{key}\s*=\s*["\'](.*)["\']\s*$', env_path.read_text(), re.M)
    return m.group(1) if m else ""


def _env_upsert(env_path: Path, key: str, value: str) -> None:
    text = env_path.read_text() if env_path.exists() else ""
    pattern = rf'^{key}\s*=\s*["\'].*["\']\s*$'
    replacement = f'{key} = "{value}"'
    if re.search(pattern, text, re.M):
        text = re.sub(pattern, replacement, text, flags=re.M)
    else:
        text = (text.rstrip() + "\n" if text.strip() else "") + replacement + "\n"
    env_path.write_text(text.rstrip() + "\n")


def _provider_list(value, default):
    if not isinstance(value, list):
        return default
    out = []
    for item in value:
        if isinstance(item, str) and item in ALLOWED_PROVIDERS:
            v = "codex" if item == "chatgpt" else item
            if v not in out:
                out.append(v)
    return out or default


def _choose_brain_order(primary: str) -> list[str]:
    primary = "codex" if primary == "codex" else "claude"
    fallback = "claude" if primary == "codex" else "codex"
    if ui.confirm(f"Use {fallback} as fallback brain for all workers?", default_yes=True):
        return [primary, fallback]
    return [primary]


def _copy_missing_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"verified hydration seed is unsafe: {source}")
    if destination.exists() or destination.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _seed_durable_instance_data(source: Path, destination: Path) -> None:
    """Seed only mutable instance state; executable code stays in generations."""

    for relative in DATA_SEED_ROOT_FILES:
        candidate = source / relative
        if candidate.exists() or candidate.is_symlink():
            _copy_missing_file(candidate, destination / relative)

    templates = source / "templates"
    if templates.exists() or templates.is_symlink():
        if templates.is_symlink() or not templates.is_dir():
            raise RuntimeError("verified hydration template root is unsafe")
        for candidate in templates.rglob("*"):
            if candidate.is_symlink():
                raise RuntimeError(
                    f"verified hydration template is unsafe: {candidate}"
                )
            relative = candidate.relative_to(templates)
            target = destination / relative
            if candidate.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif candidate.is_file():
                _copy_missing_file(candidate, target)
            else:
                raise RuntimeError(
                    f"verified hydration template is unsafe: {candidate}"
                )

    for relative in DATA_SEED_DIRECTORIES:
        (destination / relative).mkdir(parents=True, exist_ok=True)


def _seed_legacy_flat_install(source: Path, destination: Path) -> None:
    """Compatibility seed for a pre-generation Silicon being migrated in place."""

    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"verified hydration source is unsafe: {path}")
        rel = path.relative_to(source)
        if any(part in SKIP_NAMES for part in rel.parts):
            continue
        target = destination / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if len(rel.parts) == 1 and rel.parts[0] in PRESERVE_ROOT and target.exists():
            continue
        if target.exists() or target.is_symlink():
            continue
        _copy_missing_file(path, target)


def _install_initial_generation(
    destination: Path,
    prepared: PreparedStemcell,
    *,
    install_deps: bool,
) -> None:
    """Atomically establish the authenticated first code generation."""

    destination = Path(destination).resolve(strict=True)
    identity = prepared.release.manifest.identity
    expected_tree = identity.tree_sha256
    generation_id = f"{expected_tree[:16]}-{expected_tree[:16]}"
    store = GenerationStore(destination)
    generation = store.releases / generation_id
    if generation.exists():
        if generation.is_symlink() or not generation.is_dir():
            raise RuntimeError("initial generation target is unsafe")
        actual_tree, _files = hash_tree(generation)
        if actual_tree != expected_tree:
            raise RuntimeError("existing initial generation is corrupt")
    else:
        store.releases.mkdir(parents=True, exist_ok=True)
        temporary = store.releases / f".{generation_id}.{os.getpid()}.tmp"
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            shutil.copytree(prepared.source, temporary)
            actual_tree, _files = hash_tree(temporary)
            if actual_tree != expected_tree:
                raise RuntimeError(
                    "materialized initial generation changed while copying"
                )
            os.replace(temporary, generation)
            fsync_dir(store.releases)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    environment_path = ""
    if install_deps:
        if prepared.environment is None:
            if (generation / "requirements.lock").is_file():
                raise RuntimeError(
                    "the shared dependency environment was not prepared"
                )
        else:
            environment_path = str(prepared.environment.resolve(strict=True))

    overlay = OverlayStore(destination).capture(
        prepared.source,
        generation,
        base_tree_sha256=expected_tree,
    )
    new_generation = {
        "generation_id": generation_id,
        "release_path": generation.relative_to(destination).as_posix(),
        "upstream_tree_sha256": expected_tree,
        "materialized_tree_sha256": expected_tree,
        "environment_path": environment_path,
        "release": identity.to_dict(),
        "runtime_image": prepared.release.manifest.runtime_image,
        "overlay_root_hash": overlay["root_hash"],
    }
    try:
        current = store.current()
    except ManagedPointerMissing:
        # A first hydration can lose power after the fail-closed managed marker
        # is durable but before its pointer is published. The exact generation
        # above has already been hash-verified against the authenticated
        # release, so republishing that pointer is the only safe recovery.
        floor = store.release_floor()
        if floor is not None and (
            not release_identity_is_authoritative(identity)
            or identity.sequence != int(floor["sequence"])
            or identity.tree_sha256 != floor["tree_sha256"]
        ):
            raise RuntimeError(
                "managed first-generation recovery does not match the "
                "published release recorded before the crash"
            )
        store.activate(new_generation, previous=store.legacy_flat())
        return
    if current.get("kind") == "immutable-release":
        if current.get("upstream_tree_sha256") != expected_tree:
            raise RuntimeError(
                "target already has a different active Silicon generation"
            )
        return
    store.activate(new_generation)


def hydrate(
    target: str,
    setup_config=None,
    *,
    install_deps: bool = True,
    setup_interface: bool = True,
    register_install: bool = True,
    prepared: PreparedStemcell | None = None,
    bind_docker_runtime: bool = False,
) -> None:
    if prepared is None:
        with prepare_hydration(
            install_deps=install_deps,
            bind_docker_runtime=bind_docker_runtime,
        ) as shared:
            hydrate(
                target,
                setup_config,
                install_deps=install_deps,
                setup_interface=setup_interface,
                register_install=register_install,
                prepared=shared,
                bind_docker_runtime=False,
            )
        return

    abs_target = str(Path(target).resolve())
    os.makedirs(abs_target, exist_ok=True)
    dst = Path(abs_target)
    src = prepared.source

    # Instance name: silicon.json address/name, else folder name
    name = ""
    sj = dst / "silicon.json"
    if sj.exists():
        try:
            data = json.loads(sj.read_text())
            name = (data.get("address") or data.get("name") or "").strip()
        except Exception:
            pass
    if not name:
        name = dst.name

    ui.info(f"Hydrating {abs_target}...")
    if any((dst / marker).is_file() for marker in LEGACY_FLAT_MARKERS):
        _seed_legacy_flat_install(src, dst)
    else:
        _seed_durable_instance_data(src, dst)

    # Seed silicon.json
    silicon = {}
    if sj.exists():
        try:
            silicon = json.loads(sj.read_text())
        except json.JSONDecodeError:
            silicon = {}
    silicon.setdefault("name", "Silicon")
    silicon.setdefault("run", "python main.py")
    silicon.setdefault("brain", "claude")
    silicon.setdefault(
        "workers",
        {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]},
    )
    if not silicon.get("address"):  # the stemcell ships an empty address — fill it
        silicon["address"] = name
    silicon.pop("version", None)
    sj.write_text(json.dumps(silicon, indent=4) + "\n")

    # Seed env.py required keys used by the current stemcell.
    env_path = dst / "env.py"
    for key, default in {"GLASS_API_KEY": "", "BROWSER_PROFILE": name}.items():
        if not _env_value(env_path, key):
            _env_upsert(env_path, key, default)

    # Interactive setup modifies only durable instance data. The published code
    # generation remains byte-for-byte identical to its release identity.
    if setup_config is not None:
        _apply_setup(sj, setup_config)
    elif ui.interactive():
        _interactive_setup(sj)

    _install_initial_generation(dst, prepared, install_deps=install_deps)

    if register_install:
        registry.register(name, abs_target)
    if setup_interface:
        interface_cli.setup(abs_target)
    ui.success(f"Hydrated '{name}' at {abs_target}")
    ui.info(f"Run 'silicon start {name}' when you're ready.")


def choose_setup_config(
    label: str = "Default silicon settings",
    *,
    brain: str | None = None,
    brain_order: list[str] | None = None,
) -> dict:
    # Brain provider order — one choice drives manager + every worker type.
    if label:
        ui.info(label)
    # A caller-supplied brain (e.g. Glass's non-interactive setup agent) skips the
    # prompt entirely and is honored even if that tool isn't detected on PATH yet
    # (it may still be installing during provisioning).
    if brain in ("claude", "codex"):
        order = [b for b in (brain_order or [brain]) if b in ("claude", "codex")] or [brain]
        return {
            "brain": brain,
            "brain_order": order,
            "workers": {k: _provider_list(order, [brain]) for k in ("browser", "terminal", "writer")},
        }
    brain = "claude"
    order = ["claude"]
    workers = {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]}
    have_claude = bool(shutil.which("claude"))
    have_codex = bool(shutil.which("codex"))
    if have_claude and have_codex:
        ui.info("Detected both claude and codex.")
        brain = "codex" if ui.ask("Who do you want the brain to be – claude or codex?", "claude") == "codex" else "claude"
        order = _choose_brain_order(brain)
        workers = {"browser": order, "terminal": order, "writer": order}
    elif have_codex:
        brain = "codex"
        order = ["codex"]
        workers = {"browser": ["codex"], "terminal": ["codex"], "writer": ["codex"]}
    return {
        "brain": brain,
        "brain_order": order,
        "workers": {k: _provider_list(v, ["claude"]) for k, v in workers.items()},
    }


def _apply_setup(sj: Path, setup_config: dict) -> None:
    try:
        silicon = json.loads(sj.read_text())
    except Exception:
        silicon = {}
    brain = setup_config.get("brain") if isinstance(setup_config, dict) else ""
    order = setup_config.get("brain_order") if isinstance(setup_config, dict) else None
    workers = setup_config.get("workers") if isinstance(setup_config, dict) else None
    silicon["brain"] = "codex" if brain == "codex" else "claude"
    silicon["brain_order"] = _provider_list(order, [silicon["brain"]])
    silicon["workers"] = {
        "browser": _provider_list((workers or {}).get("browser"), silicon["brain_order"]),
        "terminal": _provider_list((workers or {}).get("terminal"), silicon["brain_order"]),
        "writer": _provider_list((workers or {}).get("writer"), silicon["brain_order"]),
    }
    sj.write_text(json.dumps(silicon, indent=4) + "\n")


def _interactive_setup(sj: Path) -> None:
    _apply_setup(sj, choose_setup_config(""))
