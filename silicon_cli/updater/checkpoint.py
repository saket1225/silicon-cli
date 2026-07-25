"""Portable, confined recovery-checkpoint references."""
from __future__ import annotations

from pathlib import Path


class CheckpointError(RuntimeError):
    pass


def _root_hash(value: object) -> str:
    digest = str(value or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CheckpointError("recovery checkpoint has an invalid root hash")
    return digest


def normalize_checkpoint(instance: Path, value: dict[str, str]) -> dict[str, str]:
    """Validate a freshly created checkpoint and store instance-relative paths."""

    instance = Path(instance).resolve(strict=True)
    if not isinstance(value, dict):
        raise CheckpointError("recovery checkpoint provider returned no object")
    result = {str(key): str(item) for key, item in value.items()}
    result["root_hash"] = _root_hash(result.get("root_hash"))
    snapshots = instance / ".silicon" / "snapshots"
    store_input = Path(result.get("store", ""))
    manifest_input = Path(result.get("manifest_path", ""))
    if store_input.is_symlink() or manifest_input.is_symlink():
        raise CheckpointError("recovery checkpoint paths must not be symbolic links")
    try:
        store = store_input.resolve(strict=True)
        manifest = manifest_input.resolve(strict=True)
    except OSError as exc:
        raise CheckpointError(f"recovery checkpoint is unavailable: {exc}") from exc
    expected_store = snapshots.resolve(strict=True)
    if store != expected_store:
        raise CheckpointError(
            "recovery checkpoint store escaped the instance snapshot store"
        )
    if store not in manifest.parents or not manifest.is_file():
        raise CheckpointError(
            "recovery checkpoint manifest escaped its snapshot store"
        )
    result["store"] = store.relative_to(instance).as_posix()
    result["manifest_path"] = manifest.relative_to(instance).as_posix()
    provider = result.get("provider", "")
    if provider and (len(provider) > 64 or "\x00" in provider):
        raise CheckpointError("recovery checkpoint provider is invalid")
    return result


def resolve_checkpoint(instance: Path, value: dict[str, str]) -> dict[str, str]:
    """Resolve a durable checkpoint reference after an instance is moved."""

    instance = Path(instance).resolve(strict=True)
    result = {str(key): str(item) for key, item in value.items()}
    result["root_hash"] = _root_hash(result.get("root_hash"))
    snapshots = (instance / ".silicon" / "snapshots").resolve(strict=True)
    for key in ("store", "manifest_path"):
        path = Path(result.get(key, ""))
        if path.is_absolute():
            raise CheckpointError(
                f"journaled recovery checkpoint {key} must be instance-relative"
            )
        resolved = (instance / path).resolve(strict=True)
        if resolved != snapshots and snapshots not in resolved.parents:
            raise CheckpointError(
                f"journaled recovery checkpoint {key} escaped its snapshot store"
            )
        if resolved.is_symlink():
            raise CheckpointError(
                f"journaled recovery checkpoint {key} must not be a symbolic link"
            )
        result[key] = str(resolved)
    manifest = Path(result["manifest_path"])
    if not manifest.is_file() or Path(result["store"]) not in manifest.parents:
        raise CheckpointError("journaled recovery checkpoint manifest is invalid")
    return result
