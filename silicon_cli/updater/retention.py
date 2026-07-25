"""Bounded, reference-aware retention for immutable update material."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .io import fsync_dir
from .journal import TERMINAL_STATES, TransactionJournal
from .lock import InstanceLock
from .overlay import OverlayError, OverlayStore

_TREE_RE = re.compile(r"^[0-9a-f]{64}$")
_OVERLAY_MANIFEST_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_OBJECT_PREFIX_RE = re.compile(r"^[0-9a-f]{2}$")
_OBJECT_SUFFIX_RE = re.compile(r"^[0-9a-f]{62}$")
MAX_OVERLAY_MANIFESTS = 100_000
MAX_OVERLAY_OBJECTS = 2_000_000
MAX_ACTIVE_POINTER_BYTES = 1024 * 1024


class RetentionError(RuntimeError):
    pass


@dataclass
class _References:
    releases: set[Path]
    trees: set[str]
    environments: set[Path]
    overlays: set[str]


@dataclass(frozen=True)
class _OverlayPlan:
    store: Path
    delete_manifests: tuple[Path, ...]
    delete_objects: tuple[Path, ...]
    retained_roots: tuple[str, ...]
    retained_base_trees: tuple[str, ...]


def _canonical_digest(value: object, label: str) -> str:
    digest = str(value or "")
    if not _TREE_RE.fullmatch(digest):
        raise RetentionError(f"{label} is not a canonical SHA-256 digest")
    return digest


def _confined_directory(path: Path, root: Path) -> Path:
    root = root.resolve(strict=True)
    if path.is_symlink():
        raise RetentionError(f"refusing to prune a linked directory: {path}")
    resolved = path.resolve(strict=True)
    if root not in resolved.parents:
        raise RetentionError(f"refusing to prune outside {root}: {path}")
    if not resolved.is_dir():
        raise RetentionError(f"retention target is not a directory: {path}")
    return resolved


def _remove_directory(path: Path, root: Path) -> None:
    resolved = _confined_directory(path, root)
    trash = resolved.with_name(f".gc-{resolved.name}-{os.getpid()}")
    os.replace(resolved, trash)
    shutil.rmtree(trash)


def _require_real_directory(path: Path, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RetentionError(f"could not inspect {label}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RetentionError(f"{label} must be a real directory")
    return path


def _remove_regular_file(path: Path, parent: Path, label: str) -> None:
    _require_real_directory(parent, f"{label} parent")
    if path.parent != parent:
        raise RetentionError(f"{label} escaped its canonical directory: {path}")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RetentionError(f"{label} changed before deletion: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RetentionError(f"refusing unsafe {label}: {path}")
    path.unlink()


def _read_bounded_json(path: Path, label: str) -> dict:
    try:
        before = path.lstat()
    except OSError as exc:
        raise RetentionError(f"could not inspect {label}: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > MAX_ACTIVE_POINTER_BYTES
    ):
        raise RetentionError(f"{label} must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RetentionError(f"could not securely open {label}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise RetentionError(f"{label} changed while opening")
            payload = handle.read(MAX_ACTIVE_POINTER_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (
                len(payload) > MAX_ACTIVE_POINTER_BYTES
                or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
            ):
                raise RetentionError(f"{label} changed while reading")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RetentionError(f"could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RetentionError(f"{label} must contain a JSON object")
    return value


def _active_generation(instance: Path) -> dict:
    state = instance / ".silicon"
    pointer = state / "current.json"
    marker = state / "generation-managed-v1.json"
    if not pointer.exists() and not pointer.is_symlink():
        if marker.exists() or marker.is_symlink():
            raise RetentionError(
                f"managed generation pointer is missing for {instance}"
            )
        return {
            "release_path": str(instance),
            "upstream_tree_sha256": "",
            "environment_path": "",
        }
    value = _read_bounded_json(pointer, f"{instance} active generation pointer")
    if (
        value.get("schema") != 1
        or not isinstance(value.get("release_path"), str)
        or not value["release_path"]
        or "\x00" in value["release_path"]
        or not isinstance(value.get("upstream_tree_sha256"), str)
        or not isinstance(value.get("environment_path"), str)
        or "\x00" in value["environment_path"]
    ):
        raise RetentionError(
            f"{instance} active generation pointer has invalid fields"
        )
    upstream = str(value["upstream_tree_sha256"])
    if upstream and not _TREE_RE.fullmatch(upstream):
        raise RetentionError(
            f"{instance} active generation pointer has an invalid upstream tree"
        )
    _overlay_from_generation(value, f"{instance} active generation")
    return value


def _paths_from_generation(
    value: object, instance: Path
) -> tuple[set[Path], set[str], set[Path]]:
    releases: set[Path] = set()
    trees: set[str] = set()
    environments: set[Path] = set()
    if not isinstance(value, dict):
        return releases, trees, environments
    release_path = str(value.get("release_path") or "")
    if release_path:
        path = Path(release_path)
        if not path.is_absolute():
            path = instance / path
        releases.add(path.resolve(strict=False))
    tree = str(value.get("upstream_tree_sha256") or "")
    if _TREE_RE.fullmatch(tree):
        trees.add(tree)
    environment = str(value.get("environment_path") or "")
    if environment:
        path = Path(environment)
        if not path.is_absolute():
            path = instance / path
        environments.add(path.resolve(strict=False))
    return releases, trees, environments


def _overlay_from_generation(value: object, label: str) -> str:
    if not isinstance(value, dict):
        return ""
    raw = value.get("overlay_root_hash")
    if raw in {None, ""}:
        return ""
    return _canonical_digest(raw, f"{label} overlay")


def _overlay_references_from_journal(
    journal: TransactionJournal,
) -> set[str]:
    metadata = journal.metadata
    references: set[str] = set()
    for key in (
        "new_generation",
        "prior_generation",
        "rollback_target_generation",
    ):
        digest = _overlay_from_generation(
            metadata.get(key),
            f"{journal.transaction_id} {key}",
        )
        if digest:
            references.add(digest)
    for key in (
        "customization_overlay",
        "rollback_customization_delta",
        "rollback_customization_overlay",
    ):
        value = metadata.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise RetentionError(
                f"{journal.transaction_id} {key} must be an object"
            )
        references.add(
            _canonical_digest(
                value.get("root_hash"),
                f"{journal.transaction_id} {key}",
            )
        )
    return references


class RetentionManager:
    def __init__(
        self,
        instance: Path,
        cache_root: Path,
        *,
        all_instances: Iterable[Path] = (),
        keep_generations: int = 3,
        snapshot_gc: Callable[[], Mapping[str, Iterable[str]]] | None = None,
    ):
        self.instance = Path(instance).resolve()
        self.cache_root = Path(cache_root).resolve()
        roots = {self.instance}
        roots.update(Path(item).resolve() for item in all_instances)
        self.all_instances = sorted(roots)
        self.keep_generations = max(2, int(keep_generations))
        self.snapshot_gc = snapshot_gc

    def configure_snapshot_gc(
        self,
        callback: Callable[[], Mapping[str, Iterable[str]]],
    ) -> None:
        self.snapshot_gc = callback

    def _references(self) -> _References:
        release_paths: set[Path] = set()
        trees: set[str] = set()
        environment_paths: set[Path] = set()
        overlay_roots: set[str] = set()
        for instance in self.all_instances:
            try:
                current = _active_generation(instance)
            except Exception as exc:
                raise RetentionError(
                    f"cannot establish active generation references for "
                    f"{instance}: {exc}"
                ) from exc
            groups = _paths_from_generation(current, instance)
            release_paths.update(groups[0])
            trees.update(groups[1])
            environment_paths.update(groups[2])
            current_overlay = _overlay_from_generation(
                current,
                f"{instance} active generation",
            )
            if current_overlay and instance == self.instance:
                overlay_roots.add(current_overlay)

            latest_overlay = (
                instance / ".silicon" / "overlays" / "latest.json"
            )
            if latest_overlay.exists() or latest_overlay.is_symlink():
                # Deferred to avoid importing backup/config modules while the
                # updater package initializes.
                from ..backup_runtime import (
                    BackupSafetyError,
                    load_latest_customization_reference,
                )

                try:
                    latest = load_latest_customization_reference(instance)
                except BackupSafetyError as exc:
                    raise RetentionError(
                        f"cannot verify latest customization backup reference "
                        f"for {instance}: {exc}"
                    ) from exc
                latest_hash = _canonical_digest(
                    latest.get("overlay_root_hash"),
                    f"{instance} latest backup overlay",
                )
                base_tree = _canonical_digest(
                    latest.get("base_tree_sha256"),
                    f"{instance} latest backup base",
                )
                trees.add(base_tree)
                release_paths.add(
                    (
                        instance
                        / Path(str(latest["active_release_path"]))
                    ).resolve(strict=False)
                )
                if instance == self.instance:
                    overlay_roots.add(latest_hash)

            committed_kept = 0
            for journal in TransactionJournal.history(instance):
                metadata = journal.metadata
                if instance == self.instance:
                    # Overlay records can be needed even by a terminal journal
                    # (notably a rollback delta). Journals remain authoritative
                    # references until journal retention removes them.
                    overlay_roots.update(
                        _overlay_references_from_journal(journal)
                    )
                generations = [
                    metadata.get("new_generation"),
                    metadata.get("prior_generation"),
                ]
                if journal.state not in TERMINAL_STATES:
                    generations.append(
                        {
                            "release_path": metadata.get("staged_release_path", ""),
                            "environment_path": metadata.get("environment_path", ""),
                            "upstream_tree_sha256": (
                                metadata.get("release", {}).get("tree_sha256", "")
                                if isinstance(metadata.get("release"), dict)
                                else ""
                            ),
                        }
                    )
                elif journal.state == "COMMITTED":
                    if committed_kept >= self.keep_generations:
                        continue
                    committed_kept += 1
                else:
                    continue
                for generation in generations:
                    groups = _paths_from_generation(generation, instance)
                    release_paths.update(groups[0])
                    trees.update(groups[1])
                    environment_paths.update(groups[2])

        # A host pull fetches and prepares its exact published release before any
        # final instance directory exists. Its separate owner-only journal is
        # therefore the only reference protecting that cache entry across a
        # concurrent update retention pass.
        pull_journals = self.cache_root.parent / "pull-transactions"
        if pull_journals.is_dir() and not pull_journals.is_symlink():
            # Deferred to avoid a package-initialization cycle:
            # pull_transaction uses updater.io while updater.__init__ exposes
            # the engine, which imports this module.
            from ..pull_transaction import (
                PullJournal,
                PullJournalError,
                TERMINAL_STATES as PULL_TERMINAL_STATES,
            )

            for path in sorted(pull_journals.glob("*.json")):
                try:
                    journal = PullJournal.load(path)
                except PullJournalError:
                    # Unknown transaction state must make retention
                    # conservative. Preserve every shared cache candidate
                    # rather than risking destruction of staged pull input.
                    release_cache = self.cache_root / "releases"
                    if release_cache.is_dir():
                        trees.update(
                            candidate.name
                            for candidate in release_cache.iterdir()
                            if candidate.is_dir()
                            and _TREE_RE.fullmatch(candidate.name)
                        )
                    environment_cache = self.cache_root / "environments"
                    if environment_cache.is_dir():
                        environment_paths.update(
                            candidate.resolve(strict=False)
                            for candidate in environment_cache.iterdir()
                            if candidate.is_dir()
                        )
                    continue
                if journal.state in PULL_TERMINAL_STATES:
                    continue
                tree = str(journal.value.get("release_tree_sha256") or "")
                if _TREE_RE.fullmatch(tree):
                    trees.add(tree)
                environment = str(journal.value.get("environment_path") or "")
                if environment:
                    environment_paths.add(
                        Path(environment).resolve(strict=False)
                    )
        return _References(
            releases=release_paths,
            trees=trees,
            environments=environment_paths,
            overlays=overlay_roots,
        )

    def _plan_overlay_prune(self, referenced: set[str]) -> _OverlayPlan:
        root = self.instance / ".silicon" / "overlays"
        if not root.exists() and not root.is_symlink():
            if referenced:
                raise RetentionError(
                    "referenced customization overlays are missing from the "
                    "instance store"
                )
            return _OverlayPlan(root, (), (), (), ())

        _require_real_directory(
            self.instance / ".silicon",
            "Silicon state directory",
        )
        _require_real_directory(root, "customization overlay store")
        for entry in sorted(os.scandir(root), key=lambda item: item.name):
            if entry.name not in {"manifests", "objects", "latest.json"}:
                raise RetentionError(
                    "unexpected customization overlay store entry: "
                    f"{root / entry.name}"
                )
        manifests = _require_real_directory(
            root / "manifests",
            "customization overlay manifests",
        )
        objects = _require_real_directory(
            root / "objects",
            "customization overlay objects",
        )
        sha_root = _require_real_directory(
            objects / "sha256",
            "customization overlay SHA-256 objects",
        )

        manifest_records: list[tuple[str, Path, int]] = []
        for entry in sorted(os.scandir(manifests), key=lambda item: item.name):
            match = _OVERLAY_MANIFEST_RE.fullmatch(entry.name)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RetentionError(
                    f"could not inspect overlay manifest {entry.name}: {exc}"
                ) from exc
            if match is None or not stat.S_ISREG(metadata.st_mode):
                raise RetentionError(
                    "unexpected or unsafe customization overlay manifest: "
                    f"{manifests / entry.name}"
                )
            manifest_records.append(
                (match.group(1), manifests / entry.name, metadata.st_mtime_ns)
            )
            if len(manifest_records) > MAX_OVERLAY_MANIFESTS:
                raise RetentionError(
                    "customization overlay retention exceeds its manifest "
                    "scan limit"
                )

        object_records: list[tuple[str, Path]] = []
        for entry in sorted(os.scandir(objects), key=lambda item: item.name):
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RetentionError(
                    f"could not inspect overlay object storage: {exc}"
                ) from exc
            if (
                entry.name != "sha256"
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise RetentionError(
                    "unexpected or unsafe customization object directory: "
                    f"{objects / entry.name}"
                )
        for prefix in sorted(os.scandir(sha_root), key=lambda item: item.name):
            prefix_path = sha_root / prefix.name
            try:
                metadata = prefix.stat(follow_symlinks=False)
            except OSError as exc:
                raise RetentionError(
                    f"could not inspect overlay object prefix: {exc}"
                ) from exc
            if (
                _OBJECT_PREFIX_RE.fullmatch(prefix.name) is None
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise RetentionError(
                    "unexpected or unsafe customization object prefix: "
                    f"{prefix_path}"
                )
            for entry in sorted(
                os.scandir(prefix_path),
                key=lambda item: item.name,
            ):
                try:
                    object_metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise RetentionError(
                        f"could not inspect customization object: {exc}"
                    ) from exc
                if (
                    _OBJECT_SUFFIX_RE.fullmatch(entry.name) is None
                    or stat.S_ISLNK(object_metadata.st_mode)
                    or not stat.S_ISREG(object_metadata.st_mode)
                ):
                    raise RetentionError(
                        "unexpected or unsafe customization object: "
                        f"{prefix_path / entry.name}"
                    )
                object_records.append(
                    (prefix.name + entry.name, prefix_path / entry.name)
                )
                if len(object_records) > MAX_OVERLAY_OBJECTS:
                    raise RetentionError(
                        "customization overlay retention exceeds its object "
                        "scan limit"
                    )

        roots = {root_hash for root_hash, _path, _mtime in manifest_records}
        missing = sorted(referenced - roots)
        if missing:
            raise RetentionError(
                "referenced customization overlay manifests are missing: "
                + ", ".join(missing)
            )
        recent = {
            root_hash
            for root_hash, _path, _mtime in sorted(
                manifest_records,
                key=lambda item: (-item[2], item[0]),
            )[: self.keep_generations]
        }
        retained = referenced | recent
        store = OverlayStore(self.instance)
        for root_hash, _path, _mtime in sorted(manifest_records):
            try:
                store.load(root_hash)
            except OverlayError as exc:
                raise RetentionError(
                    f"customization overlay {root_hash} is corrupt: {exc}"
                ) from exc

        retained_objects: set[str] = set()
        retained_base_trees: set[str] = set()
        for root_hash in sorted(retained):
            try:
                manifest = store.verify(root_hash)
            except OverlayError as exc:
                raise RetentionError(
                    f"retained customization overlay {root_hash} is corrupt: "
                    f"{exc}"
                ) from exc
            retained_base_trees.add(
                _canonical_digest(
                    manifest.get("base_tree_sha256"),
                    f"customization overlay {root_hash} base",
                )
            )
            retained_objects.update(
                _canonical_digest(
                    entry.get("sha256"),
                    f"customization overlay {root_hash} object",
                )
                for entry in manifest["files"]
            )

        return _OverlayPlan(
            store=root,
            delete_manifests=tuple(
                path
                for root_hash, path, _mtime in sorted(
                    manifest_records,
                    key=lambda item: item[0],
                )
                if root_hash not in retained
            ),
            delete_objects=tuple(
                path
                for digest, path in object_records
                if digest not in retained_objects
            ),
            retained_roots=tuple(sorted(retained)),
            retained_base_trees=tuple(sorted(retained_base_trees)),
        )

    @staticmethod
    def _apply_overlay_prune(
        plan: _OverlayPlan,
        removed: dict[str, list[str]],
    ) -> None:
        if not plan.delete_manifests and not plan.delete_objects:
            return
        manifests = plan.store / "manifests"
        sha_root = plan.store / "objects" / "sha256"
        for path in plan.delete_manifests:
            if _OVERLAY_MANIFEST_RE.fullmatch(path.name) is None:
                raise RetentionError(
                    f"refusing non-canonical overlay manifest deletion: {path}"
                )
            _remove_regular_file(path, manifests, "overlay manifest")
            removed["overlay_manifests"].append(path.stem)
        if plan.delete_manifests:
            fsync_dir(manifests)
        modified_prefixes: set[Path] = set()
        for path in plan.delete_objects:
            if (
                _OBJECT_PREFIX_RE.fullmatch(path.parent.name) is None
                or _OBJECT_SUFFIX_RE.fullmatch(path.name) is None
                or path.parent.parent != sha_root
            ):
                raise RetentionError(
                    f"refusing non-canonical overlay object deletion: {path}"
                )
            _remove_regular_file(path, path.parent, "overlay object")
            removed["overlay_objects"].append(path.parent.name + path.name)
            modified_prefixes.add(path.parent)
        for prefix in sorted(modified_prefixes):
            fsync_dir(prefix)

    def prune(self) -> dict[str, list[str]]:
        references = self._references()
        overlay_plan = self._plan_overlay_prune(references.overlays)
        references.trees.update(overlay_plan.retained_base_trees)
        releases = references.releases
        trees = references.trees
        environments = references.environments
        removed = {
            "generations": [],
            "release_cache": [],
            "environments": [],
            "overlay_manifests": [],
            "overlay_objects": [],
            "snapshot_manifests": [],
            "snapshot_objects": [],
        }

        generation_root = self.instance / ".silicon" / "releases"
        if generation_root.is_dir() and not generation_root.is_symlink():
            candidates = sorted(
                (path for path in generation_root.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            # Even an unreferenced recent generation is retained inside the
            # configurable known-good window.
            retain_recent = {
                path.resolve(strict=False)
                for path in candidates[: self.keep_generations]
            }
            for path in candidates:
                resolved = path.resolve(strict=False)
                if resolved in releases or resolved in retain_recent:
                    continue
                _remove_directory(path, generation_root)
                removed["generations"].append(path.name)

        with InstanceLock(self.cache_root, f"retention-{os.getpid()}"):
            release_cache = self.cache_root / "releases"
            if release_cache.is_dir() and not release_cache.is_symlink():
                candidates = sorted(
                    (
                        path
                        for path in release_cache.iterdir()
                        if path.is_dir() and _TREE_RE.fullmatch(path.name)
                    ),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                retain_names = {
                    path.name for path in candidates[: self.keep_generations]
                }
                for path in candidates:
                    if path.name in trees or path.name in retain_names:
                        continue
                    _remove_directory(path, release_cache)
                    removed["release_cache"].append(path.name)

            shared_environment_root = self.cache_root / "environments"
            self._prune_environments(
                shared_environment_root,
                environments,
                removed,
            )
        self._prune_environments(
            self.instance / ".silicon" / "environments",
            environments,
            removed,
        )
        self._apply_overlay_prune(overlay_plan, removed)
        if self.snapshot_gc is not None:
            result = self.snapshot_gc()
            if not isinstance(result, Mapping):
                raise RetentionError(
                    "canonical snapshot retention returned an invalid result"
                )
            expected = {"manifests", "objects"}
            if set(result) != expected:
                raise RetentionError(
                    "canonical snapshot retention returned unknown or missing "
                    "fields"
                )
            for source, destination in (
                ("manifests", "snapshot_manifests"),
                ("objects", "snapshot_objects"),
            ):
                values = result[source]
                if isinstance(values, (str, bytes)) or not isinstance(
                    values, Iterable
                ):
                    raise RetentionError(
                        "canonical snapshot retention returned an invalid "
                        f"{source} list"
                    )
                removed[destination].extend(
                    _canonical_digest(
                        value,
                        f"canonical snapshot retention {source[:-1]}",
                    )
                    for value in values
                )
        return removed

    def _prune_environments(
        self,
        environment_root: Path,
        environments: set[Path],
        removed: dict[str, list[str]],
    ) -> None:
        if environment_root.is_dir() and not environment_root.is_symlink():
            candidates = sorted(
                (
                    path
                    for path in environment_root.iterdir()
                    if path.is_dir() and not path.name.startswith(".")
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            retain_recent = {
                path.resolve(strict=False)
                for path in candidates[: self.keep_generations]
            }
            for path in candidates:
                resolved = path.resolve(strict=False)
                if resolved in environments or resolved in retain_recent:
                    continue
                _remove_directory(path, environment_root)
                removed["environments"].append(path.name)
