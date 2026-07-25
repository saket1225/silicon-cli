"""Durable update transaction journal and state machine."""
from __future__ import annotations

import math
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .io import atomic_write_json, read_json

TERMINAL_STATES = {"COMMITTED", "CANCELLED", "ROLLED_BACK", "FAILED"}
ORDERED_STATES = [
    "CREATED",
    "RESOLVED",
    "STAGED",
    "PLANNED",
    "DEPENDENCIES_READY",
    "DRAIN_REQUESTED",
    "QUIESCENT",
    "CHECKPOINTED",
    "STOPPING",
    "STOPPED",
    "ACTIVATED",
    "STARTED",
    "VALIDATED",
    "COMMITTED",
]
ALL_STATES = set(ORDERED_STATES) | TERMINAL_STATES
TRANSACTION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_EVENTS = 10_000


class InvalidTransition(RuntimeError):
    pass


class JournalCorruption(RuntimeError):
    """Durable updater state is malformed or cannot be trusted."""


class FailpointCrash(BaseException):
    """Tests use this to model abrupt process death after a durable phase."""


class TransactionJournal:
    def __init__(self, instance: Path, value: dict[str, Any]):
        self.instance = Path(instance)
        self.value = value
        self.path = (
            self.instance
            / ".silicon"
            / "transactions"
            / f"{self.value['transaction_id']}.json"
        )

    @classmethod
    def new_id(cls) -> str:
        return f"{int(time.time())}-{uuid.uuid4().hex[:12]}"

    @classmethod
    def create(
        cls,
        instance: Path,
        metadata: dict[str, Any] | None = None,
        *,
        transaction_id: str | None = None,
    ) -> "TransactionJournal":
        transaction_id = transaction_id or cls.new_id()
        now = time.time()
        journal = cls(
            instance,
            {
                "schema": 1,
                "transaction_id": transaction_id,
                "state": "CREATED",
                "created_at": now,
                "updated_at": now,
                "metadata": metadata or {},
                "events": [
                    {"state": "CREATED", "at": now, "detail": "transaction created"}
                ],
            },
        )
        journal._save()
        return journal

    @classmethod
    def load(cls, path: Path) -> "TransactionJournal":
        path = Path(path)
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise JournalCorruption(
                f"could not inspect transaction journal {path}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise JournalCorruption(
                f"transaction journal is not a confined regular file: {path}"
            )
        if metadata.st_size <= 0 or metadata.st_size > MAX_JOURNAL_BYTES:
            raise JournalCorruption(
                f"transaction journal has an invalid size: {path}"
            )
        try:
            value = read_json(path)
        except (OSError, ValueError) as exc:
            raise JournalCorruption(
                f"could not read transaction journal {path}: {exc}"
            ) from exc
        cls._validate_value(value, expected_id=path.stem)
        journal = cls(path.parent.parent.parent, value)
        if journal.path.resolve(strict=False) != path.resolve(strict=False):
            raise JournalCorruption(
                f"transaction journal identity does not match its path: {path}"
            )
        return journal

    @staticmethod
    def _timestamp(value: object, label: str) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise JournalCorruption(f"transaction journal has invalid {label}")
        return float(value)

    @classmethod
    def _validate_value(
        cls, value: object, *, expected_id: str | None = None
    ) -> None:
        required = {
            "schema",
            "transaction_id",
            "state",
            "created_at",
            "updated_at",
            "metadata",
            "events",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise JournalCorruption(
                "transaction journal has unknown or missing fields"
            )
        if value.get("schema") != 1:
            raise JournalCorruption("unsupported transaction journal schema")
        transaction_id = value.get("transaction_id")
        if (
            not isinstance(transaction_id, str)
            or TRANSACTION_ID_RE.fullmatch(transaction_id) is None
            or (expected_id is not None and transaction_id != expected_id)
        ):
            raise JournalCorruption(
                "transaction journal identity does not match its filename"
            )
        state = value.get("state")
        if not isinstance(state, str) or state not in ALL_STATES:
            raise JournalCorruption("transaction journal has an invalid state")
        created_at = cls._timestamp(value.get("created_at"), "creation time")
        updated_at = cls._timestamp(value.get("updated_at"), "update time")
        if updated_at < created_at:
            raise JournalCorruption(
                "transaction journal update time predates its creation"
            )
        if not isinstance(value.get("metadata"), dict):
            raise JournalCorruption(
                "transaction journal metadata must be an object"
            )
        events = value.get("events")
        if (
            not isinstance(events, list)
            or not events
            or len(events) > MAX_JOURNAL_EVENTS
        ):
            raise JournalCorruption(
                "transaction journal has an invalid event history"
            )
        previous = ""
        previous_at = created_at
        for index, event in enumerate(events):
            if not isinstance(event, dict) or set(event) != {
                "state",
                "at",
                "detail",
            }:
                raise JournalCorruption(
                    "transaction journal contains an invalid event"
                )
            event_state = event.get("state")
            if (
                not isinstance(event_state, str)
                or event_state not in ALL_STATES
                or not isinstance(event.get("detail"), str)
                or len(event["detail"]) > 64 * 1024
            ):
                raise JournalCorruption(
                    "transaction journal contains invalid event values"
                )
            event_at = cls._timestamp(event.get("at"), "event time")
            if event_at < previous_at or event_at > updated_at:
                raise JournalCorruption(
                    "transaction journal event timestamps are inconsistent"
                )
            if index == 0:
                if event_state != "CREATED":
                    raise JournalCorruption(
                        "transaction journal does not begin in CREATED"
                    )
            elif previous in TERMINAL_STATES:
                raise JournalCorruption(
                    "transaction journal contains events after a terminal state"
                )
            elif event_state in TERMINAL_STATES:
                pass
            else:
                try:
                    if (
                        ORDERED_STATES.index(event_state)
                        != ORDERED_STATES.index(previous) + 1
                    ):
                        raise JournalCorruption(
                            "transaction journal event order is invalid"
                        )
                except ValueError as exc:
                    raise JournalCorruption(
                        "transaction journal event order is invalid"
                    ) from exc
            previous = event_state
            previous_at = event_at
        if previous != state:
            raise JournalCorruption(
                "transaction journal state disagrees with its final event"
            )

    @classmethod
    def history(cls, instance: Path) -> list["TransactionJournal"]:
        directory = Path(instance) / ".silicon" / "transactions"
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise JournalCorruption(
                f"transaction journal directory is unsafe: {directory}"
            )
        journals = []
        for path in directory.glob("*.json"):
            journals.append(cls.load(path))
        journals.sort(
            key=lambda item: float(item.value.get("updated_at", 0)),
            reverse=True,
        )
        return journals

    @property
    def transaction_id(self) -> str:
        return str(self.value["transaction_id"])

    @property
    def state(self) -> str:
        return str(self.value["state"])

    @property
    def metadata(self) -> dict[str, Any]:
        return self.value.setdefault("metadata", {})

    def _save(self) -> None:
        self.value["updated_at"] = time.time()
        self._validate_value(
            self.value, expected_id=self.path.stem
        )
        atomic_write_json(self.path, self.value)

    def merge_metadata(self, **values: Any) -> None:
        self.metadata.update(values)
        self._save()

    def transition(
        self,
        state: str,
        detail: str = "",
        *,
        failpoint: str | None = None,
    ) -> None:
        current = self.state
        if current in TERMINAL_STATES:
            raise InvalidTransition(f"transaction already ended in {current}")
        if state in {"FAILED", "CANCELLED", "ROLLED_BACK"}:
            pass
        else:
            try:
                current_index = ORDERED_STATES.index(current)
                new_index = ORDERED_STATES.index(state)
            except ValueError as exc:
                raise InvalidTransition(f"unknown transition {current} -> {state}") from exc
            if new_index != current_index + 1:
                raise InvalidTransition(f"invalid transition {current} -> {state}")
        now = time.time()
        self.value["state"] = state
        self.value.setdefault("events", []).append(
            {"state": state, "at": now, "detail": detail}
        )
        self._save()
        if failpoint == state:
            raise FailpointCrash(f"injected crash after {state}")

    def request_cancel(self) -> None:
        marker = self.path.with_suffix(".cancel")
        atomic_write_json(
            marker,
            {
                "schema": 1,
                "transaction_id": self.transaction_id,
                "requested_at": time.time(),
            },
        )

    def cancellation_requested(self) -> bool:
        return self.path.with_suffix(".cancel").exists()

    def clear_cancel(self) -> None:
        self.path.with_suffix(".cancel").unlink(missing_ok=True)
