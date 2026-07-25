"""Schema-identical bootstrap adapter for pre-snapshot-api Stemcells.

New Stemcells own recovery policy through ``core.data_policy`` and
``core.backup``.  This narrowly scoped adapter exists only so a legacy flat
installation can take the mandatory first recovery point needed to update into
that world.  It writes the same schema-1 object/manifest layout, excludes
plaintext credentials unconditionally, and is not used once the installed
Stemcell exposes the canonical APIs.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping

from .io import atomic_write_json, fsync_dir, sha256_file, validate_relative_path
from .lock import AdvisoryFileLock, AdvisoryLockError

SNAPSHOT_SCHEMA = 1
CLASSES = {
    "security_state": (
        ".silicon/release-sequence-floor.json",
    ),
    "critical_living": (
        "prompts/MEMORY.md",
        "prompts/memory/**",
        "prompts/LORE.md",
        "prompts/CONTACTS.md",
        "silicon.json",
        ".backupsilicon",
        ".silicon/data-policy.json",
    ),
    "task_delivery": (
        "core/interface_state/**",
        ".silicon-interface/**",
        "sessions/**",
        "worker/sessions/**",
    ),
    "self_customization": (
        "prompts/**/*.md",
        "extensions/**",
        "skills/**",
        ".silicon/overlays/**",
    ),
    "artifacts": ("logs/**", "worker/outputs/**"),
}
SECRET_NAMES = {
    ".glass.json",
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
RESTORE_PRESERVE_EXACT = {
    # The maintenance epoch/fence is newer control state, not user memory.
    # Rewinding it could let a resumed process accept work under an old epoch.
    "core/interface_state/maintenance.json",
}
RELEASE_SEQUENCE_FLOOR = ".silicon/release-sequence-floor.json"
RELEASE_SEQUENCE_FLOOR_LOCK = ".silicon/release-sequence-floor.lock"
MAX_RELEASE_SEQUENCE_FLOOR_BYTES = 4096


class BootstrapSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ReleaseFloorPlan:
    snapshot_entry: Mapping[str, object] | None
    snapshot_sequence: int | None
    snapshot_tree_sha256: str | None
    minimum_sequence: int
    minimum_tree_sha256: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_release_sequence_floor(
    value: Mapping[str, object],
    *,
    label: str,
) -> tuple[int, str]:
    sequence = value.get("sequence")
    tree_sha256 = value.get("tree_sha256")
    recorded_at = value.get("recorded_at")
    if (
        set(value) != {"schema", "sequence", "tree_sha256", "recorded_at"}
        or value.get("schema") != 1
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
        or not isinstance(tree_sha256, str)
        or len(tree_sha256) != 64
        or any(char not in "0123456789abcdef" for char in tree_sha256)
        or not isinstance(recorded_at, (int, float))
        or isinstance(recorded_at, bool)
        or not math.isfinite(float(recorded_at))
        or float(recorded_at) <= 0
    ):
        raise BootstrapSnapshotError(f"{label} is invalid")
    return sequence, tree_sha256


def _read_release_sequence_floor(
    path: Path,
    *,
    label: str,
) -> tuple[int, str] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BootstrapSnapshotError(f"could not inspect {label}: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > MAX_RELEASE_SEQUENCE_FLOOR_BYTES
    ):
        raise BootstrapSnapshotError(
            f"{label} must be a bounded, unredirected regular file"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BootstrapSnapshotError(
            f"could not securely open {label}: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise BootstrapSnapshotError(f"{label} changed while opening")
            payload = handle.read(MAX_RELEASE_SEQUENCE_FLOOR_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (
                len(payload) > MAX_RELEASE_SEQUENCE_FLOOR_BYTES
                or _stat_signature(opened) != _stat_signature(after)
            ):
                raise BootstrapSnapshotError(f"{label} changed while reading")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapSnapshotError(f"{label} is corrupt: {exc}") from exc
    if not isinstance(value, dict):
        raise BootstrapSnapshotError(f"{label} must be a JSON object")
    return _validate_release_sequence_floor(value, label=label)


def _plan_release_sequence_floor_restore(
    root: Path,
    store: Path,
    manifest: Mapping[str, object],
) -> _ReleaseFloorPlan | None:
    current = _read_release_sequence_floor(
        root / RELEASE_SEQUENCE_FLOOR,
        label="release sequence floor",
    )
    snapshot_entry = next(
        (
            entry
            for entry in manifest["files"]  # type: ignore[union-attr]
            if str(entry["path"]) == RELEASE_SEQUENCE_FLOOR
        ),
        None,
    )
    snapshot = None
    if snapshot_entry is not None:
        digest = str(snapshot_entry["sha256"])
        snapshot = _read_release_sequence_floor(
            store / "objects" / "sha256" / digest[:2] / digest[2:],
            label="snapshot release sequence floor",
        )
        if snapshot is None:
            raise BootstrapSnapshotError(
                "snapshot release sequence floor object is missing"
            )
    if current is None and snapshot is None:
        return None
    if (
        current is not None
        and snapshot is not None
        and current[0] == snapshot[0]
        and current[1] != snapshot[1]
    ):
        raise BootstrapSnapshotError(
            "release sequence floor reuses one sequence for different "
            "immutable release trees"
        )
    minimum = current
    if minimum is None or (
        snapshot is not None and snapshot[0] > minimum[0]
    ):
        minimum = snapshot
    assert minimum is not None
    return _ReleaseFloorPlan(
        snapshot_entry=snapshot_entry,
        snapshot_sequence=snapshot[0] if snapshot is not None else None,
        snapshot_tree_sha256=snapshot[1] if snapshot is not None else None,
        minimum_sequence=minimum[0],
        minimum_tree_sha256=minimum[1],
    )


def _release_floor_needs_restore(
    root: Path,
    plan: _ReleaseFloorPlan,
) -> bool:
    if plan.snapshot_entry is None:
        return False
    assert plan.snapshot_sequence is not None
    assert plan.snapshot_tree_sha256 is not None
    current = _read_release_sequence_floor(
        root / RELEASE_SEQUENCE_FLOOR,
        label="release sequence floor",
    )
    if current is None:
        return True
    if (
        current[0] == plan.snapshot_sequence
        and current[1] != plan.snapshot_tree_sha256
    ):
        raise BootstrapSnapshotError(
            "release sequence floor reuses one sequence for different "
            "immutable release trees"
        )
    return current[0] < plan.snapshot_sequence


def _verify_release_sequence_floor(
    root: Path,
    plan: _ReleaseFloorPlan,
) -> None:
    current = _read_release_sequence_floor(
        root / RELEASE_SEQUENCE_FLOOR,
        label="release sequence floor",
    )
    if current is None or current[0] < plan.minimum_sequence:
        raise BootstrapSnapshotError(
            "snapshot restore lowered or removed the release sequence floor"
        )
    if (
        current[0] == plan.minimum_sequence
        and current[1] != plan.minimum_tree_sha256
    ):
        raise BootstrapSnapshotError(
            "snapshot restore changed the immutable release tree at the "
            "release sequence floor"
        )


def _safe_pattern(pattern: str) -> str:
    value = str(pattern).strip()
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)
    ):
        raise BootstrapSnapshotError(f"unsafe protected path pattern: {pattern!r}")
    return PurePosixPath(*PurePosixPath(value).parts).as_posix()


def _secret(relative: str) -> bool:
    name = PurePosixPath(relative).name.casefold()
    return (
        name in SECRET_NAMES
        or name.startswith(".env.")
        or name.endswith(SECRET_SUFFIXES)
    )


def _matches(relative: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    # ``**/`` may match zero path components in the canonical policy.
    while "**/" in pattern:
        pattern = pattern.replace("**/", "", 1)
        if fnmatch.fnmatchcase(relative, pattern):
            return True
    return False


def _legacy_patterns(root: Path) -> tuple[str, ...]:
    path = root / ".backupsilicon"
    if not path.is_file() or path.is_symlink():
        return ()
    return tuple(
        _safe_pattern(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _local_additions(root: Path) -> dict[str, tuple[str, ...]]:
    path = root / ".silicon" / "data-policy.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapSnapshotError(f"invalid data-policy.json: {exc}") from exc
    additive = value.get("additive", {}) if isinstance(value, dict) else {}
    if not isinstance(additive, dict):
        raise BootstrapSnapshotError("data policy additive classes must be an object")
    return {
        str(name): tuple(_safe_pattern(item) for item in patterns)
        for name, patterns in additive.items()
        if isinstance(patterns, list)
    }


def _protection_patterns(root: Path) -> dict[str, tuple[str, ...]]:
    patterns = {name: tuple(values) for name, values in CLASSES.items()}
    for name, values in _local_additions(root).items():
        patterns[name] = tuple((*patterns.get(name, ()), *values))
    legacy = _legacy_patterns(root)
    if legacy:
        patterns["legacy_additive"] = legacy
    return patterns


def _classes_for(
    relative: str, patterns: dict[str, tuple[str, ...]]
) -> list[str]:
    return sorted(
        name
        for name, values in patterns.items()
        if any(_matches(relative, pattern) for pattern in values)
    )


def _inventory(root: Path) -> list[tuple[str, Path]]:
    result = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        kept = []
        for name in sorted(directories):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if (
                name == "__pycache__"
                or relative
                in {
                    ".git",
                    ".home",
                    ".local",
                    ".tools",
                    ".venv",
                    ".silicon/releases",
                    ".silicon/environments",
                    ".silicon/work",
                    ".silicon/transactions",
                    ".silicon/snapshots",
                }
            ):
                continue
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                raise BootstrapSnapshotError(
                    f"refusing symlink in protected source tree: {relative}"
                )
            if not stat.S_ISDIR(mode):
                raise BootstrapSnapshotError(
                    f"refusing special protected source: {relative}"
                )
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise BootstrapSnapshotError(
                    f"refusing linked/special protected source: {relative}"
                )
            result.append((relative, path))
    return result


def create_local_snapshot(root: Path, *, release_id: str) -> dict[str, str]:
    root = Path(root).resolve(strict=True)
    store = root / ".silicon" / "snapshots"
    objects = store / "objects" / "sha256"
    manifests = store / "manifests"
    objects.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    patterns = _protection_patterns(root)
    entries = []
    for relative, source in _inventory(root):
        classes = _classes_for(relative, patterns)
        if not classes:
            continue
        if _secret(relative):
            raise BootstrapSnapshotError(
                f"protected data attempted to include plaintext credentials: {relative}"
            )
        digest = sha256_file(source)
        destination = objects / digest[:2] / digest[2:]
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.tmp"
            )
            try:
                shutil.copyfile(source, temporary)
                os.chmod(temporary, 0o400)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                if sha256_file(temporary) != digest:
                    raise BootstrapSnapshotError(
                        f"protected source changed while copying: {relative}"
                    )
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        if sha256_file(destination) != digest:
            raise BootstrapSnapshotError(f"snapshot object is corrupt: {relative}")
        entries.append(
            {
                "path": relative,
                "sha256": digest,
                "size": source.stat().st_size,
                "mode": stat.S_IMODE(source.stat().st_mode),
                "classes": classes,
            }
        )
    entries.sort(key=lambda item: os.fsencode(str(item["path"])))
    body = {
        "schema": SNAPSHOT_SCHEMA,
        "release_id": str(release_id),
        "files": entries,
        "tombstones": [],
    }
    root_hash = hashlib.sha256(_canonical(body)).hexdigest()
    manifest = {**body, "root_hash": root_hash}
    manifest_path = manifests / f"{root_hash}.json"
    atomic_write_json(manifest_path, manifest, mode=0o400)
    verify_local_snapshot(manifest_path, store=store)
    return {
        "root_hash": root_hash,
        "manifest_path": str(manifest_path),
        "store": str(store),
        "provider": "silicon-cli-bootstrap",
    }


def verify_local_snapshot(manifest_path: Path, *, store: Path) -> dict:
    path = Path(manifest_path)
    if path.is_symlink():
        raise BootstrapSnapshotError("snapshot manifest must not be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapSnapshotError(f"invalid snapshot manifest: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "release_id",
        "files",
        "tombstones",
        "root_hash",
    }:
        raise BootstrapSnapshotError("snapshot manifest fields are invalid")
    body = {key: value[key] for key in value if key != "root_hash"}
    if (
        value["schema"] != SNAPSHOT_SCHEMA
        or hashlib.sha256(_canonical(body)).hexdigest() != value["root_hash"]
    ):
        raise BootstrapSnapshotError("snapshot root hash is invalid")
    if not isinstance(value["files"], list) or not isinstance(
        value["tombstones"], list
    ):
        raise BootstrapSnapshotError("snapshot files and tombstones must be arrays")
    seen = set()
    for entry in value["files"]:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "size",
            "mode",
            "classes",
        }:
            raise BootstrapSnapshotError("snapshot file entry is invalid")
        relative = str(entry.get("path", ""))
        validate_relative_path(relative)
        if relative in seen or _secret(relative):
            raise BootstrapSnapshotError(
                f"duplicate or forbidden snapshot path: {relative}"
            )
        seen.add(relative)
        digest = str(entry.get("sha256", ""))
        size = entry.get("size")
        mode = entry.get("mode")
        classes = entry.get("classes")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
            or not isinstance(classes, list)
            or any(not isinstance(item, str) or not item for item in classes)
            or (
                all(isinstance(item, str) for item in classes)
                and classes != sorted(set(classes))
            )
        ):
            raise BootstrapSnapshotError(
                f"snapshot metadata is invalid: {relative}"
            )
        source = Path(store) / "objects" / "sha256" / digest[:2] / digest[2:]
        if (
            len(digest) != 64
            or source.is_symlink()
            or not source.is_file()
            or sha256_file(source) != digest
            or source.stat().st_size != size
        ):
            raise BootstrapSnapshotError(
                f"snapshot object failed verification: {relative}"
            )
        if relative == RELEASE_SEQUENCE_FLOOR:
            if (
                _read_release_sequence_floor(
                    source,
                    label="snapshot release sequence floor",
                )
                is None
            ):
                raise BootstrapSnapshotError(
                    "snapshot release sequence floor object is missing"
                )
    tombstones = value["tombstones"]
    if any(not isinstance(relative, str) for relative in tombstones):
        raise BootstrapSnapshotError("snapshot tombstones are invalid")
    if (
        tombstones != sorted(set(tombstones), key=os.fsencode)
        or any(relative in seen or _secret(relative) for relative in tombstones)
    ):
        raise BootstrapSnapshotError("snapshot tombstones are invalid")
    for relative in tombstones:
        validate_relative_path(relative)
    return value


def _restore_local_snapshot_in_place_locked(
    root: Path,
    manifest_path: Path,
    *,
    store: Path,
) -> dict:
    """Idempotently restore protected legacy data while all services are stopped."""

    root = Path(root).resolve(strict=True)
    expected_store = root / ".silicon" / "snapshots"
    if Path(store).is_symlink() or Path(manifest_path).is_symlink():
        raise BootstrapSnapshotError("snapshot restore paths must not be symlinks")
    store = Path(store).resolve(strict=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    if (
        store != expected_store.resolve(strict=True)
        or store not in manifest_path.parents
    ):
        raise BootstrapSnapshotError("snapshot restore escaped the instance store")
    manifest = verify_local_snapshot(manifest_path, store=store)
    release_floor_plan = _plan_release_sequence_floor_restore(
        root,
        store,
        manifest,
    )
    patterns = _protection_patterns(root)
    snapshot_paths = {
        str(entry["path"])
        for entry in manifest["files"]
        if str(entry["path"]) not in RESTORE_PRESERVE_EXACT
    }
    current_protected = {
        relative
        for relative, _path in _inventory(root)
        if relative not in (*RESTORE_PRESERVE_EXACT, RELEASE_SEQUENCE_FLOOR)
        and _classes_for(relative, patterns)
    }
    delete_paths = (
        current_protected - snapshot_paths
    ) | {
        str(relative)
        for relative in manifest["tombstones"]
        if str(relative)
        not in (*RESTORE_PRESERVE_EXACT, RELEASE_SEQUENCE_FLOOR)
    }
    # Deletions are confined concrete paths. Repeating them after a crash is a
    # no-op; restoring every file below is atomic per path.
    for relative in sorted(delete_paths, key=os.fsencode, reverse=True):
        target = root / validate_relative_path(relative)
        if target.is_symlink():
            raise BootstrapSnapshotError(
                f"refusing symlink while restoring protected data: {relative}"
            )
        if target.is_file():
            target.unlink()
            fsync_dir(target.parent)
        elif target.exists():
            raise BootstrapSnapshotError(
                f"protected restore target is not a regular file: {relative}"
            )
    for entry in manifest["files"]:
        relative = str(entry["path"])
        if relative in RESTORE_PRESERVE_EXACT:
            continue
        if relative == RELEASE_SEQUENCE_FLOOR:
            if release_floor_plan is None:
                raise BootstrapSnapshotError(
                    "release sequence floor restore plan is missing"
                )
            if not _release_floor_needs_restore(root, release_floor_plan):
                continue
        target = root / validate_relative_path(relative)
        cursor = root
        for component in Path(relative).parts[:-1]:
            cursor = cursor / component
            if cursor.is_symlink():
                raise BootstrapSnapshotError(
                    f"refusing linked restore parent: {relative}"
                )
            if cursor.exists() and not cursor.is_dir():
                raise BootstrapSnapshotError(
                    f"restore parent is not a directory: {relative}"
                )
            cursor.mkdir(exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise BootstrapSnapshotError(
                f"restore target is not a regular file: {relative}"
            )
        digest = str(entry["sha256"])
        source = store / "objects" / "sha256" / digest[:2] / digest[2:]
        temporary = target.with_name(f".{target.name}.{os.getpid()}.restore")
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, int(entry["mode"]))
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            if (
                sha256_file(temporary) != digest
                or temporary.stat().st_size != int(entry["size"])
            ):
                raise BootstrapSnapshotError(
                    f"restored snapshot object changed: {relative}"
                )
            os.replace(temporary, target)
            fsync_dir(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
    if release_floor_plan is not None:
        _verify_release_sequence_floor(root, release_floor_plan)
    return {
        "root_hash": str(manifest["root_hash"]),
        "restored_files": len(snapshot_paths),
        "deleted_files": len(delete_paths),
    }


def restore_local_snapshot_in_place(
    root: Path,
    manifest_path: Path,
    *,
    store: Path,
) -> dict:
    """Restore protected data under the updater's shared release-floor lock."""

    requested_root = Path(root)
    try:
        metadata = requested_root.lstat()
    except OSError as exc:
        raise BootstrapSnapshotError(
            f"could not inspect snapshot restore root: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BootstrapSnapshotError(
            "snapshot restore root must be a real directory"
        )
    canonical_root = requested_root.resolve(strict=True)
    try:
        with AdvisoryFileLock(
            canonical_root / RELEASE_SEQUENCE_FLOOR_LOCK,
            label="release sequence floor lock",
        ):
            return _restore_local_snapshot_in_place_locked(
                canonical_root,
                manifest_path,
                store=store,
            )
    except AdvisoryLockError as exc:
        raise BootstrapSnapshotError(str(exc)) from exc
