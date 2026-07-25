"""Atomic generation pointer for side-by-side Silicon releases."""
from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .io import atomic_write_json, fsync_dir, read_json
from .lock import AdvisoryFileLock, AdvisoryLockError
from .release import (
    ReleaseIdentity,
    ReleaseVerificationError,
    runtime_image_is_pinned,
)


class GenerationError(RuntimeError):
    pass


class ManagedPointerMissing(GenerationError):
    """A generation-managed instance lost its authoritative pointer."""


class GenerationStore:
    def __init__(self, instance: Path):
        self.instance = Path(instance).resolve(strict=True)
        self.state_root = self.instance / ".silicon"
        self.releases = self.state_root / "releases"
        self.pointer = self.state_root / "current.json"
        self.managed_marker = self.state_root / "generation-managed-v1.json"
        self.release_floor_path = self.state_root / "release-sequence-floor.json"
        self.release_floor_lock_path = (
            self.state_root / "release-sequence-floor.lock"
        )

    @staticmethod
    def _reject_symlink_chain(path: Path, *, label: str) -> None:
        cursor = path
        while True:
            if cursor.is_symlink():
                raise GenerationError(f"{label} contains a symbolic link")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent

    @staticmethod
    def _publisher_authenticated(identity: ReleaseIdentity) -> bool:
        return bool(
            identity.sequence > 0
            and identity.trust == "signed-ed25519"
            and identity.source == "glass"
        )

    def current(self) -> dict[str, Any]:
        managed = self._managed()
        # ``Path.exists()`` is false for a dangling symlink. Inspect the link
        # before treating a missing pointer as a legacy flat installation.
        if not self.pointer.exists() and not self.pointer.is_symlink():
            if managed:
                raise ManagedPointerMissing(
                    "managed generation pointer is missing; refusing to "
                    "downgrade to stale flat instance code"
                )
            return self.legacy_flat()
        if self.pointer.is_symlink() or not self.pointer.is_file():
            raise GenerationError("generation pointer is not a regular file")
        try:
            value = read_json(self.pointer)
        except (OSError, ValueError) as exc:
            raise GenerationError(
                f"could not read generation pointer: {exc}"
            ) from exc
        self.validate(value)
        return value

    def legacy_flat(self) -> dict[str, Any]:
        """Return the explicit pre-generation state for a verified migration."""

        value = {
            "schema": 1,
            "generation_id": "legacy-flat",
            "kind": "legacy-flat",
            "release_path": str(self.instance),
            "upstream_tree_sha256": "",
            "environment_path": "",
            "activated_at": 0,
        }
        self.validate(value)
        return value

    def _managed(self) -> bool:
        path = self.managed_marker
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink() or not path.is_file():
            raise GenerationError("managed-generation marker is unsafe")
        try:
            value = read_json(path)
        except (OSError, ValueError) as exc:
            raise GenerationError(
                "managed-generation marker is corrupt"
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "managed_at"}
            or value.get("schema") != 1
            or not isinstance(value.get("managed_at"), (int, float))
            or isinstance(value.get("managed_at"), bool)
            or not math.isfinite(float(value["managed_at"]))
            or float(value["managed_at"]) <= 0
        ):
            raise GenerationError("managed-generation marker is invalid")
        return True

    def release_floor(
        self, current: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Return the highest authenticated release sequence ever accepted.

        The floor is deliberately independent from the active generation:
        an explicit runtime rollback must never make an older signed release
        eligible through the normal update channel.
        """

        stored: dict[str, Any] | None = None
        path = self.release_floor_path
        if path.exists() or path.is_symlink():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size <= 0
                or path.stat().st_size > 4096
            ):
                raise GenerationError("release sequence floor is unsafe")
            try:
                value = read_json(path)
            except (OSError, ValueError) as exc:
                raise GenerationError(
                    "release sequence floor is corrupt"
                ) from exc
            if (
                set(value)
                != {"schema", "sequence", "tree_sha256", "recorded_at"}
                or value.get("schema") != 1
                or not isinstance(value.get("sequence"), int)
                or isinstance(value.get("sequence"), bool)
                or int(value["sequence"]) <= 0
                or not isinstance(value.get("tree_sha256"), str)
                or len(value["tree_sha256"]) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in value["tree_sha256"]
                )
                or not isinstance(value.get("recorded_at"), (int, float))
                or isinstance(value.get("recorded_at"), bool)
                or not math.isfinite(float(value["recorded_at"]))
                or float(value["recorded_at"]) <= 0
            ):
                raise GenerationError("release sequence floor is invalid")
            stored = value

        active: dict[str, Any] | None = None
        if isinstance(current, dict) and isinstance(current.get("release"), dict):
            try:
                identity = ReleaseIdentity.from_dict(current["release"])
            except ReleaseVerificationError as exc:
                raise GenerationError(
                    "active generation has an invalid release identity"
                ) from exc
            if self._publisher_authenticated(identity):
                active = {
                    "schema": 1,
                    "sequence": identity.sequence,
                    "tree_sha256": identity.tree_sha256,
                    "recorded_at": float(current.get("activated_at") or time.time()),
                }

        if stored is None:
            return active
        if active is None or int(stored["sequence"]) > int(active["sequence"]):
            return stored
        if int(stored["sequence"]) < int(active["sequence"]):
            return active
        if stored["tree_sha256"] != active["tree_sha256"]:
            raise GenerationError(
                "release sequence floor conflicts with the active generation"
            )
        return stored

    def _record_release_floor_unlocked(
        self,
        release: dict[str, Any] | None,
    ) -> None:
        if not isinstance(release, dict):
            return
        try:
            identity = ReleaseIdentity.from_dict(release)
        except ReleaseVerificationError as exc:
            raise GenerationError("cannot record an invalid release identity") from exc
        if not self._publisher_authenticated(identity):
            return
        existing = self.release_floor()
        if existing is not None:
            old_sequence = int(existing["sequence"])
            if old_sequence > identity.sequence:
                return
            if old_sequence == identity.sequence:
                if existing["tree_sha256"] != identity.tree_sha256:
                    raise GenerationError(
                        "release sequence was reused for different immutable content"
                    )
                return
        atomic_write_json(
            self.release_floor_path,
            {
                "schema": 1,
                "sequence": identity.sequence,
                "tree_sha256": identity.tree_sha256,
                "recorded_at": time.time(),
            },
            mode=0o600,
        )

    @contextmanager
    def _release_floor_lock(self) -> Iterator[None]:
        try:
            with AdvisoryFileLock(
                self.release_floor_lock_path,
                label="release sequence floor lock",
            ):
                yield
        except AdvisoryLockError as exc:
            raise GenerationError(str(exc)) from exc

    def record_release_floor(self, release: dict[str, Any] | None) -> None:
        """Durably raise, but never lower, the signed-release sequence floor."""

        if not isinstance(release, dict):
            return
        with self._release_floor_lock():
            self._record_release_floor_unlocked(release)

    def _enforce_release_floor(
        self,
        release: dict[str, Any] | None,
        *,
        allow_release_rollback: bool,
    ) -> None:
        if allow_release_rollback:
            return
        floor = self.release_floor()
        if floor is None:
            return
        if not isinstance(release, dict):
            raise GenerationError(
                "cannot activate an unauthenticated generation after a signed "
                "release sequence floor was established"
            )
        try:
            identity = ReleaseIdentity.from_dict(release)
        except ReleaseVerificationError as exc:
            raise GenerationError(
                "cannot activate an invalid release identity"
            ) from exc
        if not self._publisher_authenticated(identity):
            raise GenerationError(
                "cannot activate an unauthenticated generation after a signed "
                "release sequence floor was established"
            )
        if identity.sequence < int(floor["sequence"]):
            raise GenerationError(
                "cannot activate a release below the signed sequence floor"
            )
        if (
            identity.sequence == int(floor["sequence"])
            and identity.tree_sha256 != floor["tree_sha256"]
        ):
            raise GenerationError(
                "release sequence was reused for different immutable content"
            )

    def validate(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise GenerationError("generation pointer must be an object")
        if (
            not isinstance(value.get("schema"), int)
            or isinstance(value.get("schema"), bool)
            or value.get("schema") != 1
        ):
            raise GenerationError("unsupported generation pointer schema")
        kind = value.get("kind")
        common = {
            "schema",
            "generation_id",
            "kind",
            "release_path",
            "upstream_tree_sha256",
            "environment_path",
            "activated_at",
        }
        if (
            not isinstance(value.get("generation_id"), str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                value["generation_id"],
            )
            is None
            or not isinstance(value.get("release_path"), str)
            or "\x00" in value["release_path"]
            or not isinstance(value.get("environment_path"), str)
            or "\x00" in value["environment_path"]
            or not isinstance(value.get("activated_at"), (int, float))
            or isinstance(value.get("activated_at"), bool)
            or not math.isfinite(float(value["activated_at"]))
            or float(value["activated_at"]) < 0
        ):
            raise GenerationError("generation pointer has invalid scalar fields")
        if kind == "legacy-flat":
            if set(value) != common:
                raise GenerationError(
                    "legacy generation pointer has unknown or missing fields"
                )
            if value.get("upstream_tree_sha256") != "":
                raise GenerationError(
                    "legacy generation has an unexpected upstream identity"
                )
            if self.resolve_release(value) != self.instance:
                raise GenerationError("legacy generation points outside its instance")
            return
        if kind != "immutable-release":
            raise GenerationError(f"unknown generation kind: {kind}")
        expected = common | {
            "materialized_tree_sha256",
            "release",
            "overlay_root_hash",
        }
        fields = frozenset(value)
        if fields not in {
            frozenset(expected),
            frozenset(expected | {"runtime_image"}),
        }:
            raise GenerationError(
                "immutable generation pointer has unknown or missing fields"
            )
        runtime_image = value.get("runtime_image", "")
        if (
            not isinstance(runtime_image, str)
            or (runtime_image and not runtime_image_is_pinned(runtime_image))
        ):
            raise GenerationError(
                "immutable generation has an invalid runtime image"
            )
        for label in (
            "upstream_tree_sha256",
            "materialized_tree_sha256",
            "overlay_root_hash",
        ):
            digest = value.get(label)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise GenerationError(
                    f"immutable generation has an invalid {label}"
                )
        try:
            identity = ReleaseIdentity.from_dict(value.get("release"))
        except ReleaseVerificationError as exc:
            raise GenerationError(
                f"immutable generation has an invalid release identity: {exc}"
            ) from exc
        if identity.tree_sha256 != value["upstream_tree_sha256"]:
            raise GenerationError(
                "generation and release upstream identities disagree"
            )
        raw_release = Path(value["release_path"])
        expected_release = (
            Path(".silicon") / "releases" / value["generation_id"]
        )
        if raw_release.is_absolute() or raw_release != expected_release:
            raise GenerationError(
                "immutable generation has a non-canonical release path"
            )
        release_input = self.instance / raw_release
        self._reject_symlink_chain(
            release_input, label="generation release path"
        )
        release = release_input.resolve(strict=False)
        releases_root = self.releases.resolve(strict=False)
        if (
            releases_root not in release.parents
            or not release.is_dir()
        ):
            raise GenerationError("generation release path escaped the release store")
        main = release / "main.py"
        if main.is_symlink() or not main.is_file():
            raise GenerationError("generation has no main.py")
        raw_environment_value = value["environment_path"]
        if raw_environment_value:
            raw_environment = Path(raw_environment_value)
            if (
                not raw_environment.is_absolute()
                and (
                    ".." in raw_environment.parts
                    or raw_environment.parts[:2] != (".silicon", "environments")
                )
            ):
                raise GenerationError(
                    "generation has a non-canonical environment path"
                )
            environment_input = (
                raw_environment
                if raw_environment.is_absolute()
                else self.instance / raw_environment
            )
            self._reject_symlink_chain(
                environment_input, label="generation environment path"
            )
            if not environment_input.is_dir():
                raise GenerationError(
                    "generation dependency environment is unavailable"
                )

    def activate(
        self,
        generation: dict[str, Any],
        *,
        previous: dict[str, Any] | None = None,
        allow_release_rollback: bool = False,
    ) -> dict[str, Any]:
        value = dict(generation)
        value.update(
            {
                "schema": 1,
                "kind": "immutable-release",
                "activated_at": time.time(),
            }
        )
        self.validate(value)
        with self._release_floor_lock():
            previous = self.current() if previous is None else previous
            self.validate(previous)
            self._enforce_release_floor(
                value.get("release"),
                allow_release_rollback=allow_release_rollback,
            )
            self._record_release_floor_unlocked(previous.get("release"))
            self._record_release_floor_unlocked(value.get("release"))
            if not self._managed():
                atomic_write_json(
                    self.managed_marker,
                    {"schema": 1, "managed_at": time.time()},
                )
            atomic_write_json(self.pointer, value)
        return previous

    def restore(self, generation: dict[str, Any]) -> None:
        self.validate(generation)
        with self._release_floor_lock():
            self._record_release_floor_unlocked(generation.get("release"))
            if generation.get("kind") == "legacy-flat":
                if self._managed():
                    atomic_write_json(self.pointer, generation)
                else:
                    self.pointer.unlink(missing_ok=True)
                    fsync_dir(self.pointer.parent)
                return
            atomic_write_json(self.pointer, generation)

    def active_root(self) -> Path:
        return self.resolve_release(self.current())

    def resolve_release(self, generation: dict[str, Any]) -> Path:
        value = Path(str(generation.get("release_path", "")))
        if not value.is_absolute():
            value = self.instance / value
        self._reject_symlink_chain(value, label="generation release path")
        return value.resolve(strict=False)

    def resolve_environment(self, generation: dict[str, Any]) -> Path | None:
        raw = str(generation.get("environment_path", ""))
        if not raw:
            return None
        value = Path(raw)
        if not value.is_absolute():
            value = self.instance / value
        self._reject_symlink_chain(value, label="generation environment path")
        try:
            return value.resolve(strict=True)
        except OSError as exc:
            raise GenerationError(
                "generation dependency environment is unavailable"
            ) from exc
