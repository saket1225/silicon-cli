"""Task drain coordination plus a durable public Glass maintenance lease."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import certifi

from .io import atomic_write_json, read_json


class MaintenanceError(RuntimeError):
    pass


class MaintenanceTimeout(MaintenanceError):
    pass


class TransientMaintenanceError(MaintenanceError):
    """A Glass transport failure that is safe to retry idempotently."""


TERMINAL_GLASS_PHASES = {"idle", "rolled_back", "deferred", "failed"}
MAX_GLASS_RESPONSE_BYTES = 1024 * 1024
MAX_GLASS_ERROR_BYTES = 64 * 1024
GLASS_TRANSITION_ATTEMPTS = 4


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    return (
        scheme,
        (parsed.hostname or "").lower().rstrip("."),
        parsed.port or (443 if scheme == "https" else 80),
    )


class _PinnedOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str, int]):
        super().__init__()
        self.origin = origin

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        destination = urllib.parse.urljoin(request.full_url, new_url)
        if _origin(destination) != self.origin:
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "refusing cross-origin maintenance redirect",
                headers,
                fp,
            )
        return super().redirect_request(
            request, fp, code, msg, headers, destination
        )


def _validated_server(value: str) -> str:
    value = str(value or "").rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    hostname = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower().rstrip(".") == "localhost"
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme.lower() == "https"
            or (parsed.scheme.lower() == "http" and loopback)
        )
    ):
        raise MaintenanceError(
            "refusing to send a Silicon API key to an unsafe Glass URL"
        )
    return value


def _glass_credentials(instance: Path) -> tuple[str, str] | None:
    path = instance / ".glass.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise MaintenanceError(".glass.json is unavailable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise MaintenanceError(".glass.json must be a local regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MaintenanceError("refusing to follow .glass.json") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise MaintenanceError(".glass.json changed while being opened")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise MaintenanceError(".glass.json must contain an object")
    server = _validated_server(str(value.get("server_url") or ""))
    key = str(value.get("api_key") or value.get("silicon_api_key") or "").strip()
    if not key:
        raise MaintenanceError(".glass.json has no Silicon API key")
    return server, key


class GlassMaintenanceLease:
    """Keep the Carbon-visible update projection alive while services are down."""

    def __init__(
        self,
        instance: Path,
        *,
        lease_seconds: int = 120,
        heartbeat_seconds: float = 30.0,
        opener: Callable[..., object] | None = None,
    ):
        self.instance = Path(instance)
        self.credentials = _glass_credentials(self.instance)
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._server_origin = (
            _origin(self.credentials[0]) if self.credentials is not None else None
        )
        self._tls_context = ssl.create_default_context(cafile=certifi.where())
        self._injected_opener = opener
        self._url_opener = (
            urllib.request.build_opener(
                _PinnedOriginRedirectHandler(self._server_origin),
                urllib.request.HTTPSHandler(context=self._tls_context),
            )
            if opener is None and self._server_origin is not None
            else None
        )
        self._update_id = ""
        self._target_version = ""
        self._phase = "idle"
        self._revision = 0
        self._queued_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._pending_path = (
            self.instance / ".silicon" / "maintenance" / "glass-pending.json"
        )

    @property
    def configured(self) -> bool:
        return self.credentials is not None

    def _open_request(self, request: urllib.request.Request):
        if self._injected_opener is not None:
            response = self._injected_opener(
                request,
                timeout=10,
                context=(
                    self._tls_context
                    if request.full_url.startswith("https://")
                    else None
                ),
            )
        else:
            if self._url_opener is None:
                raise MaintenanceError("Glass maintenance transport is unavailable")
            response = self._url_opener.open(request, timeout=10)
        final_url = (
            response.geturl()
            if hasattr(response, "geturl")
            else request.full_url
        )
        if _origin(str(final_url)) != self._server_origin:
            raise MaintenanceError(
                "refusing cross-origin maintenance redirect"
            )
        return response

    def _send(self, phase: str) -> dict:
        if self.credentials is None:
            return {"active": False, "phase": "idle"}
        server, key = self.credentials
        with self._send_lock:
            self._revision += 1
            payload = {
                "update_id": self._update_id,
                "target_version": self._target_version,
                "phase": phase,
                "revision": self._revision,
                "queued_count": self._queued_count,
                "lease_seconds": self.lease_seconds,
            }
            request = urllib.request.Request(
                server + "/api/v1/silicons/me/maintenance",
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                method="PUT",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Silicon-Key": key,
                },
            )
            response = None
            try:
                response = self._open_request(request)
                body = response.read(MAX_GLASS_RESPONSE_BYTES + 1)
                if len(body) > MAX_GLASS_RESPONSE_BYTES:
                    raise MaintenanceError(
                        "Glass maintenance response exceeded the size limit"
                    )
                value = json.loads(body.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(MAX_GLASS_ERROR_BYTES + 1)
                    suffix = ""
                    if len(detail) > MAX_GLASS_ERROR_BYTES:
                        detail = detail[:MAX_GLASS_ERROR_BYTES]
                        suffix = "…"
                    detail_text = detail.decode(
                        "utf-8", errors="replace"
                    ) + suffix
                except Exception:
                    detail_text = str(exc)
                error_type = (
                    TransientMaintenanceError
                    if exc.code in {408, 425, 429} or 500 <= exc.code <= 599
                    else MaintenanceError
                )
                raise error_type(
                    f"Glass maintenance update failed ({exc.code}): "
                    f"{detail_text}"
                ) from exc
            except MaintenanceError:
                raise
            except Exception as exc:
                error_type = (
                    TransientMaintenanceError
                    if isinstance(
                        exc,
                        (urllib.error.URLError, TimeoutError, ConnectionError),
                    )
                    else MaintenanceError
                )
                raise error_type(
                    f"Glass maintenance update failed: {exc}"
                ) from exc
            finally:
                if response is not None and hasattr(response, "close"):
                    response.close()
            if not isinstance(value, dict):
                raise MaintenanceError("Glass returned an invalid maintenance response")
            accepted_id = value.get(
                "accepted_update_id", value.get("update_id")
            )
            accepted_revision = value.get(
                "accepted_revision", value.get("source_revision")
            )
            # Legacy test/self-hosted responders may still echo the submitted
            # source revision as ``revision``. New Glass keeps that distinct
            # from its UI invalidation revision.
            if accepted_revision is None:
                accepted_revision = value.get("revision")
            if (
                value.get("phase") != phase
                or accepted_id != self._update_id
                or accepted_revision != self._revision
            ):
                raise MaintenanceError(
                    "Glass did not accept the requested maintenance transition"
                )
            self._phase = phase
            return value

    def _send_required(self, phase: str) -> dict:
        """Retry only idempotent, explicitly transient Glass transitions."""

        for attempt in range(GLASS_TRANSITION_ATTEMPTS):
            try:
                return self._send(phase)
            except TransientMaintenanceError:
                if attempt + 1 >= GLASS_TRANSITION_ATTEMPTS:
                    raise
                time.sleep(0.25 * (2**attempt))
        raise AssertionError("unreachable Glass transition retry state")

    def _get_projection(self) -> dict:
        if self.credentials is None:
            return {"active": False, "phase": "idle", "revision": 0}
        server, key = self.credentials
        request = urllib.request.Request(
            server + "/api/v1/silicons/me/maintenance",
            method="GET",
            headers={"Accept": "application/json", "X-Silicon-Key": key},
        )
        response = None
        try:
            response = self._open_request(request)
            payload = response.read(MAX_GLASS_RESPONSE_BYTES + 1)
            if len(payload) > MAX_GLASS_RESPONSE_BYTES:
                raise MaintenanceError(
                    "Glass maintenance response exceeded the size limit"
                )
            value = json.loads(payload.decode("utf-8"))
        except MaintenanceError:
            raise
        except Exception as exc:
            raise MaintenanceError(
                f"Glass returned an invalid maintenance response: {exc}"
            ) from exc
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()
        if not isinstance(value, dict):
            raise MaintenanceError("Glass returned an invalid maintenance response")
        return value

    def begin(self, update_id: str, target_version: str) -> None:
        self._update_id = update_id
        self._target_version = target_version[:64]
        if not self.configured:
            return
        self._retry_pending()
        self._send_required("preparing")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"silicon-update-lease-{update_id}",
            daemon=True,
        )
        self._thread.start()

    def reattach(
        self, update_id: str, target_version: str, phase: str
    ) -> None:
        """Resume one lease without moving its public phase backwards."""

        self._update_id = update_id
        self._target_version = target_version[:64]
        self._phase = phase
        if not self.configured:
            return
        try:
            projection = self._get_projection()
            if projection.get("update_id") not in {"", update_id}:
                raise MaintenanceError(
                    "Glass has a different active maintenance lease"
                )
            self._revision = max(
                self._revision,
                int(
                    projection.get(
                        "source_revision", projection.get("revision", 0)
                    )
                ),
            )
            self._send_required(phase)
        except Exception:
            # Local crash recovery must not strand a stopped Silicon merely
            # because Glass is temporarily unreachable. Its old lease will
            # truthfully become status_unknown and this phase remains retryable.
            self._persist_pending(phase)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"silicon-update-lease-{update_id}",
            daemon=True,
        )
        self._thread.start()

    @property
    def update_id(self) -> str:
        return self._update_id

    def adopt(self, update_id: str) -> None:
        """Attach a separate cancel/status process to the durable Glass lease."""

        self._update_id = update_id
        if not self.configured:
            return
        projection = self._get_projection()
        if projection.get("update_id") not in {"", update_id}:
            raise MaintenanceError(
                "Glass has a different active maintenance lease"
            )
        self._target_version = str(projection.get("target_version") or "")[:64]
        self._revision = max(
            self._revision,
            int(
                projection.get(
                    "source_revision", projection.get("revision", 0)
                )
            ),
        )
        self._phase = str(projection.get("phase") or "draining")

    def report(self, phase: str, *, required: bool = True) -> None:
        if not self.configured:
            return
        try:
            self._send_required(phase)
        except MaintenanceError:
            if required:
                raise
            self._persist_pending(phase)

    def set_queued_count(self, count: int) -> None:
        self._queued_count = max(0, int(count))

    def finish(self, outcome: str) -> None:
        if not self.configured:
            return
        if outcome == "committed":
            phases = ("resuming", "idle")
        elif outcome == "rolled_back":
            phases = ("rolled_back",)
        elif outcome == "cancelled":
            phases = ("deferred",)
        else:
            phases = ("failed",)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        final_phase = phases[-1]
        for phase in phases:
            delivered = False
            for attempt in range(3):
                try:
                    self._send(phase)
                    delivered = True
                    break
                except MaintenanceError:
                    if attempt < 2:
                        time.sleep(0.25 * (2**attempt))
            if not delivered:
                # Intermediate notification phases are best effort. Always
                # attempt the terminal state so a failed "resuming" message
                # cannot renew an old active lease and block the next update.
                if phase == final_phase:
                    self._persist_pending(final_phase)
                    if self._injected_opener is None:
                        schedule_pending_terminal_reconciliation(self.instance)
            elif phase == final_phase:
                self._pending_path.unlink(missing_ok=True)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self._send(self._phase)
            except MaintenanceError:
                # The lease naturally becomes status_unknown if connectivity is
                # lost.  Never claim a newer phase that Glass did not accept.
                self._persist_pending(self._phase)

    def _persist_pending(self, phase: str) -> None:
        atomic_write_json(
            self._pending_path,
            {
                "schema": 1,
                "update_id": self._update_id,
                "target_version": self._target_version,
                "phase": phase,
                "revision": self._revision + 1,
                "queued_count": self._queued_count,
                "lease_seconds": self.lease_seconds,
                "created_at": time.time(),
            },
        )

    def _retry_pending(self) -> None:
        if not self._pending_path.exists():
            return
        try:
            self.reconcile_pending_terminal()
        except Exception:
            # A terminal retry must never prevent the next update's own
            # truthful preparing lease.
            pass

    def _load_pending(self) -> dict:
        path = self._pending_path
        if path.is_symlink() or not path.is_file():
            raise MaintenanceError("pending Glass maintenance state is unsafe")
        try:
            metadata = path.stat()
            value = read_json(path)
        except (OSError, ValueError) as exc:
            raise MaintenanceError(
                "pending Glass maintenance state is unreadable"
            ) from exc
        required = {
            "schema",
            "update_id",
            "target_version",
            "phase",
            "revision",
            "queued_count",
            "lease_seconds",
            "created_at",
        }
        if (
            metadata.st_size <= 0
            or metadata.st_size > 64 * 1024
            or not isinstance(value, dict)
            or set(value) != required
            or value.get("schema") != 1
            or not isinstance(value.get("update_id"), str)
            or not value["update_id"]
            or len(value["update_id"]) > 128
            or not isinstance(value.get("target_version"), str)
            or len(value["target_version"]) > 64
            or value.get("phase") not in TERMINAL_GLASS_PHASES
            or not isinstance(value.get("revision"), int)
            or isinstance(value.get("revision"), bool)
            or value["revision"] <= 0
            or not isinstance(value.get("queued_count"), int)
            or isinstance(value.get("queued_count"), bool)
            or value["queued_count"] < 0
            or not isinstance(value.get("lease_seconds"), int)
            or isinstance(value.get("lease_seconds"), bool)
            or not 1 <= value["lease_seconds"] <= 3600
            or not isinstance(value.get("created_at"), (int, float))
            or isinstance(value.get("created_at"), bool)
            or not math.isfinite(float(value["created_at"]))
            or value["created_at"] <= 0
        ):
            raise MaintenanceError("pending Glass maintenance state is invalid")
        return value

    def reconcile_pending_terminal(self) -> bool:
        """Deliver one pending terminal projection without clobbering newer state."""

        if not self.configured or not self._pending_path.exists():
            return True
        pending = self._load_pending()
        projection = self._get_projection()
        remote_id = str(projection.get("update_id") or "")
        remote_revision = projection.get(
            "source_revision", projection.get("revision", 0)
        )
        if (
            not isinstance(remote_revision, int)
            or isinstance(remote_revision, bool)
            or remote_revision < 0
        ):
            raise MaintenanceError(
                "Glass returned an invalid maintenance revision"
            )
        pending_id = str(pending["update_id"])
        pending_revision = int(pending["revision"])
        if remote_id not in {"", pending_id}:
            # A newer update owns the projection. The old terminal retry must
            # never overwrite it.
            self._pending_path.unlink(missing_ok=True)
            return True
        if remote_id == pending_id and remote_revision >= pending_revision:
            if (
                remote_revision == pending_revision
                and projection.get("phase") == pending["phase"]
            ):
                self._pending_path.unlink(missing_ok=True)
                return True
            raise MaintenanceError(
                "Glass already has a newer maintenance revision for this update"
            )

        old_id = self._update_id
        old_target = self._target_version
        old_revision = self._revision
        old_queued = self._queued_count
        try:
            self._update_id = pending_id
            self._target_version = str(pending["target_version"])
            self._revision = pending_revision - 1
            self._queued_count = int(pending["queued_count"])
            self._send(str(pending["phase"]))
            self._pending_path.unlink(missing_ok=True)
            return True
        finally:
            self._update_id = old_id
            self._target_version = old_target
            self._revision = old_revision
            self._queued_count = old_queued


def reconcile_pending_terminal(
    instance: Path,
    *,
    attempts: int = 8,
    delay_seconds: float = 5.0,
) -> bool:
    """Boundedly retry a durable terminal projection after CLI exit or reboot."""

    lease = GlassMaintenanceLease(Path(instance))
    if not lease.configured:
        return True
    for attempt in range(max(1, attempts)):
        try:
            if lease.reconcile_pending_terminal():
                return True
        except MaintenanceError:
            pass
        if attempt + 1 < attempts:
            time.sleep(max(0.0, delay_seconds))
    return False


def schedule_pending_terminal_reconciliation(instance: Path) -> None:
    """Start a detached bounded retry worker when terminal delivery is pending."""

    root = Path(instance).expanduser().resolve(strict=False)
    pending = root / ".silicon" / "maintenance" / "glass-pending.json"
    if not pending.is_file() or pending.is_symlink():
        return
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "silicon_cli.cli",
                "_maintenance_reconcile",
                str(root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        # The durable pending file remains for startup/next-command repair.
        return


class MaintenanceProtocol:
    """Drive the canonical coordinator in the active installed Stemcell."""

    def __init__(
        self,
        instance: Path,
        *,
        command: Callable[[list[str]], dict] | None = None,
        glass: GlassMaintenanceLease | None = None,
        legacy_offline_safe: Callable[[], bool] | None = None,
        poll_interval_seconds: float = 0.25,
    ):
        self.instance = Path(instance)
        self._command = command
        self.glass = glass or GlassMaintenanceLease(self.instance)
        self._legacy_offline_safe = legacy_offline_safe
        self._legacy_offline = False
        self._offline_fence = (
            self.instance
            / ".silicon"
            / "maintenance"
            / "legacy-offline.json"
        )
        self._target_version = ""
        self._epoch: int | None = None
        self._poll_interval_seconds = max(
            0.05, float(poll_interval_seconds)
        )

    @property
    def legacy_offline(self) -> bool:
        return self._legacy_offline

    def _coordinator_available(self) -> bool:
        if self._command is not None:
            return True
        from ..config import active_release_root

        try:
            release = active_release_root(self.instance)
        except (OSError, RuntimeError):
            return False
        coordinator = release / "core" / "maintenance.py"
        return (
            not coordinator.is_symlink()
            and coordinator.is_file()
        )

    def _offline_fence_owner(self) -> str:
        if not self._offline_fence.exists() and not self._offline_fence.is_symlink():
            return ""
        if self._offline_fence.is_symlink() or not self._offline_fence.is_file():
            raise MaintenanceError("legacy offline update fence is unsafe")
        try:
            value = read_json(self._offline_fence)
        except (OSError, ValueError) as exc:
            raise MaintenanceError(
                "legacy offline update fence is corrupt"
            ) from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "update_id", "created_at"}
            or value.get("schema") != 1
            or not isinstance(value.get("update_id"), str)
            or not value["update_id"]
        ):
            raise MaintenanceError("legacy offline update fence is invalid")
        return value["update_id"]

    def _enter_legacy_offline(self, transaction_id: str) -> None:
        owner = self._offline_fence_owner()
        if owner and owner != transaction_id:
            raise MaintenanceError(
                "a different legacy offline update fence is already active"
            )
        if self._legacy_offline_safe is None or not self._legacy_offline_safe():
            raise MaintenanceError(
                "this legacy Stemcell has no task-safe coordinator; stop both "
                "Silicon and its Glass agent first with `silicon stop --full "
                "<name>`, then retry the update"
            )
        self._offline_fence.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self._offline_fence,
            {
                "schema": 1,
                "update_id": transaction_id,
                "created_at": time.time(),
            },
        )
        self._legacy_offline = True

    def _leave_legacy_offline(self, transaction_id: str) -> None:
        owner = self._offline_fence_owner()
        if owner and owner != transaction_id:
            raise MaintenanceError(
                "refusing to clear another update's legacy offline fence"
            )
        if owner == transaction_id:
            self._offline_fence.unlink(missing_ok=True)
        self._legacy_offline = False

    def _run(self, arguments: list[str]) -> dict:
        if self._command is not None:
            return self._command(arguments)
        # Late imports avoid a config/updater import cycle.
        from ..config import (
            active_release_root,
            python_run_cmd,
            runtime_environment,
        )

        release = active_release_root(self.instance)
        result = subprocess.run(
            [
                python_run_cmd(self.instance),
                "-m",
                "core.maintenance",
                "--root",
                str(self.instance),
                *arguments,
            ],
            cwd=release,
            env=runtime_environment(self.instance),
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout.strip().splitlines()
        if result.returncode or not output:
            raise MaintenanceError(
                "the installed Stemcell does not support task-safe maintenance: "
                + (result.stderr.strip() or result.stdout.strip() or "no response")
            )
        try:
            value = json.loads(output[-1])
        except json.JSONDecodeError as exc:
            raise MaintenanceError(
                "the installed Stemcell returned invalid maintenance status"
            ) from exc
        if not isinstance(value, dict) or value.get("error"):
            raise MaintenanceError(str(value.get("error") or "invalid status"))
        return value

    def begin(self, transaction_id: str, target_version: str) -> None:
        self._target_version = target_version
        self.glass.begin(transaction_id, target_version)

    def reattach(
        self, transaction_id: str, target_version: str, phase: str
    ) -> None:
        self._target_version = target_version
        if self._offline_fence_owner() == transaction_id:
            self._legacy_offline = True
        self.glass.reattach(transaction_id, target_version, phase)

    def request_drain(self, transaction_id: str, deadline: float | None) -> None:
        if not self._coordinator_available():
            self._enter_legacy_offline(transaction_id)
            self._epoch = 0
            self.glass.set_queued_count(0)
            self.glass.report("draining")
            return
        remaining = (
            max(0.0, deadline - time.time()) if deadline is not None else None
        )
        arguments = ["request", "--id", transaction_id]
        if remaining is not None:
            arguments.extend(["--deadline", str(remaining)])
        status = self._run(arguments)
        self._epoch = int(status.get("epoch", -1))
        self.glass.set_queued_count(int(status.get("queued_message_count", 0)))
        self.glass.report("draining")

    def await_quiescent(
        self,
        transaction_id: str,
        deadline: float | None,
        cancelled,
        *,
        services_running: bool,
    ) -> None:
        if self._legacy_offline:
            if (
                self._legacy_offline_safe is None
                or not self._legacy_offline_safe()
            ):
                raise MaintenanceError(
                    "legacy Silicon became active after the offline update "
                    "fence; refusing to stop it"
                )
            return
        if not services_running:
            return
        while True:
            if cancelled():
                raise InterruptedError("update cancellation requested")
            status = self._run(["status"])
            self.glass.set_queued_count(int(status.get("queued_message_count", 0)))
            if (
                status.get("maintenance_id") == transaction_id
                and int(status.get("epoch", -2)) == self._epoch
                and status.get("phase") == "draining"
                and status.get("safe_to_stop") is True
                and int(status.get("active_count", 1)) == 0
            ):
                return
            if deadline is not None and time.time() >= deadline:
                raise MaintenanceTimeout(
                    "Silicon did not reach a safe task boundary before the update deadline"
                )
            time.sleep(self._poll_interval_seconds)

    def set_phase(self, transaction_id: str, phase: str, _detail: str) -> None:
        glass_phase = phase
        local_phase = phase
        if phase == "checkpointing":
            local_phase = ""
        elif phase == "rolled_back":
            local_phase = "rolling_back"
            # Glass's rolled_back phase is terminal and must be emitted only
            # after the prior generation is actually running again.
            glass_phase = ""
        if (
            not self._legacy_offline
            and local_phase
            in {"updating", "validating", "rolling_back", "available"}
        ):
            try:
                self._run(["phase", local_phase, "--id", transaction_id])
            except MaintenanceError:
                # A long recovery can outlive its drain deadline. The runtime
                # then expires the owned fence back to available on its own.
                # Recovery has already restored and health-checked the prior
                # generation before publishing ``rolled_back`` here, so this
                # exact owned terminal state is an idempotent success.
                status = self._run(["status"])
                expired_owned_recovery = (
                    phase == "rolled_back"
                    and status.get("maintenance_id") == transaction_id
                    and status.get("phase") == "available"
                    and int(status.get("active_count", 0)) == 0
                )
                if not expired_owned_recovery:
                    raise
        if glass_phase in {
            "draining",
            "checkpointing",
            "updating",
            "validating",
        }:
            self.glass.report(glass_phase)

    def cancel(self, transaction_id: str) -> None:
        owned = self.glass.update_id == transaction_id
        try:
            if not self._legacy_offline:
                self._run(["cancel", "--id", transaction_id])
        finally:
            if self._legacy_offline:
                self._leave_legacy_offline(transaction_id)
            if not owned:
                try:
                    self.glass.adopt(transaction_id)
                except MaintenanceError:
                    pass
                self.glass.finish("cancelled")

    def finish(self, transaction_id: str, outcome: str) -> None:
        try:
            if self._legacy_offline:
                pass
            elif outcome == "committed":
                self._run(["phase", "available", "--id", transaction_id])
            elif outcome == "rolled_back":
                try:
                    self._run(["phase", "rolling_back", "--id", transaction_id])
                except MaintenanceError:
                    pass
                try:
                    self._run(["phase", "available", "--id", transaction_id])
                except MaintenanceError:
                    pass
            elif outcome in {"failed", "cancelled"}:
                try:
                    self._run(["cancel", "--id", transaction_id])
                except MaintenanceError:
                    pass
        finally:
            try:
                if self._legacy_offline:
                    self._leave_legacy_offline(transaction_id)
            finally:
                self.glass.finish(outcome)
