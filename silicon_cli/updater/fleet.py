"""Crash-safe host journal for rolling multi-Silicon updates."""
from __future__ import annotations

import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from ..host_lock import HostFileLock, ensure_private_directory
from .io import atomic_write_json, read_json
from .release import (
    ReleaseIdentity,
    ReleaseVerificationError,
    runtime_image_is_pinned,
)

FLEET_ID_RE = re.compile(r"\Afleet-[A-Za-z0-9._-]{1,120}\Z")
ACTIVE_STATES = {"ACTIVATING", "COMPENSATING", "NEEDS_ATTENTION"}
TERMINAL_STATES = {"COMMITTED", "COMPENSATED"}
MEMBER_STATES = {
    "pending",
    "activating",
    "committed",
    "compensating",
    "compensated",
    "failed",
}
MAX_FLEET_BYTES = 4 * 1024 * 1024
MAX_FLEET_MEMBERS = 1024


class FleetJournalError(RuntimeError):
    """Fleet rollout state is corrupt or inconsistent."""


class FleetJournal:
    def __init__(self, root: Path, value: dict[str, Any]):
        self.root = Path(root)
        self.directory = self.root / "update-fleets"
        self.lock_path = self.root / "update-fleets.lock"
        self.value = value
        self.path = self.directory / f"{value['fleet_id']}.json"

    @staticmethod
    def new_id() -> str:
        return f"fleet-{int(time.time())}-{os.getpid()}-{time.time_ns()}"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        release: dict[str, Any],
        runtime_image: str,
        members: list[dict[str, str]],
    ) -> "FleetJournal":
        root = ensure_private_directory(Path(root))
        now = time.time()
        journal = cls(
            root,
            {
                "schema": 1,
                "fleet_id": cls.new_id(),
                "state": "ACTIVATING",
                "created_at": now,
                "updated_at": now,
                "release": release,
                "runtime_image": runtime_image,
                "members": [
                    {
                        "name": str(member["name"]),
                        "path": str(
                            Path(member["path"])
                            .expanduser()
                            .resolve(strict=False)
                        ),
                        "state": "pending",
                        "update_transaction_id": "",
                        "rollback_transaction_id": "",
                        "error": "",
                    }
                    for member in members
                ],
            },
        )
        journal._validate(journal.value)
        with HostFileLock(journal.lock_path):
            if cls.active(root) is not None:
                raise FleetJournalError(
                    "an incomplete fleet update must be reconciled first"
                )
            ensure_private_directory(journal.directory)
            journal._save_unlocked()
        return journal

    @classmethod
    def load(cls, path: Path, *, root: Path) -> "FleetJournal":
        path = Path(path)
        root = Path(root)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise FleetJournalError(
                f"could not inspect fleet journal {path}: {exc}"
            ) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_FLEET_BYTES
        ):
            raise FleetJournalError(f"fleet journal is unsafe: {path}")
        try:
            value = read_json(path)
        except (OSError, ValueError) as exc:
            raise FleetJournalError(
                f"fleet journal is unreadable: {path}"
            ) from exc
        cls._validate(value, expected_id=path.stem)
        journal = cls(root, value)
        if journal.path.resolve(strict=False) != path.resolve(strict=False):
            raise FleetJournalError(
                "fleet journal identity does not match its path"
            )
        return journal

    @classmethod
    def history(cls, root: Path) -> list["FleetJournal"]:
        root = Path(root)
        directory = root / "update-fleets"
        if not directory.exists() and not directory.is_symlink():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise FleetJournalError("fleet journal directory is unsafe")
        result = [cls.load(path, root=root) for path in directory.glob("*.json")]
        result.sort(key=lambda item: float(item.value["updated_at"]), reverse=True)
        return result

    @classmethod
    def active(cls, root: Path) -> "FleetJournal | None":
        active = [
            journal
            for journal in cls.history(root)
            if journal.value["state"] in ACTIVE_STATES
        ]
        if len(active) > 1:
            raise FleetJournalError(
                "multiple incomplete fleet journals require operator repair"
            )
        return active[0] if active else None

    @classmethod
    def _validate(
        cls, value: object, *, expected_id: str | None = None
    ) -> None:
        required = {
            "schema",
            "fleet_id",
            "state",
            "created_at",
            "updated_at",
            "release",
            "runtime_image",
            "members",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise FleetJournalError(
                "fleet journal has unknown or missing fields"
            )
        fleet_id = value.get("fleet_id")
        state = value.get("state")
        if (
            value.get("schema") != 1
            or not isinstance(fleet_id, str)
            or FLEET_ID_RE.fullmatch(fleet_id) is None
            or (expected_id is not None and fleet_id != expected_id)
            or not isinstance(state, str)
            or state not in ACTIVE_STATES | TERMINAL_STATES
        ):
            raise FleetJournalError("fleet journal identity or state is invalid")
        created = value.get("created_at")
        updated = value.get("updated_at")
        if any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            or float(item) <= 0
            for item in (created, updated)
        ) or float(updated) < float(created):
            raise FleetJournalError("fleet journal timestamps are invalid")
        try:
            ReleaseIdentity.from_dict(value.get("release"))
        except ReleaseVerificationError as exc:
            raise FleetJournalError(
                f"fleet target release is invalid: {exc}"
            ) from exc
        runtime_image = value.get("runtime_image")
        if (
            not isinstance(runtime_image, str)
            or (runtime_image and not runtime_image_is_pinned(runtime_image))
        ):
            raise FleetJournalError("fleet runtime image is invalid")
        members = value.get("members")
        if (
            not isinstance(members, list)
            or not 2 <= len(members) <= MAX_FLEET_MEMBERS
        ):
            raise FleetJournalError("fleet member list is invalid")
        names: set[str] = set()
        paths: set[str] = set()
        member_fields = {
            "name",
            "path",
            "state",
            "update_transaction_id",
            "rollback_transaction_id",
            "error",
        }
        for member in members:
            if not isinstance(member, dict) or set(member) != member_fields:
                raise FleetJournalError("fleet member record is invalid")
            name = member.get("name")
            path = member.get("path")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 128
                or not isinstance(path, str)
                or not Path(path).is_absolute()
                or "\x00" in path
                or not isinstance(member.get("state"), str)
                or member["state"] not in MEMBER_STATES
                or any(
                    not isinstance(member.get(field), str)
                    or len(member[field]) > 4096
                    for field in (
                        "update_transaction_id",
                        "rollback_transaction_id",
                        "error",
                    )
                )
            ):
                raise FleetJournalError("fleet member values are invalid")
            normalized_path = str(Path(path).resolve(strict=False))
            if name in names or normalized_path in paths:
                raise FleetJournalError("fleet journal has duplicate members")
            names.add(name)
            paths.add(normalized_path)

    def _save_unlocked(self) -> None:
        self.value["updated_at"] = time.time()
        self._validate(self.value, expected_id=self.path.stem)
        atomic_write_json(self.path, self.value)

    def save(self) -> None:
        with HostFileLock(self.lock_path):
            self._save_unlocked()

    def set_state(self, state: str) -> None:
        if state not in ACTIVE_STATES | TERMINAL_STATES:
            raise FleetJournalError(f"invalid fleet state: {state}")
        current = self.value["state"]
        if current in TERMINAL_STATES and state != current:
            raise FleetJournalError(
                f"terminal fleet journal cannot transition from {current}"
            )
        self.value["state"] = state
        self.save()

    def member(self, index: int, **values: str) -> None:
        record = self.value["members"][index]
        unknown = set(values) - {
            "state",
            "update_transaction_id",
            "rollback_transaction_id",
            "error",
        }
        if unknown:
            raise FleetJournalError(
                "unknown fleet member fields: " + ", ".join(sorted(unknown))
            )
        record.update(values)
        self.save()
