"""Crash-safe host journal and staged filesystem commit for ``silicon pull``."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .host_lock import ensure_private_directory
from .updater.io import atomic_write_json, fsync_dir, read_json

SCHEMA = 1
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
TRANSACTION_ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
INSTANCE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,119}\Z")
STATES = {
    "INIT",
    "PLANNED",
    "STAGING",
    "STAGED",
    "COMMITTING",
    "RENAMED",
    "CLAIM_COMMITTED",
    "POSTCOMMIT",
    "COMPLETE",
    "ABORTED",
}
TERMINAL_STATES = {"COMPLETE", "ABORTED"}
ITEM_FIELDS = {
    "silicon_id",
    "silicon_name",
    "name",
    "final_path",
    "stage_path",
    "setup_config",
    "staged",
    "renamed",
    "registered",
    "interface_attempted",
    "started",
    "backup_attempted",
}
PROVIDER_METADATA_KEYS = {
    "SILICON_PROVIDER_KEYS_SOURCE",
    "SILICON_PROVIDER_KEYS_TEAM",
    "SILICON_PROVIDER_KEYS",
}
ROOT_FIELDS = {
    "schema",
    "transaction_id",
    "kind",
    "server",
    "credential_fingerprint",
    "state",
    "created_at",
    "updated_at",
    "team_name",
    "runtime",
    "runtime_image",
    "release_tree_sha256",
    "environment_path",
    "backups",
    "provider_key_env",
    "items",
}


class PullJournalError(RuntimeError):
    pass


def credential_fingerprint(server: str, credential: str) -> str:
    return hashlib.sha256(
        b"silicon-pull-credential-v1\0"
        + server.encode("utf-8")
        + b"\0"
        + credential.encode("utf-8")
    ).hexdigest()


def _timestamp(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise PullJournalError(f"pull journal has invalid {label}")
    return float(value)


def _validate_setup_config(value: object) -> None:
    if not isinstance(value, dict) or len(value) > 32:
        raise PullJournalError("pull journal has an invalid setup configuration")
    if len(str(value)) > 64 * 1024:
        raise PullJournalError("pull journal setup configuration is too large")


def _validate_item(item: object, transaction_id: str) -> None:
    if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
        raise PullJournalError("pull journal has an invalid item")
    for field in (
        "staged",
        "renamed",
        "registered",
        "interface_attempted",
        "started",
        "backup_attempted",
    ):
        if not isinstance(item[field], bool):
            raise PullJournalError("pull journal has an invalid item state")
    for field in ("silicon_id", "silicon_name", "name", "final_path", "stage_path"):
        if not isinstance(item[field], str) or "\x00" in item[field]:
            raise PullJournalError("pull journal has an invalid item identity")
    if (
        not item["silicon_id"]
        or len(item["silicon_id"]) > 256
        or len(item["silicon_name"]) > 512
        or INSTANCE_NAME_RE.fullmatch(item["name"]) is None
    ):
        raise PullJournalError("pull journal has an invalid item identity")
    final = Path(item["final_path"])
    stage = Path(item["stage_path"])
    expected_stage = f".{item['name']}.silicon-pull-{transaction_id[:12]}"
    if (
        not final.is_absolute()
        or not stage.is_absolute()
        or final.name != item["name"]
        or stage.name != expected_stage
        or final.parent != stage.parent
        or final == stage
    ):
        raise PullJournalError("pull journal has an unsafe target path")
    _validate_setup_config(item["setup_config"])
    if item["renamed"] and not item["staged"]:
        raise PullJournalError("pull journal renamed an unstaged item")
    if item["started"] and not item["registered"]:
        raise PullJournalError("pull journal started an unregistered item")


def _validate(value: object, *, expected_id: str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != ROOT_FIELDS:
        raise PullJournalError("pull journal has unknown or missing fields")
    if value.get("schema") != SCHEMA:
        raise PullJournalError("unsupported pull journal schema")
    transaction_id = value.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or TRANSACTION_ID_RE.fullmatch(transaction_id) is None
        or (expected_id is not None and transaction_id != expected_id)
    ):
        raise PullJournalError("pull journal has an invalid transaction identity")
    if value.get("kind") not in {"single", "team"}:
        raise PullJournalError("pull journal has an invalid kind")
    for field, maximum in (
        ("server", 2048),
        ("credential_fingerprint", 64),
        ("team_name", 512),
        ("runtime_image", 1024),
        ("release_tree_sha256", 64),
        ("environment_path", 4096),
    ):
        field_value = value.get(field)
        if (
            not isinstance(field_value, str)
            or "\x00" in field_value
            or len(field_value) > maximum
        ):
            raise PullJournalError(f"pull journal has an invalid {field}")
    if not re.fullmatch(r"[a-f0-9]{64}", value["credential_fingerprint"]):
        raise PullJournalError("pull journal has an invalid credential fingerprint")
    if value["release_tree_sha256"] and not re.fullmatch(
        r"[a-f0-9]{64}", value["release_tree_sha256"]
    ):
        raise PullJournalError("pull journal has an invalid release identity")
    if value["environment_path"] and not Path(
        value["environment_path"]
    ).is_absolute():
        raise PullJournalError("pull journal has an invalid environment path")
    if value.get("state") not in STATES:
        raise PullJournalError("pull journal has an invalid state")
    created = _timestamp(value.get("created_at"), "creation time")
    updated = _timestamp(value.get("updated_at"), "update time")
    if updated < created:
        raise PullJournalError("pull journal update predates creation")
    if value.get("runtime") not in {"", "local", "docker"}:
        raise PullJournalError("pull journal has an invalid runtime")
    if not isinstance(value.get("backups"), bool):
        raise PullJournalError("pull journal has an invalid backup choice")
    provider_env = value.get("provider_key_env")
    if (
        not isinstance(provider_env, dict)
        or not set(provider_env).issubset(PROVIDER_METADATA_KEYS)
    ):
        raise PullJournalError("pull journal has invalid provider metadata")
    for key, item in provider_env.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or len(key) > 128
            or len(item) > 4096
            or "\x00" in key + item
        ):
            raise PullJournalError("pull journal has invalid provider metadata")
    if (
        "SILICON_PROVIDER_KEYS_SOURCE" in provider_env
        and provider_env["SILICON_PROVIDER_KEYS_SOURCE"] != "glass"
    ):
        raise PullJournalError("pull journal has invalid provider metadata")
    key_names = provider_env.get("SILICON_PROVIDER_KEYS", "")
    if key_names and any(
        re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) is None
        for name in key_names.split(",")
    ):
        raise PullJournalError("pull journal has invalid provider metadata")
    items = value.get("items")
    if not isinstance(items, list) or len(items) > 256:
        raise PullJournalError("pull journal has an invalid item list")
    seen_names: set[str] = set()
    seen_silicons: set[str] = set()
    seen_paths: set[str] = set()
    for item in items:
        _validate_item(item, transaction_id)
        if (
            item["name"] in seen_names
            or item["silicon_id"] in seen_silicons
            or item["final_path"] in seen_paths
            or item["stage_path"] in seen_paths
        ):
            raise PullJournalError("pull journal contains duplicate targets")
        seen_names.add(item["name"])
        seen_silicons.add(item["silicon_id"])
        seen_paths.update({item["final_path"], item["stage_path"]})
    if value["state"] not in {"INIT", "ABORTED"} and not items:
        raise PullJournalError("planned pull journal has no targets")
    return value


@dataclass
class PullJournal:
    path: Path
    value: dict[str, Any]

    @property
    def transaction_id(self) -> str:
        return self.value["transaction_id"]

    @property
    def state(self) -> str:
        return self.value["state"]

    @property
    def items(self) -> list[dict]:
        return self.value["items"]

    @classmethod
    def open_or_create(
        cls,
        root: Path,
        *,
        kind: str,
        server: str,
        credential: str,
    ) -> "PullJournal":
        directory = ensure_private_directory(Path(root) / "pull-transactions")
        fingerprint = credential_fingerprint(server, credential)
        matches: list[PullJournal] = []
        for path in sorted(directory.glob("*.json")):
            journal = cls.load(path)
            if (
                journal.state not in TERMINAL_STATES
                and journal.value["kind"] == kind
                and journal.value["server"] == server
                and journal.value["credential_fingerprint"] == fingerprint
            ):
                matches.append(journal)
        if len(matches) > 1:
            raise PullJournalError(
                "multiple unfinished pulls match this credential; refusing to guess"
            )
        if matches:
            return matches[0]

        now = time.time()
        transaction_id = uuid.uuid4().hex
        path = directory / f"{transaction_id}.json"
        journal = cls(
            path,
            {
                "schema": SCHEMA,
                "transaction_id": transaction_id,
                "kind": kind,
                "server": server,
                "credential_fingerprint": fingerprint,
                "state": "INIT",
                "created_at": now,
                "updated_at": now,
                "team_name": "",
                "runtime": "",
                "runtime_image": "",
                "release_tree_sha256": "",
                "environment_path": "",
                "backups": False,
                "provider_key_env": {},
                "items": [],
            },
        )
        journal.save()
        return journal

    @classmethod
    def load(cls, path: Path) -> "PullJournal":
        path = Path(path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise PullJournalError(f"could not inspect pull journal: {path}") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_JOURNAL_BYTES
        ):
            raise PullJournalError(f"pull journal is unsafe: {path}")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PullJournalError(f"pull journal is not owner-only: {path}")
        try:
            value = read_json(path)
        except (OSError, ValueError) as exc:
            raise PullJournalError(f"could not read pull journal: {path}") from exc
        _validate(value, expected_id=path.stem)
        return cls(path, value)

    def save(self) -> None:
        self.value["updated_at"] = time.time()
        _validate(self.value, expected_id=self.path.stem)
        atomic_write_json(self.path, self.value, mode=0o600)

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise PullJournalError(f"invalid pull state: {state}")
        if self.state in TERMINAL_STATES and state != self.state:
            raise PullJournalError("cannot change a terminal pull transaction")
        self.value["state"] = state
        self.save()

    def initialize(
        self,
        *,
        team_name: str,
        runtime: str,
        runtime_image: str,
        release_tree_sha256: str,
        environment_path: str,
        backups: bool,
        provider_key_env: dict[str, str],
        items: list[dict],
    ) -> None:
        if self.state != "INIT" or self.items:
            raise PullJournalError("pull transaction is already planned")
        self.value.update(
            {
                "team_name": str(team_name)[:512],
                "runtime": runtime,
                "runtime_image": runtime_image,
                "release_tree_sha256": release_tree_sha256,
                "environment_path": environment_path,
                "backups": bool(backups),
                "provider_key_env": dict(provider_key_env),
                "items": items,
                "state": "PLANNED",
            }
        )
        self.save()

    def update_item(self, index: int, **updates: bool) -> None:
        item = self.items[index]
        for field, value in updates.items():
            if field not in ITEM_FIELDS or not isinstance(value, bool):
                raise PullJournalError("invalid pull item update")
            item[field] = value
        self.save()

    def marker_value(self, item: dict) -> dict:
        return {
            "schema": 1,
            "transaction_id": self.transaction_id,
            "credential_fingerprint": self.value["credential_fingerprint"],
            "silicon_id": item["silicon_id"],
            "name": item["name"],
        }

    @staticmethod
    def marker_path(root: Path) -> Path:
        return root / ".silicon" / "pull-transaction.json"

    def write_stage_marker(self, item: dict) -> None:
        stage = Path(item["stage_path"])
        state_root = stage / ".silicon"
        state_root.mkdir(mode=0o700, exist_ok=True)
        metadata = state_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PullJournalError(f"pull state directory is unsafe: {state_root}")
        if os.name != "nt":
            os.chmod(state_root, 0o700)
        atomic_write_json(
            self.marker_path(stage),
            self.marker_value(item),
            mode=0o600,
        )

    def verify_marker(self, root: Path, item: dict) -> None:
        marker = self.marker_path(root)
        try:
            root_metadata = root.lstat()
            metadata = marker.lstat()
            value = read_json(marker)
        except (OSError, ValueError) as exc:
            raise PullJournalError(
                f"pull target has no authenticated transaction marker: {root}"
            ) from exc
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or (
                os.name != "nt"
                and stat.S_IMODE(root_metadata.st_mode) & 0o077
            )
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > 64 * 1024
            or (
                os.name != "nt"
                and stat.S_IMODE(metadata.st_mode) & 0o077
            )
            or value != self.marker_value(item)
        ):
            raise PullJournalError(
                f"pull target has an invalid transaction marker: {root}"
            )

    def prepare_stage(self, item: dict) -> Path:
        stage = Path(item["stage_path"])
        final = Path(item["final_path"])
        if final.exists() or final.is_symlink():
            raise PullJournalError(f"pull target already exists: {final}")
        if not stage.exists():
            os.mkdir(stage, 0o700)
            fsync_dir(stage.parent)
        metadata = stage.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PullJournalError(f"pull stage is unsafe: {stage}")
        if os.name != "nt":
            os.chmod(stage, 0o700)
        marker = self.marker_path(stage)
        if marker.exists() or any(stage.iterdir()):
            self.verify_marker(stage, item)
        else:
            self.write_stage_marker(item)
        return stage

    def reconcile_and_commit(self) -> None:
        # First verify every target so no rename occurs if any member is
        # missing, colliding, or from another transaction.
        for index, item in enumerate(self.items):
            stage = Path(item["stage_path"])
            final = Path(item["final_path"])
            stage_exists = stage.exists() or stage.is_symlink()
            final_exists = final.exists() or final.is_symlink()
            if stage_exists and final_exists:
                raise PullJournalError(
                    f"both pull stage and final target exist: {final}"
                )
            if final_exists:
                self.verify_marker(final, item)
                if not item["renamed"]:
                    self.update_item(index, renamed=True)
                continue
            if not stage_exists:
                raise PullJournalError(f"pull stage disappeared: {stage}")
            self.verify_marker(stage, item)
            if not item["staged"]:
                raise PullJournalError(f"pull stage was not verified: {stage}")

        if self.state not in {"COMMITTING", "RENAMED"}:
            self.set_state("COMMITTING")
        for index, item in enumerate(self.items):
            if item["renamed"]:
                continue
            stage = Path(item["stage_path"])
            final = Path(item["final_path"])
            if final.exists() or final.is_symlink():
                self.verify_marker(final, item)
            else:
                os.rename(stage, final)
                fsync_dir(final.parent)
                self.verify_marker(final, item)
            self.update_item(index, renamed=True)
        self.set_state("RENAMED")

    def cleanup_precommit(self, *, mark_aborted: bool = True) -> None:
        if any(bool(item["renamed"]) for item in self.items):
            raise PullJournalError("cannot clean a pull after final rename began")
        for item in self.items:
            stage = Path(item["stage_path"])
            if not (stage.exists() or stage.is_symlink()):
                continue
            if stage.is_symlink() or not stage.is_dir():
                raise PullJournalError(f"refusing to remove unsafe pull stage: {stage}")
            if any(stage.iterdir()):
                self.verify_marker(stage, item)
            shutil.rmtree(stage)
            fsync_dir(stage.parent)
            item["staged"] = False
        if mark_aborted:
            self.set_state("ABORTED")
        else:
            self.value["state"] = "PLANNED"
            self.save()


def planned_item(
    *,
    silicon_id: str,
    silicon_name: str,
    name: str,
    parent: Path,
    transaction_id: str,
    setup_config: dict,
) -> dict:
    parent = Path(parent).resolve(strict=True)
    final = (parent / name).resolve(strict=False)
    stage = parent / f".{name}.silicon-pull-{transaction_id[:12]}"
    return {
        "silicon_id": silicon_id,
        "silicon_name": silicon_name,
        "name": name,
        "final_path": str(final),
        "stage_path": str(stage),
        "setup_config": dict(setup_config),
        "staged": False,
        "renamed": False,
        "registered": False,
        "interface_attempted": False,
        "started": False,
        "backup_attempted": False,
    }
