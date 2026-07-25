"""CLI-owned safety boundary around canonical Stemcell backups.

This module does not implement snapshots or uploads.  It secures the active
Silicon's source customizations as a content-addressed overlay, then callers
invoke the one canonical ``core.backup`` implementation from the active code
generation.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import REGISTRY_DIR
from .updater.cache import ReleaseCache
from .updater.generation import GenerationStore
from .updater.io import (
    atomic_write_json,
    ensure_real_directory,
    hash_tree,
    read_json,
)
from .updater.overlay import OverlayStore


class BackupSafetyError(RuntimeError):
    """A backup could not establish a trustworthy customization overlay."""


LATEST_OVERLAY_REFERENCE = Path(".silicon") / "overlays" / "latest.json"


def _hex_digest(value: object, label: str) -> str:
    digest = str(value or "")
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise BackupSafetyError(f"{label} is not a canonical SHA-256 digest")
    return digest


def _legacy_base(root: Path) -> tuple[Path, str]:
    base = root / ".silicon-upstream" / "base"
    metadata_path = root / ".silicon-upstream" / "meta.json"
    if (
        base.is_symlink()
        or not base.is_dir()
        or metadata_path.is_symlink()
        or not metadata_path.is_file()
    ):
        raise BackupSafetyError(
            "legacy Silicon has no trusted CLI upstream base; update it before "
            "running a canonical backup"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupSafetyError(
            f"legacy upstream metadata is invalid: {exc}"
        ) from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema") != 1
        or metadata.get("authority") != "silicon-cli"
    ):
        raise BackupSafetyError("legacy upstream base is not CLI-authoritative")
    digest, _files = hash_tree(base)
    recorded = str(metadata.get("tree_sha256") or "")
    if recorded and _hex_digest(recorded, "legacy upstream tree") != digest:
        raise BackupSafetyError("legacy upstream base changed after it was seeded")
    return base.resolve(strict=True), digest


def _relative_to_instance(root: Path, path: Path, label: str) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix() or "."
    except ValueError as exc:
        raise BackupSafetyError(f"{label} escaped the Silicon instance") from exc


def capture_active_customizations(
    instance: str | Path,
    *,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """Capture active source/DNA differences against the exact upstream base.

    The caller must hold the instance update lock for the entire capture and
    subsequent canonical backup.  ``latest.json`` is itself backup-visible, so
    a restored snapshot can identify and verify the exact overlay it contains.
    """

    root = Path(instance).resolve(strict=True)
    state = ensure_real_directory(root / ".silicon", root=root)
    work_root = ensure_real_directory(state / "work", root=state)
    overlays_root = ensure_real_directory(state / "overlays", root=state)
    generations = GenerationStore(root)
    current = generations.current()
    active = generations.resolve_release(current).resolve(strict=True)
    kind = str(current.get("kind") or "")
    base_source: str

    temporary = Path(tempfile.mkdtemp(prefix="backup-overlay.", dir=work_root))
    try:
        if kind == "immutable-release":
            upstream = _hex_digest(
                current.get("upstream_tree_sha256"),
                "active upstream tree",
            )
            cache = ReleaseCache(Path(cache_root or REGISTRY_DIR / "cache"))
            try:
                cached = cache.load(upstream)
            except Exception as exc:
                raise BackupSafetyError(
                    "the exact upstream release needed to capture active "
                    f"customizations is unavailable or corrupt: {upstream}"
                ) from exc
            if cached.manifest.identity.tree_sha256 != upstream:
                raise BackupSafetyError(
                    "release cache identity does not match the active generation"
                )
            base = temporary / "base"
            cache.materialize(cached, base)
            base_source = "verified-release-cache"
        elif kind == "legacy-flat":
            base, upstream = _legacy_base(root)
            base_source = "legacy-cli-seed"
        else:
            raise BackupSafetyError(f"unsupported active generation kind: {kind}")

        store = OverlayStore(root)
        captured = store.capture(
            base,
            active,
            base_tree_sha256=upstream,
        )
        overlay_hash = _hex_digest(
            captured.get("root_hash"),
            "customization overlay",
        )
        overlay_manifest = store.verify(overlay_hash)
        if overlay_manifest.get("base_tree_sha256") != upstream:
            raise BackupSafetyError(
                "customization overlay is bound to the wrong upstream release"
            )
        if generations.current() != current:
            raise BackupSafetyError(
                "active generation changed while customizations were captured"
            )

        reference = {
            "schema": 1,
            "generation_id": str(current.get("generation_id") or ""),
            "generation_kind": kind,
            "active_release_path": _relative_to_instance(
                root,
                active,
                "active release",
            ),
            "base_source": base_source,
            "base_tree_sha256": upstream,
            "overlay_root_hash": overlay_hash,
            "overlay_manifest_path": (
                Path(".silicon")
                / "overlays"
                / "manifests"
                / f"{overlay_hash}.json"
            ).as_posix(),
            "captured_at": time.time(),
        }
        if not reference["generation_id"]:
            raise BackupSafetyError("active generation has no stable identity")
        atomic_write_json(overlays_root / "latest.json", reference, mode=0o400)
        return load_latest_customization_reference(root)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def load_latest_customization_reference(
    instance: str | Path,
) -> dict[str, Any]:
    """Load and fully verify the durable backup-visible overlay reference."""

    root = Path(instance).resolve(strict=True)
    path = root / LATEST_OVERLAY_REFERENCE
    if path.is_symlink() or not path.is_file():
        raise BackupSafetyError("customization overlay reference is missing or unsafe")
    try:
        value = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BackupSafetyError(
            f"customization overlay reference is invalid: {exc}"
        ) from exc
    expected = {
        "schema",
        "generation_id",
        "generation_kind",
        "active_release_path",
        "base_source",
        "base_tree_sha256",
        "overlay_root_hash",
        "overlay_manifest_path",
        "captured_at",
    }
    if (
        set(value) != expected
        or value.get("schema") != 1
        or value.get("generation_kind") not in {
            "immutable-release",
            "legacy-flat",
        }
        or value.get("base_source") not in {
            "verified-release-cache",
            "legacy-cli-seed",
        }
        or not isinstance(value.get("captured_at"), (int, float))
        or isinstance(value.get("captured_at"), bool)
        or float(value["captured_at"]) <= 0
        or not str(value.get("generation_id") or "")
    ):
        raise BackupSafetyError("customization overlay reference has invalid fields")
    base_hash = _hex_digest(value["base_tree_sha256"], "overlay base tree")
    overlay_hash = _hex_digest(
        value["overlay_root_hash"],
        "customization overlay",
    )
    expected_manifest = (
        Path(".silicon")
        / "overlays"
        / "manifests"
        / f"{overlay_hash}.json"
    ).as_posix()
    if value.get("overlay_manifest_path") != expected_manifest:
        raise BackupSafetyError("overlay reference points at the wrong manifest")
    active_relative = str(value.get("active_release_path") or "")
    active = (root / active_relative).resolve(strict=False)
    _relative_to_instance(root, active, "referenced active release")
    manifest = OverlayStore(root).verify(overlay_hash)
    if manifest.get("base_tree_sha256") != base_hash:
        raise BackupSafetyError("overlay reference and manifest disagree")
    return value
