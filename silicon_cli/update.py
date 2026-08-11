"""CLI-owned transactional Silicon updates and CLI self-updates."""
from __future__ import annotations

import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

from . import (
    docker_runtime,
    glassagent,
    interface_cli,
    process,
    registry,
    runtime_contract,
    ui,
)
from .config import (
    REGISTRY_DIR,
    active_release_root,
    python_run_cmd,
    runtime_environment,
)
from .updater import (
    EngineHooks,
    TransactionalUpdater,
    UpdateConflict,
    UpdateError,
)
from .updater.cache import ReleaseCache
from .updater.channel import ReleaseChannelError, fetch_latest_release
from .updater.fleet import FleetJournal, FleetJournalError
from .updater.lock import InstanceLock
from .updater.maintenance import (
    MaintenanceError,
    MaintenanceProtocol,
    MaintenanceTimeout,
)
from .updater.release import FetchedRelease

# A restarted Silicon must satisfy min_uptime AND publish a fresh heartbeat
# before it counts as healthy, and it contacts Glass for provider keys during
# boot. Container start plus that round-trip routinely exceeds ten seconds, so
# a short budget here does not report an unhealthy Silicon -- it reports a slow
# one, fails the recovery, and leaves an interrupted transaction that blocks
# every later update until someone resumes it by hand.
# The runtime writes its readiness heartbeat on the main loop tick (LOOP_TICK,
# 10s). Demanding the heartbeat be under five seconds old therefore asks for
# something true only half the tick, and the gate wants three consecutive
# observations -- so it passed on timing luck and failed outright on a busy
# Silicon whose tick runs long. Allow several ticks, which still catches a
# runtime that has genuinely stopped heartbeating.
# On a severely CPU-starved fleet host the live main loop has been observed to
# go 79-100 seconds between heartbeats. Process birth identity, active
# generation identity, readiness, and consecutive observations are checked
# separately, so accepting up to three minutes of heartbeat delay avoids a
# false rollback without treating a dead or replaced process as healthy.
HEARTBEAT_MAX_AGE_SECONDS = 180.0

HEALTH_BUDGET_SECONDS = 90.0
# A saturated multi-container host can spend more than two minutes between
# container creation and the runtime's first healthy main-loop heartbeat.  A
# five-minute ceiling still fails a genuinely broken boot promptly, while a
# healthy fast boot returns as soon as the gate passes and pays no extra delay.
HEALTH_BUDGET_DOCKER_SECONDS = 300.0
DOCKER_HEALTH_POLL_SECONDS = 2.0
# Fleet workers may drain and checkpoint in parallel, but Docker's control
# plane becomes slower and less reliable when every worker recreates a
# container at once on a shared-volume host. Keep the expensive stop/recreate
# calls bounded independently from fleet concurrency.
DOCKER_CONTROL_CONCURRENCY = 4
_DOCKER_CONTROL_GATE = threading.BoundedSemaphore(
    DOCKER_CONTROL_CONCURRENCY
)
# Rollback preparation also performs heavy tree hashing, snapshot work, and
# fsyncs. Letting more workers than the Docker control plane run concurrently
# saturates the same host volume before they even reach container recreation.
FLEET_COMPENSATION_CONCURRENCY = DOCKER_CONTROL_CONCURRENCY
DEFAULT_FLEET_CONCURRENCY = 8
DEFAULT_FLEET_CANARY_COUNT = 1
MAX_FLEET_CONCURRENCY = 64
# A local runtime that cannot acknowledge its drain promptly is parked so a
# ready Silicon can use the activation slot.  The per-attempt slice makes that
# handoff quick; the total budget prevents an unattended fleet command from
# waiting for hours on one permanently busy member.  Callers can explicitly
# override the total budget with --deadline.
DEFAULT_FLEET_DRAIN_SLICE_SECONDS = 2.0
DEFAULT_FLEET_DRAIN_BUDGET_SECONDS = 30.0
DEFAULT_FLEET_DRAIN_PROBE_SECONDS = 0.25
PREWARM_MARKER = "SILICON_UPDATE_PREWARM="
SNAPSHOT_TRANSIENT_ATTEMPTS = 3
_ATOMIC_SNAPSHOT_DISAPPEAR_RE = re.compile(
    r"Protected source disappeared: [^\n]*"
    r"(?:^|/)\..+\.(?:[0-9]+\.[0-9]+|[A-Za-z0-9_-]{8})\.tmp(?:\s|$)"
)


def _create_checkpoint_with_transient_retry(snapshot_command, release_id: str):
    """Repeat the whole protected-path walk after an atomic temp rename."""

    payload = {"release_id": release_id}
    for attempt in range(SNAPSHOT_TRANSIENT_ATTEMPTS):
        try:
            return snapshot_command("create", payload)
        except UpdateError as exc:
            if (
                _ATOMIC_SNAPSHOT_DISAPPEAR_RE.search(str(exc)) is None
                or attempt + 1 >= SNAPSHOT_TRANSIENT_ATTEMPTS
            ):
                raise
            time.sleep(0.1 * (2**attempt))
    raise AssertionError("unreachable checkpoint retry state")


def _compensation_worker_count(concurrency: int, members: int) -> int:
    if members <= 0:
        return 0
    return min(
        max(1, concurrency),
        FLEET_COMPENSATION_CONCURRENCY,
        members,
    )


def _cache() -> ReleaseCache:
    return ReleaseCache(REGISTRY_DIR / "cache")


_SNAPSHOT_GC_SCRIPT = r"""
import json
import sys
from pathlib import Path

from core.backup import garbage_collect_referenced_snapshots

root = Path(sys.argv[1])
plan = garbage_collect_referenced_snapshots(root)
print(json.dumps({
    "manifests": [path.stem for path in plan.delete_manifests],
    "objects": [path.parent.name + path.name for path in plan.delete_objects],
}, sort_keys=True))
"""


def _parse_snapshot_gc_result(
    result: subprocess.CompletedProcess,
) -> dict[str, list[str]]:
    lines = str(getattr(result, "stdout", "") or "").strip().splitlines()
    if int(getattr(result, "returncode", 1)) or not lines:
        raise UpdateError(
            "canonical snapshot retention failed: "
            + (
                str(getattr(result, "stderr", "") or "").strip()
                or str(getattr(result, "stdout", "") or "").strip()
                or "no response"
            )
        )
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise UpdateError(
            "canonical snapshot retention returned invalid JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"manifests", "objects"}
        or not all(
            isinstance(value[key], list)
            and all(
                isinstance(item, str)
                and len(item) == 64
                and all(char in "0123456789abcdef" for char in item)
                for item in value[key]
            )
            for key in ("manifests", "objects")
        )
    ):
        raise UpdateError(
            "canonical snapshot retention returned an invalid result"
        )
    return {
        "manifests": list(value["manifests"]),
        "objects": list(value["objects"]),
    }


def _canonical_snapshot_gc(
    inst: registry.Install,
):
    """Return a post-commit callback that runs active Stemcell snapshot GC."""

    def collect() -> dict[str, list[str]]:
        if inst.is_docker:
            result = docker_runtime.run_active_python(
                inst,
                [
                    "-c",
                    _SNAPSHOT_GC_SCRIPT,
                    docker_runtime.CONTAINER_PATH,
                ],
            )
        else:
            release_root = active_release_root(inst.path)
            result = subprocess.run(
                [
                    python_run_cmd(inst.path),
                    "-c",
                    _SNAPSHOT_GC_SCRIPT,
                    inst.path,
                ],
                cwd=release_root,
                env=runtime_environment(inst.path),
                capture_output=True,
                text=True,
                check=False,
            )
        return _parse_snapshot_gc_result(result)

    return collect


def _fetch_latest(cache: ReleaseCache) -> FetchedRelease:
    return fetch_latest_release(
        cache,
        info=ui.info,
    )


def _local_hooks(inst: registry.Install) -> EngineHooks:
    def state() -> dict[str, bool]:
        return {
            "main": process.is_running(inst.pid_file),
            "glass_agent": glassagent.status(inst.path),
            "interface": interface_cli.daemon_running(inst.path),
        }

    maintenance = MaintenanceProtocol(
        Path(inst.path),
        legacy_offline_safe=lambda: not any(state().values()),
    )

    def quiesce_delivery() -> None:
        if interface_cli.daemon_running(inst.path):
            interface_cli.stop_daemon(inst.path, required=True)

    def stop_services() -> None:
        if maintenance.legacy_offline and any(state().values()):
            raise UpdateError(
                "legacy Silicon became active after its offline update fence; "
                "refusing to stop a possibly active task"
            )
        process.stop_one(inst.name, full=True)

    def start_services(previous: dict[str, bool]) -> None:
        interface = bool(previous.get("interface")) or bool(
            previous.get("main")
            and interface_cli.daemon_required(inst.path)
        )
        if previous.get("main"):
            process.start_one(
                inst.name,
                start_agent=bool(previous.get("glass_agent")),
                reconcile_updates=False,
            )
        else:
            if interface:
                interface_cli.start_daemon(inst.path, required=True)
            if previous.get("glass_agent"):
                glassagent.start(inst.path)
        if interface and not interface_cli.daemon_running(inst.path):
            interface_cli.start_daemon(inst.path, required=True)

    def healthy(previous: dict[str, bool]) -> bool:
        # Require three consecutive healthy observations. A process that merely
        # exists for one instant is not a successful restart.
        consecutive = 0
        deadline = time.monotonic() + HEALTH_BUDGET_SECONDS
        while time.monotonic() < deadline:
            main_ok = (
                process.runtime_ready(
                    inst.path,
                    inst.pid_file,
                    min_uptime=5.0,
                    max_heartbeat_age=HEARTBEAT_MAX_AGE_SECONDS,
                )
                if previous.get("main")
                else not process.is_running(inst.pid_file)
            )
            agent_ok = glassagent.status(inst.path) == bool(
                previous.get("glass_agent")
            )
            interface_ok = (
                not (
                    previous.get("interface")
                    or (
                        previous.get("main")
                        and interface_cli.daemon_required(inst.path)
                    )
                )
                or interface_cli.daemon_running(inst.path)
            )
            if main_ok and agent_ok and interface_ok:
                consecutive += 1
                if consecutive >= 3:
                    return True
            else:
                consecutive = 0
            time.sleep(0.5)
        return False

    def snapshot_command(mode: str, payload: dict[str, str]) -> dict[str, str]:
        release_root = active_release_root(inst.path)
        if mode == "create":
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from core.backup import create_local_snapshot;"
                "root=Path(sys.argv[2]);"
                "store=root/'.silicon'/'snapshots';"
                "result=create_local_snapshot(root,release_id=sys.argv[3],store=store);"
                "print(json.dumps({'root_hash':result.root_hash,"
                "'manifest_path':str(result.manifest_path),'store':str(store),"
                "'provider':'stemcell-canonical'}))"
            )
            arguments = [
                str(release_root),
                inst.path,
                payload["release_id"],
            ]
        elif mode == "verify":
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "sys.path.insert(0,sys.argv[1]);"
                "from core.backup import verify_local_snapshot;"
                "manifest=verify_local_snapshot(Path(sys.argv[2]),store=Path(sys.argv[3]));"
                "print(json.dumps({'root_hash':manifest['root_hash']}))"
            )
            arguments = [
                str(release_root),
                payload["manifest_path"],
                payload["store"],
            ]
        else:
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "from core.backup import restore_local_snapshot_in_place;"
                "result=restore_local_snapshot_in_place("
                "Path(sys.argv[2]),Path(sys.argv[3]),store=Path(sys.argv[4]));"
                "print(json.dumps({'root_hash':result.root_hash}))"
            )
            arguments = [
                str(release_root),
                inst.path,
                payload["manifest_path"],
                payload["store"],
            ]
        result = subprocess.run(
            [python_run_cmd(inst.path), "-c", script, *arguments],
            cwd=release_root,
            env=runtime_environment(inst.path),
            capture_output=True,
            text=True,
            check=False,
        )
        lines = result.stdout.strip().splitlines()
        if result.returncode or not lines:
            raise UpdateError(
                "canonical local recovery snapshot failed: "
                + (result.stderr.strip() or result.stdout.strip() or "no response")
            )
        try:
            value = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise UpdateError(
                "canonical snapshot provider returned invalid output"
            ) from exc
        if not isinstance(value, dict) or not value.get("root_hash"):
            raise UpdateError("canonical snapshot verification returned no root hash")
        return {str(key): str(item) for key, item in value.items()}

    def create_checkpoint(transaction_id: str, release_id: str) -> dict[str, str]:
        snapshot_release = f"{release_id}:pre-update:{transaction_id}"
        try:
            return _create_checkpoint_with_transient_retry(
                snapshot_command,
                snapshot_release,
            )
        except UpdateError as exc:
            message = str(exc)
            if not any(
                marker in message
                for marker in (
                    "cannot import name 'create_local_snapshot'",
                    'cannot import name "create_local_snapshot"',
                    "No module named 'core.backup'",
                    "Snapshot release sequence floor is invalid.",
                )
            ):
                raise
            from .updater.snapshot_adapter import create_local_snapshot

            return create_local_snapshot(
                Path(inst.path), release_id=snapshot_release
            )

    def verify_checkpoint(checkpoint: dict[str, str]) -> None:
        if checkpoint.get("provider") == "silicon-cli-bootstrap":
            from .updater.snapshot_adapter import verify_local_snapshot

            manifest = verify_local_snapshot(
                Path(checkpoint["manifest_path"]),
                store=Path(checkpoint["store"]),
            )
            verified = {"root_hash": str(manifest["root_hash"])}
        else:
            verified = snapshot_command("verify", checkpoint)
        if verified.get("root_hash") != checkpoint.get("root_hash"):
            raise UpdateError("canonical snapshot root hash changed during verification")

    def restore_checkpoint(checkpoint: dict[str, str]) -> None:
        def bootstrap_restore() -> str:
            from .updater.snapshot_adapter import (
                restore_local_snapshot_in_place,
            )

            restored = restore_local_snapshot_in_place(
                Path(inst.path),
                Path(checkpoint["manifest_path"]),
                store=Path(checkpoint["store"]),
            )
            return str(restored["root_hash"])

        if checkpoint.get("provider") == "silicon-cli-bootstrap":
            restored_hash = bootstrap_restore()
        else:
            try:
                restored_hash = snapshot_command("restore", checkpoint).get(
                    "root_hash", ""
                )
            except UpdateError as exc:
                if "restore_local_snapshot_in_place" not in str(exc):
                    raise
                restored_hash = bootstrap_restore()
        if restored_hash != checkpoint.get("root_hash"):
            raise UpdateError(
                "restored canonical snapshot has an unexpected root hash"
            )

    return EngineHooks(
        service_state=state,
        begin_maintenance=maintenance.begin,
        reattach_maintenance=maintenance.reattach,
        request_drain=maintenance.request_drain,
        await_quiescent=lambda txid, deadline, cancelled, running: (
            maintenance.await_quiescent(
                txid,
                deadline,
                cancelled,
                services_running=running,
            )
        ),
        cancel_drain=maintenance.cancel,
        quiesce_delivery=quiesce_delivery,
        stop_services=stop_services,
        start_services=start_services,
        health_check=healthy,
        set_phase=maintenance.set_phase,
        finish=maintenance.finish,
        create_checkpoint=create_checkpoint,
        verify_checkpoint=verify_checkpoint,
        restore_checkpoint=restore_checkpoint,
    )


def _docker_hooks(inst: registry.Install) -> EngineHooks:
    """Host-owned update hooks for a Docker-backed Silicon instance."""

    root = Path(inst.path).expanduser().resolve()

    def parse_result(result, context: str) -> dict:
        lines = str(getattr(result, "stdout", "") or "").strip().splitlines()
        if int(getattr(result, "returncode", 1)) or not lines:
            raise UpdateError(
                f"{context} failed: "
                + (
                    str(getattr(result, "stderr", "") or "").strip()
                    or str(getattr(result, "stdout", "") or "").strip()
                    or "no response"
                )
            )
        try:
            value = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise UpdateError(f"{context} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise UpdateError(f"{context} returned an invalid response")
        return value

    def coordinator_command(arguments: list[str]) -> dict:
        result = docker_runtime.run_active_python(
            inst,
            [
                "-m",
                "core.maintenance",
                "--root",
                docker_runtime.CONTAINER_PATH,
                *arguments,
            ],
        )
        # The coordinator intentionally exits nonzero with a structured JSON
        # error for invalid transitions. Preserve its MaintenanceError type so
        # idempotent recovery logic can inspect the durable status instead of
        # losing it in the generic Docker command wrapper.
        lines = str(getattr(result, "stdout", "") or "").strip().splitlines()
        if lines:
            try:
                failure = json.loads(lines[-1])
            except json.JSONDecodeError:
                failure = None
            if isinstance(failure, dict) and failure.get("error"):
                raise MaintenanceError(str(failure["error"]))
        value = parse_result(result, "Docker task-safe maintenance coordinator")
        if value.get("error"):
            raise MaintenanceError(str(value["error"]))
        return value

    def state() -> dict[str, bool]:
        container = docker_runtime.container_running(inst)
        return {
            "container": container,
            "main": docker_runtime.silicon_running(inst) if container else False,
            "glass_agent": (
                docker_runtime.glass_agent_running(inst) if container else False
            ),
            "interface": (
                docker_runtime.interface_daemon_running(inst)
                if container
                else False
            ),
        }

    coordinator_available = docker_runtime.maintenance_coordinator_available(inst)
    maintenance = (
        MaintenanceProtocol(
            root,
            command=coordinator_command,
            legacy_offline_safe=lambda: not any(state().values()),
            # A Docker status probe starts an exec process and Python runtime.
            # Polling four fleet workers at 250 ms creates an avoidable exec
            # storm and filesystem-journal pressure on the host.
            poll_interval_seconds=2.0,
        )
        if coordinator_available
        else MaintenanceProtocol(
            root,
            legacy_offline_safe=lambda: not any(state().values()),
        )
    )

    def begin_maintenance(transaction_id: str, target_version: str) -> None:
        services_active = any(state().values())
        # A fully stopped Docker instance must use the durable offline fence
        # even when its installed release contains a maintenance coordinator.
        # There is no running runtime to acknowledge or preserve a drain. Latch
        # this only for a newly beginning transaction: reattachment instead
        # recovers the persisted offline fence or the coordinator state.
        if not services_active:
            maintenance.select_offline()
        # A running legacy container cannot truthfully enter a task-safe drain.
        # Reject before publishing a Carbon-visible Glass lease.
        if not coordinator_available and services_active:
            raise MaintenanceError(
                "this legacy Docker Stemcell has no task-safe coordinator; "
                f"run `silicon stop --full {inst.name}` from the host, then "
                "retry the update"
            )
        maintenance.begin(transaction_id, target_version)

    def quiesce_delivery() -> None:
        if docker_runtime.container_running(inst):
            if docker_runtime.interface_daemon_running(inst):
                docker_runtime.stop_interface_daemon(inst, required=True)
            socket_path = root / ".silicon-interface" / "daemon.sock"
            try:
                metadata = socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                # A stopped, crashed, or not-yet-observable daemon can leave
                # its bound inode behind. Remove only the exact verified Unix
                # socket; regular files, links, and other special files remain
                # for policy validation.
                if stat.S_ISSOCK(metadata.st_mode):
                    socket_path.unlink()

    def stop_services() -> None:
        with _DOCKER_CONTROL_GATE:
            if docker_runtime.container_running(inst):
                docker_runtime.stop_one(inst, full=True)
            if docker_runtime.container_running(inst):
                raise UpdateError(
                    f"Docker container for '{inst.name}' did not stop cleanly"
                )

    def start_services(previous: dict[str, bool]) -> None:
        interface = bool(previous.get("interface")) or bool(
            previous.get("main")
            and interface_cli.daemon_required(inst.path)
        )
        container = bool(
            previous.get(
                "container",
                previous.get("main")
                or previous.get("glass_agent")
                or interface,
            )
        )
        with _DOCKER_CONTROL_GATE:
            docker_runtime.restore_one(
                inst,
                container=container,
                main=bool(previous.get("main")),
                glass_agent=bool(previous.get("glass_agent")),
                interface=interface,
                reconcile=False,
                allow_legacy_fence=maintenance.legacy_offline,
            )

    def healthy(previous: dict[str, bool]) -> bool:
        expected_interface = bool(previous.get("interface")) or bool(
            previous.get("main")
            and interface_cli.daemon_required(inst.path)
        )
        expected_container = bool(
            previous.get(
                "container",
                previous.get("main")
                or previous.get("glass_agent")
                or expected_interface,
            )
        )
        consecutive = 0
        deadline = time.monotonic() + HEALTH_BUDGET_DOCKER_SECONDS
        while time.monotonic() < deadline:
            container = docker_runtime.container_running(inst)
            main = (
                docker_runtime.silicon_ready(
                    inst,
                    min_uptime=5.0,
                    max_heartbeat_age=HEARTBEAT_MAX_AGE_SECONDS,
                )
                if container and previous.get("main")
                else (
                    docker_runtime.silicon_running(inst)
                    if container
                    else False
                )
            )
            agent = (
                docker_runtime.glass_agent_running(inst) if container else False
            )
            interface = (
                docker_runtime.interface_daemon_running(inst)
                if container and expected_interface
                else False
            )
            if (
                container == expected_container
                and main == bool(previous.get("main"))
                and agent == bool(previous.get("glass_agent"))
                and (not expected_interface or interface)
            ):
                consecutive += 1
                if consecutive >= 3:
                    return True
            else:
                consecutive = 0
            time.sleep(DOCKER_HEALTH_POLL_SECONDS)
        return False

    def checkpoint_host_path(value: str) -> Path:
        raw = Path(str(value))
        if raw.is_absolute() and (
            raw == Path(docker_runtime.CONTAINER_PATH)
            or Path(docker_runtime.CONTAINER_PATH) in raw.parents
        ):
            resolved = docker_runtime._host_path(inst, raw)
        elif raw.is_absolute():
            resolved = raw.resolve(strict=False)
        else:
            resolved = (root / raw).resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise UpdateError("Docker checkpoint path escaped the instance root")
        return resolved

    def snapshot_command(mode: str, payload: dict[str, str]) -> dict[str, str]:
        if mode == "create":
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "from core.backup import create_local_snapshot;"
                "root=Path(sys.argv[1]);"
                "store=root/'.silicon'/'snapshots';"
                "result=create_local_snapshot(root,release_id=sys.argv[2],store=store);"
                "print(json.dumps({'root_hash':result.root_hash,"
                "'manifest_path':str(result.manifest_path),'store':str(store),"
                "'provider':'stemcell-canonical'}))"
            )
            arguments = [
                docker_runtime.CONTAINER_PATH,
                payload["release_id"],
            ]
        elif mode == "verify":
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "from core.backup import verify_local_snapshot;"
                "manifest=verify_local_snapshot(Path(sys.argv[1]),store=Path(sys.argv[2]));"
                "print(json.dumps({'root_hash':manifest['root_hash']}))"
            )
            arguments = [
                docker_runtime._container_path(
                    inst, checkpoint_host_path(payload["manifest_path"])
                ),
                docker_runtime._container_path(
                    inst, checkpoint_host_path(payload["store"])
                ),
            ]
        else:
            script = (
                "import json,sys;"
                "from pathlib import Path;"
                "from core.backup import restore_local_snapshot_in_place;"
                "result=restore_local_snapshot_in_place("
                "Path(sys.argv[1]),Path(sys.argv[2]),store=Path(sys.argv[3]));"
                "print(json.dumps({'root_hash':result.root_hash}))"
            )
            arguments = [
                docker_runtime.CONTAINER_PATH,
                docker_runtime._container_path(
                    inst, checkpoint_host_path(payload["manifest_path"])
                ),
                docker_runtime._container_path(
                    inst, checkpoint_host_path(payload["store"])
                ),
            ]
        value = parse_result(
            docker_runtime.run_active_python(
                inst, ["-c", script, *arguments]
            ),
            "canonical Docker recovery snapshot",
        )
        if not value.get("root_hash"):
            raise UpdateError(
                "canonical snapshot verification returned no root hash"
            )
        result = {str(key): str(item) for key, item in value.items()}
        if mode == "create":
            result["manifest_path"] = str(
                checkpoint_host_path(result["manifest_path"])
            )
            result["store"] = str(checkpoint_host_path(result["store"]))
        return result

    def create_checkpoint(transaction_id: str, release_id: str) -> dict[str, str]:
        snapshot_release = f"{release_id}:pre-update:{transaction_id}"
        try:
            return _create_checkpoint_with_transient_retry(
                snapshot_command,
                snapshot_release,
            )
        except UpdateError as exc:
            message = str(exc)
            if not any(
                marker in message
                for marker in (
                    "cannot import name 'create_local_snapshot'",
                    'cannot import name "create_local_snapshot"',
                    "No module named 'core.backup'",
                    "Snapshot release sequence floor is invalid.",
                )
            ):
                raise
            from .updater.snapshot_adapter import create_local_snapshot

            checkpoint = create_local_snapshot(
                root, release_id=snapshot_release
            )
            return checkpoint

    def verify_checkpoint(checkpoint: dict[str, str]) -> None:
        if checkpoint.get("provider") == "silicon-cli-bootstrap":
            from .updater.snapshot_adapter import verify_local_snapshot

            manifest = verify_local_snapshot(
                checkpoint_host_path(checkpoint["manifest_path"]),
                store=checkpoint_host_path(checkpoint["store"]),
            )
            verified = {"root_hash": str(manifest["root_hash"])}
        else:
            verified = snapshot_command("verify", checkpoint)
        if verified.get("root_hash") != checkpoint.get("root_hash"):
            raise UpdateError(
                "canonical snapshot root hash changed during verification"
            )

    def restore_checkpoint(checkpoint: dict[str, str]) -> None:
        def bootstrap_restore() -> str:
            from .updater.snapshot_adapter import (
                restore_local_snapshot_in_place,
            )

            restored = restore_local_snapshot_in_place(
                root,
                checkpoint_host_path(checkpoint["manifest_path"]),
                store=checkpoint_host_path(checkpoint["store"]),
            )
            return str(restored["root_hash"])

        if checkpoint.get("provider") == "silicon-cli-bootstrap":
            restored_hash = bootstrap_restore()
        else:
            try:
                restored_hash = snapshot_command("restore", checkpoint).get(
                    "root_hash", ""
                )
            except UpdateError as exc:
                if "restore_local_snapshot_in_place" not in str(exc):
                    raise
                restored_hash = bootstrap_restore()
        if restored_hash != checkpoint.get("root_hash"):
            raise UpdateError(
                "restored canonical snapshot has an unexpected root hash"
            )

    def prepare_environment(release: Path, runtime_image: str) -> Path | None:
        if runtime_image:
            docker_runtime.prepare_release_image(runtime_image)
        return docker_runtime.prepare_environment(
            inst,
            release,
            image=runtime_image or None,
        )

    return EngineHooks(
        service_state=state,
        begin_maintenance=begin_maintenance,
        reattach_maintenance=maintenance.reattach,
        request_drain=maintenance.request_drain,
        await_quiescent=lambda txid, deadline, cancelled, running: (
            maintenance.await_quiescent(
                txid,
                deadline,
                cancelled,
                services_running=running,
            )
        ),
        cancel_drain=maintenance.cancel,
        quiesce_delivery=quiesce_delivery,
        stop_services=stop_services,
        start_services=start_services,
        health_check=healthy,
        set_phase=maintenance.set_phase,
        finish=maintenance.finish,
        create_checkpoint=create_checkpoint,
        verify_checkpoint=verify_checkpoint,
        restore_checkpoint=restore_checkpoint,
        prepare_environment=prepare_environment,
    )


def _hooks(inst: registry.Install) -> EngineHooks:
    return _docker_hooks(inst) if inst.is_docker else _local_hooks(inst)


def _engine(inst: registry.Install) -> TransactionalUpdater:
    updater = TransactionalUpdater(
        Path(inst.path),
        _cache(),
        hooks=_hooks(inst),
        all_instances=[
            Path(item.path)
            for item in registry.installs()
        ],
    )
    updater.retention.configure_snapshot_gc(_canonical_snapshot_gc(inst))
    return updater


def _matching_fleet_transaction(
    updater: TransactionalUpdater,
    fleet: FleetJournal,
    *,
    operation: str,
    source_transaction_id: str = "",
) -> dict | None:
    target_tree = str(fleet.value["release"]["tree_sha256"])
    created_at = float(fleet.value["created_at"])
    for transaction in updater.history():
        if float(transaction.get("created_at", 0)) < created_at:
            continue
        metadata = transaction.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if metadata.get("operation") != operation:
            continue
        if operation == "update":
            release = metadata.get("release")
            if (
                not isinstance(release, dict)
                or release.get("tree_sha256") != target_tree
            ):
                continue
        elif metadata.get("source_transaction_id") != source_transaction_id:
            continue
        return transaction
    return None


def _fleet_installs(fleet: FleetJournal) -> list[registry.Install]:
    available = {
        (
            item.name,
            str(Path(item.path).expanduser().resolve(strict=False)),
        ): item
        for item in registry.installs()
    }
    result = []
    for member in fleet.value["members"]:
        identity = (
            str(member["name"]),
            str(Path(member["path"]).resolve(strict=False)),
        )
        install = available.get(identity)
        if install is None:
            raise UpdateError(
                "fleet recovery cannot resolve its journaled member "
                f"{member['name']!r} at {member['path']}; registry state must "
                "be repaired before this rollout can be reconciled"
            )
        result.append(install)
    return result


def _start_fleet_compensation(
    updater: TransactionalUpdater,
    source_transaction: str,
    *,
    deadline_seconds: float | None,
) -> dict:
    """Start one rollback with a budget measured from actual worker start."""

    return updater.rollback(
        deadline=(
            time.time() + deadline_seconds
            if deadline_seconds is not None
            else None
        ),
        transaction_id=source_transaction,
        lock_held=True,
    )


def _resume_or_start_fleet_compensation(
    updater: TransactionalUpdater,
    fleet: FleetJournal,
    source_transaction: str,
    *,
    deadline_seconds: float | None,
) -> dict:
    """Finish one fleet rollback without mutating the shared fleet journal."""

    status = updater.status()
    active = status.get("active_transaction")
    if isinstance(active, dict):
        metadata = active.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("operation") != "rollback"
            or metadata.get("source_transaction_id") != source_transaction
        ):
            raise UpdateError(
                "instance has an unrelated active transaction during fleet "
                "compensation"
            )
        result = updater.resume(
            str(active["transaction_id"]), lock_held=True
        )
    else:
        prior_rollback = _matching_fleet_transaction(
            updater,
            fleet,
            operation="rollback",
            source_transaction_id=source_transaction,
        )
        if (
            isinstance(prior_rollback, dict)
            and prior_rollback.get("state") == "COMMITTED"
        ):
            result = prior_rollback
        else:
            result = _start_fleet_compensation(
                updater,
                source_transaction,
                deadline_seconds=deadline_seconds,
            )
    if result.get("state") != "COMMITTED":
        raise UpdateError("fleet compensation did not commit")
    return result


def _reconcile_incomplete_fleet(
    fleet: FleetJournal | None = None,
    *,
    deadline_seconds: float | None = None,
    concurrency: int = DEFAULT_FLEET_CONCURRENCY,
) -> dict | None:
    """Conservatively compensate a crash-interrupted partial fleet rollout."""

    fleet = fleet or FleetJournal.active(REGISTRY_DIR)
    if fleet is None:
        return None
    installs = _fleet_installs(fleet)
    cache = _cache()
    instance_roots = [Path(item.path) for item in registry.installs()]
    updaters = [
        TransactionalUpdater(
            Path(install.path),
            cache,
            hooks=_hooks(install),
            all_instances=instance_roots,
        )
        for install in installs
    ]
    reservations = [
        InstanceLock(Path(install.path), fleet.value["fleet_id"] + "-reconcile")
        for install in sorted(
            installs, key=lambda item: str(Path(item.path).resolve())
        )
    ]
    acquired: list[InstanceLock] = []
    try:
        for reservation in reservations:
            reservation.acquire()
            acquired.append(reservation)

        # First settle any activation whose process died between the
        # per-instance commit and the host journal update.
        for index, updater in enumerate(updaters):
            member = fleet.value["members"][index]
            interrupted_activation = member["state"] in {
                "activating",
                "draining",
            } or (
                member["state"] == "failed"
                and not member["update_transaction_id"]
            )
            if not interrupted_activation:
                continue
            status = updater.status()
            active = status.get("active_transaction")
            result = None
            if isinstance(active, dict):
                metadata = active.get("metadata")
                release = (
                    metadata.get("release")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    not isinstance(release, dict)
                    or release.get("tree_sha256")
                    != fleet.value["release"]["tree_sha256"]
                ):
                    raise UpdateError(
                        f"'{member['name']}' has an unrelated active "
                        "transaction during fleet recovery"
                    )
                result = updater.resume(
                    str(active["transaction_id"]), lock_held=True
                )
            else:
                result = _matching_fleet_transaction(
                    updater, fleet, operation="update"
                )
            if isinstance(result, dict) and result.get("state") == "COMMITTED":
                fleet.member(
                    index,
                    state="committed",
                    update_transaction_id=str(result["transaction_id"]),
                    error="",
                )
            else:
                fleet.member(index, state="compensated", error="")

        if all(
            member["state"] == "committed"
            for member in fleet.value["members"]
        ):
            fleet.set_state("COMMITTED")
            return fleet.value

        fleet.set_state("COMPENSATING")
        compensation: list[tuple[int, TransactionalUpdater, str]] = []
        for index in reversed(range(len(updaters))):
            updater = updaters[index]
            member = fleet.value["members"][index]
            uncommitted = member["state"] in {
                "pending",
                "activating",
                "draining",
            } or (
                member["state"] == "failed"
                and not member["update_transaction_id"]
            )
            if uncommitted:
                fleet.member(index, state="compensated", error="")
                continue
            if member["state"] == "compensated":
                continue
            source_transaction = str(member["update_transaction_id"])
            if not source_transaction:
                raise UpdateError(
                    f"fleet member '{member['name']}' has no committed "
                    "update transaction to compensate"
                )
            fleet.member(index, state="compensating", error="")
            compensation.append((index, updater, source_transaction))
        compensation_failures: list[str] = []
        if compensation:
            workers = _compensation_worker_count(
                concurrency, len(compensation)
            )
            ui.info(
                f"Restoring {len(compensation)} fleet member(s) with up to "
                f"{workers} parallel workers."
            )
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="silicon-compensate",
            ) as executor:
                futures = {
                    executor.submit(
                        _resume_or_start_fleet_compensation,
                        updater,
                        fleet,
                        source_transaction,
                        deadline_seconds=deadline_seconds,
                    ): index
                    for index, updater, source_transaction in compensation
                }
                for future in as_completed(futures):
                    index = futures[future]
                    member = fleet.value["members"][index]
                    try:
                        rollback_result = future.result()
                    except Exception as exc:
                        detail = str(exc)
                        compensation_failures.append(
                            f"{member['name']}: {detail}"
                        )
                        fleet.member(index, state="failed", error=detail)
                    else:
                        fleet.member(
                            index,
                            state="compensated",
                            rollback_transaction_id=str(
                                rollback_result["transaction_id"]
                            ),
                            error="",
                        )
        if compensation_failures:
            fleet.set_state("NEEDS_ATTENTION")
            raise UpdateError(
                "fleet compensation needs operator resume for "
                + "; ".join(compensation_failures)
            )
        fleet.set_state("COMPENSATED")
        return fleet.value
    except BaseException:
        # BaseException includes injected crash/power-loss tests. Persist what
        # is knowable, then let the next invocation deterministically retry.
        try:
            fleet.set_state("NEEDS_ATTENTION")
        except Exception:
            pass
        raise
    finally:
        for reservation in reversed(acquired):
            reservation.release()


def reconcile_before_start(inst: registry.Install) -> None:
    """Finish an interrupted local update before accepting any new work."""

    fleet = FleetJournal.active(REGISTRY_DIR)
    if fleet is not None and any(
        Path(member["path"]).resolve(strict=False)
        == Path(inst.path).resolve(strict=False)
        for member in fleet.value["members"]
    ):
        ui.warn(
            f"'{inst.name}' belongs to interrupted fleet rollout "
            f"{fleet.value['fleet_id']}; compensating it before accepting work."
        )
        _reconcile_incomplete_fleet(fleet)
    updater = _engine(inst)
    active = updater.status().get("active_transaction")
    if not isinstance(active, dict):
        return
    transaction_id = active.get("transaction_id")
    state = active.get("state")
    if not isinstance(transaction_id, str) or not isinstance(state, str):
        raise UpdateError(
            "interrupted update metadata is invalid; refusing to start Silicon"
        )
    ui.warn(
        f"'{inst.name}' has interrupted update {transaction_id} ({state}); "
        "reconciling it before Silicon can accept work."
    )
    with _transaction_signal_guard():
        result = updater.resume(transaction_id)
    outcome = result.get("state") if isinstance(result, dict) else None
    if outcome not in {"COMMITTED", "ROLLED_BACK"}:
        raise UpdateError(
            f"update {transaction_id} did not reach a safe terminal state; "
            "refusing to start Silicon"
        )
    ui.success(
        f"Interrupted update {transaction_id} reconciled as {outcome.lower()}."
    )


def _targets(target: str | None) -> list[registry.Install]:
    if target and registry.is_multi_target(target):
        names = registry.resolve_targets(target)
        if not names:
            raise UpdateError("no matching installations")
        return [registry.resolve_one(name) for name in names]
    return [registry.resolve_one(target)]


def _refuse_self_update(installs: list[registry.Install]) -> None:
    """Never let a Silicon task wait for the updater that is waiting on it."""

    roots = {
        value
        for value in (
            os.environ.get("SILICON_DATA_ROOT", ""),
            os.environ.get("SILICON_CODE_ROOT", ""),
        )
        if value.strip()
    }
    if not roots:
        return
    caller_roots = {
        Path(value).expanduser().resolve(strict=False)
        for value in roots
    }
    for install in installs:
        target = Path(install.path).expanduser().resolve(strict=False)
        if target not in caller_roots:
            continue
        raise UpdateError(
            f"refusing to update '{install.name}' from inside that Silicon's "
            "own task process: the updater must wait for the task to finish, "
            "so this process would deadlock it. Let the task return, then run "
            f"`silicon update {install.name}` from a host shell."
        )


@contextmanager
def _transaction_signal_guard():
    """Turn a normal termination request into the engine's safe cancel/recover path."""

    installed = {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        try:
            installed[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
        except (OSError, ValueError):
            pass
    try:
        yield
    finally:
        for signum, handler in installed.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


def _activate_local_fleet_work_conserving(
    prepared: list[tuple[registry.Install, TransactionalUpdater, dict]],
    member_indexes: list[int],
    release: FetchedRelease,
    fleet_journal: FleetJournal,
    committed: list[
        tuple[int, registry.Install, TransactionalUpdater, str]
    ],
    *,
    concurrency: int,
    deadline_seconds: float | None,
    success_limit: int | None = None,
    drain_slice_seconds: float = DEFAULT_FLEET_DRAIN_SLICE_SECONDS,
    drain_budget_seconds: float = DEFAULT_FLEET_DRAIN_BUDGET_SECONDS,
    drain_probe_seconds: float = DEFAULT_FLEET_DRAIN_PROBE_SECONDS,
    parked_limit: int | None = None,
) -> tuple[int, list[int], dict[str, int]]:
    """Activate ready local Silicons without letting busy drains own slots.

    A short probe starts with a productive-slot reservation. If it is still
    draining after that probe, it moves to the separate parked pool and a new
    candidate immediately receives the productive reservation. Parked workers
    continue watching their owned maintenance fence; when one becomes safe it
    must acquire the shared activation gate before checkpoint/stop/activation.

    The real maintenance deadline remains active for the entire parked drain.
    If that bounded deadline expires, the engine cancels only its owned fence,
    leaves the old generation serving, and reports a fleet failure so existing
    transactional compensation can restore already-committed members.

    This scheduler is intentionally local-runtime only.  Docker control-plane
    serialization and container recovery retain their existing wave behavior.
    """

    if not member_indexes:
        return 0, [], {"attempts": 0, "deferred_attempts": 0}
    if any(prepared[index][0].is_docker for index in member_indexes):
        raise UpdateError(
            "work-conserving activation only supports local-Python Silicons"
        )
    productive_limit = min(max(1, concurrency), len(member_indexes))
    resolved_parked_limit = min(
        productive_limit,
        max(1, productive_limit if parked_limit is None else parked_limit),
    )
    workers = min(
        productive_limit + resolved_parked_limit,
        len(member_indexes),
    )
    target_successes = min(
        len(member_indexes),
        success_limit if success_limit is not None else len(member_indexes),
    )
    pending = deque(member_indexes)
    completed_indexes: set[int] = set()
    failures = 0
    attempts = 0
    deferred_attempts = 0
    stop_scheduling = False
    member_states: dict[int, str] = {}
    probe_started_at: dict[int, float] = {}
    states_lock = threading.Lock()
    activation_gate = threading.BoundedSemaphore(productive_limit)
    peak_productive = 0
    peak_parked = 0
    peak_in_flight = 0
    futures: dict[
        Future[dict], tuple[int, registry.Install, TransactionalUpdater]
    ] = {}

    explicit_budget = deadline_seconds
    member_budget = (
        float(explicit_budget)
        if explicit_budget is not None
        else float(drain_budget_seconds)
    )
    # ``drain_slice_seconds`` remains an internal compatibility knob for
    # focused callers, but it is deliberately not used as the maintenance
    # deadline. The short value is only a probe quantum; parked Silicons keep
    # their DRAIN_REQUESTED fence for the full bounded member budget.
    probe_seconds = max(
        0.01,
        min(float(drain_probe_seconds), float(drain_slice_seconds)),
    )

    def record_peaks() -> None:
        nonlocal peak_productive, peak_parked, peak_in_flight
        productive = sum(
            state == "productive" for state in member_states.values()
        )
        parked = sum(
            state == "parked" for state in member_states.values()
        )
        peak_productive = max(peak_productive, productive)
        peak_parked = max(peak_parked, parked)
        peak_in_flight = max(peak_in_flight, len(member_states))

    def set_state(member_index: int, state: str) -> None:
        with states_lock:
            if member_index not in member_states:
                return
            member_states[member_index] = state
            if state == "probing":
                probe_started_at[member_index] = time.monotonic()
            record_peaks()

    def run_attempt(
        member_index: int,
        updater: TransactionalUpdater,
        preflight: dict,
        attempt_deadline: float,
    ) -> dict:
        return updater.run(
            release,
            deadline=attempt_deadline,
            prepared=preflight,
            lock_held=True,
            defer_retention=True,
            activation_gate=activation_gate,
            on_drain_requested=lambda: set_state(member_index, "probing"),
            on_activation_slot=lambda: set_state(
                member_index, "productive"
            ),
            on_activation_slot_released=lambda: set_state(
                member_index, "finishing"
            ),
        )

    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="silicon-activate-local",
    )
    try:
        while futures or (
            not stop_scheduling
            and len(completed_indexes) < target_successes
            and pending
        ):
            now = time.monotonic()
            with states_lock:
                for member_index, state in list(member_states.items()):
                    if (
                        state == "probing"
                        and now - probe_started_at.get(member_index, now)
                        >= probe_seconds
                    ):
                        member_states[member_index] = "parked"
                        install = prepared[member_index][0]
                        fleet_journal.member(
                            member_index,
                            state="draining",
                            error="",
                        )
                        ui.info(
                            f"'{install.name}' is still busy; parked its "
                            "bounded drain and opened the productive slot."
                        )
                record_peaks()

            while (
                not stop_scheduling
                and len(futures) < workers
                and pending
            ):
                with states_lock:
                    productive_reservations = sum(
                        state in {"starting", "probing", "productive"}
                        for state in member_states.values()
                    )
                    parked = sum(
                        state == "parked"
                        for state in member_states.values()
                    )
                remaining_successes = target_successes - len(
                    completed_indexes
                )
                if (
                    productive_reservations
                    >= min(productive_limit, remaining_successes)
                    or parked >= resolved_parked_limit
                ):
                    break
                member_index = pending.popleft()
                install, updater, preflight = prepared[member_index]
                attempt_deadline = time.time() + member_budget
                attempts += 1
                with states_lock:
                    member_states[member_index] = "starting"
                    record_peaks()
                fleet_journal.member(
                    member_index, state="activating", error=""
                )
                ui.info(
                    f"Task-safely activating the pre-staged update for "
                    f"'{install.name}'..."
                )
                future = executor.submit(
                    run_attempt,
                    member_index,
                    updater,
                    preflight,
                    attempt_deadline,
                )
                futures[future] = (member_index, install, updater)

            if not futures:
                break
            done, _not_done = wait(
                set(futures),
                timeout=min(0.05, probe_seconds),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue
            for future in done:
                member_index, install, updater = futures.pop(future)
                with states_lock:
                    member_states.pop(member_index, None)
                    probe_started_at.pop(member_index, None)
                try:
                    result = future.result()
                except MaintenanceTimeout as exc:
                    deferred_attempts += 1
                    failures += 1
                    stop_scheduling = True
                    detail = (
                        "Silicon remained busy for the bounded fleet drain "
                        f"budget ({member_budget:g}s): {exc}"
                    )
                    fleet_journal.member(
                        member_index,
                        state="compensated",
                        error=detail,
                    )
                    ui.error(
                        f"Deferred update for '{install.name}' expired "
                        f"safely: {detail}"
                    )
                except Exception as exc:
                    failures += 1
                    stop_scheduling = True
                    member_state = "failed"
                    try:
                        if updater.status().get("active_transaction") is None:
                            member_state = "compensated"
                    except Exception:
                        pass
                    fleet_journal.member(
                        member_index,
                        state=member_state,
                        error=str(exc),
                    )
                    ui.error(
                        f"Update failed safely for '{install.name}': {exc}"
                    )
                else:
                    transaction_id = str(result["transaction_id"])
                    committed.append(
                        (member_index, install, updater, transaction_id)
                    )
                    completed_indexes.add(member_index)
                    fleet_journal.member(
                        member_index,
                        state="committed",
                        update_transaction_id=transaction_id,
                        error="",
                    )
                    ui.success(
                        f"'{install.name}' updated transactionally "
                        f"({transaction_id})"
                    )
    finally:
        # A signal stops new submissions. Already-running attempts have a short
        # deadline and execute the engine's owned-fence cleanup before this
        # returns, so cancellation never abandons a background drain thread.
        executor.shutdown(wait=True, cancel_futures=True)

    remaining_indexes = [
        index for index in member_indexes if index not in completed_indexes
    ]
    return failures, remaining_indexes, {
        "attempts": attempts,
        "deferred_attempts": deferred_attempts,
        "peak_productive": peak_productive,
        "peak_parked": peak_parked,
        "peak_in_flight": peak_in_flight,
    }


def update_instance(
    target: str | None,
    *,
    dry_run: bool = False,
    deadline_seconds: float | None = None,
    concurrency: int | None = None,
    canary_count: int | None = None,
    all_at_once: bool = False,
) -> dict[str, object]:
    rollout_started = time.monotonic()
    timings: dict[str, object] = {
        "resolve_release": 0.0,
        "metadata_contract": 0.0,
        "image_pull": 0.0,
        "runtime_probe": 0.0,
        "prestage": 0.0,
        "activation": 0.0,
        "retention": 0.0,
        "waves": [],
        "total": 0.0,
    }
    installs = _targets(target)
    resolved_concurrency = _fleet_option(
        concurrency,
        env_name="SILICON_UPDATE_CONCURRENCY",
        default=DEFAULT_FLEET_CONCURRENCY,
        label="update concurrency",
        minimum=1,
        maximum=MAX_FLEET_CONCURRENCY,
    )
    resolved_canary_count = _fleet_option(
        canary_count,
        env_name="SILICON_UPDATE_CANARY_COUNT",
        default=DEFAULT_FLEET_CANARY_COUNT,
        label="canary count",
        minimum=0,
        maximum=MAX_FLEET_CONCURRENCY,
    )
    if all_at_once:
        resolved_concurrency = min(
            max(1, len(installs)),
            MAX_FLEET_CONCURRENCY,
        )
        resolved_canary_count = 0
    elif len(installs) <= 1:
        resolved_concurrency = 1
        resolved_canary_count = 0
    _refuse_self_update(installs)
    incomplete_fleet = FleetJournal.active(REGISTRY_DIR)
    if incomplete_fleet is not None:
        if dry_run:
            raise UpdateError(
                "an interrupted fleet rollout must be reconciled before a "
                "new dry-run can be trusted; run `silicon update resume all`"
            )
        ui.warn(
            "Reconciling interrupted fleet rollout "
            f"{incomplete_fleet.value['fleet_id']} before starting a new update."
        )
        _reconcile_incomplete_fleet(
            incomplete_fleet,
            deadline_seconds=deadline_seconds,
            concurrency=resolved_concurrency,
        )
    if len(installs) > 1 and target in {"all", "*"}:
        if not ui.confirm(
            "Are you sure you want to update: "
            + ", ".join(install.name for install in installs)
            + "?"
        ):
            timings["total"] = round(time.monotonic() - rollout_started, 3)
            return {"schema": 1, "status": "cancelled", "timings_seconds": timings}
    cache = _cache()
    release_started = time.monotonic()
    release = _fetch_latest(cache)
    timings["resolve_release"] = round(
        time.monotonic() - release_started,
        3,
    )
    docker_installs = [install for install in installs if install.is_docker]
    if docker_installs and not dry_run:
        runtime_image = release.manifest.runtime_image
        if not runtime_image:
            raise UpdateError(
                "Docker updates require the published Stemcell Git tag to "
                "declare an immutable runtime_image digest"
            )
        metadata_started = time.monotonic()
        runtime_contract.verify_release_contract_metadata(
            release.manifest.runtime_contract
        )
        timings["metadata_contract"] = round(
            time.monotonic() - metadata_started,
            3,
        )
        image_started = time.monotonic()
        runtime_config = docker_runtime.prepare_release_image(runtime_image)
        timings["image_pull"] = round(time.monotonic() - image_started, 3)
        probe_started = time.monotonic()
        docker_runtime.verify_runtime_contract(
            runtime_config,
            runtime_image,
        )
        timings["runtime_probe"] = round(
            time.monotonic() - probe_started,
            3,
        )
    instance_roots = [Path(item.path) for item in registry.installs()]
    updaters = [
        (
            install,
            TransactionalUpdater(
                Path(install.path),
                cache,
                hooks=_hooks(install),
                all_instances=instance_roots,
            ),
        )
        for install in installs
    ]
    failures = 0
    if dry_run:
        for install, updater in updaters:
            try:
                result = updater.plan(release)
                print(json.dumps(result, indent=2, sort_keys=True))
                if not result["safe_to_apply"]:
                    failures += 1
            except Exception as exc:
                failures += 1
                ui.error(f"Dry-run failed safely for '{install.name}': {exc}")
        if failures:
            raise SystemExit(2)
        timings["total"] = round(time.monotonic() - rollout_started, 3)
        return {"schema": 1, "status": "dry-run", "timings_seconds": timings}

    fleet_id = f"fleet-{int(time.time())}-{os.getpid()}"
    reservations = [
        InstanceLock(Path(install.path), fleet_id)
        for install, _updater in sorted(
            updaters, key=lambda row: str(Path(row[0].path).resolve())
        )
    ]
    acquired: list[InstanceLock] = []
    prepared: list[
        tuple[registry.Install, TransactionalUpdater, dict]
    ] = []
    committed: list[
        tuple[int, registry.Install, TransactionalUpdater, str]
    ] = []
    fleet_journal: FleetJournal | None = None
    try:
        for reservation in reservations:
            reservation.acquire()
            acquired.append(reservation)
        preflight_started = time.monotonic()
        preflight_results: dict[int, dict] = {}
        preflight_workers = min(resolved_concurrency, len(updaters))
        if len(updaters) > 1:
            ui.info(
                f"Pre-staging {len(updaters)} Silicons with up to "
                f"{preflight_workers} parallel workers while they stay online."
            )
        with ThreadPoolExecutor(
            max_workers=preflight_workers,
            thread_name_prefix="silicon-preflight",
        ) as executor:
            preflight_futures: dict[
                Future[dict], tuple[
                    int, registry.Install, TransactionalUpdater
                ]
            ] = {}
            for member_index, (install, updater) in enumerate(updaters):
                persisted = updater.load_preflight(release)
                if isinstance(persisted, dict):
                    preflight_results[member_index] = persisted
                    ui.info(
                        f"Reusing verified prewarm for '{install.name}'."
                    )
                    continue
                ui.info(
                    f"Pre-staging '{install.name}' while it continues "
                    "serving work..."
                )
                future = executor.submit(
                    updater.preflight,
                    release,
                    lock_held=True,
                )
                preflight_futures[future] = (
                    member_index,
                    install,
                    updater,
                )
            for future in as_completed(preflight_futures):
                member_index, install, updater = preflight_futures[future]
                try:
                    result = future.result()
                    if not result["safe_to_apply"]:
                        failures += 1
                        conflicts = result.get("plan", {}).get("conflicts", [])
                        detail = (
                            "conflicts: " + ", ".join(conflicts)
                            if conflicts
                            else "one or more release prerequisites failed"
                        )
                        ui.error(
                            f"Preflight failed for '{install.name}' ({detail}); "
                            "no selected Silicon has been stopped or activated."
                        )
                    else:
                        try:
                            updater.save_preflight(result, release)
                        except Exception as exc:
                            ui.warn(
                                f"Could not persist prewarm for "
                                f"'{install.name}': {exc}"
                            )
                        preflight_results[member_index] = result
                except UpdateConflict as exc:
                    failures += 1
                    ui.error(
                        f"Preflight for '{install.name}' has conflicts; no "
                        "selected Silicon has been stopped or activated: "
                        + ", ".join(exc.plan.conflicts)
                    )
                except Exception as exc:
                    failures += 1
                    ui.error(
                        f"Preflight failed safely for '{install.name}': {exc}"
                    )
        prepared = [
            (install, updater, preflight_results[index])
            for index, (install, updater) in enumerate(updaters)
            if index in preflight_results
        ]
        timings["prestage"] = round(
            time.monotonic() - preflight_started,
            3,
        )
        if failures:
            raise SystemExit(2)
        if len(prepared) > 1:
            ui.success(
                f"Pre-staging completed in "
                f"{time.monotonic() - preflight_started:.1f}s."
            )

        if len(prepared) > 1:
            fleet_journal = FleetJournal.create(
                REGISTRY_DIR,
                release=release.manifest.identity.to_dict(),
                runtime_image=release.manifest.runtime_image,
                members=[
                    {"name": install.name, "path": install.path}
                    for install, _updater, _preflight in prepared
                ],
            )

        activation_started = time.monotonic()
        local_work_conserving = (
            fleet_journal is not None
            and all(not install.is_docker for install, _updater, _ in prepared)
        )
        if local_work_conserving:
            # A canary is a success quota rather than a fixed member. If the
            # first candidate is busy it remains parked while another ready
            # Silicon can establish the canary before the main activation pool
            # opens. Both phases retain at most concurrency productive slots
            # plus concurrency parked drains.
            remaining_indexes = list(range(len(prepared)))
            wave_number = 0
            if resolved_canary_count:
                wave_number += 1
                wave_started = time.monotonic()
                committed_before_wave = len(committed)
                ui.info(
                    "Starting work-conserving canary activation with "
                    f"{resolved_concurrency} productive slot(s) and up to "
                    f"{resolved_concurrency} parked drain(s)."
                )
                with _transaction_signal_guard():
                    (
                        wave_failures,
                        remaining_indexes,
                        wave_stats,
                    ) = _activate_local_fleet_work_conserving(
                        prepared,
                        remaining_indexes,
                        release,
                        fleet_journal,
                        committed,
                        concurrency=resolved_concurrency,
                        deadline_seconds=deadline_seconds,
                        success_limit=resolved_canary_count,
                    )
                failures += wave_failures
                timings["waves"].append(
                    {
                        "wave": wave_number,
                        "kind": "canary",
                        "members": len(committed) - committed_before_wave,
                        **wave_stats,
                        "seconds": round(
                            time.monotonic() - wave_started, 3
                        ),
                    }
                )
            if not failures and remaining_indexes:
                wave_number += 1
                wave_started = time.monotonic()
                committed_before_wave = len(committed)
                ui.info(
                    "Starting work-conserving fleet activation with "
                    f"{resolved_concurrency} productive slot(s) and up to "
                    f"{resolved_concurrency} parked drain(s)."
                )
                with _transaction_signal_guard():
                    (
                        wave_failures,
                        remaining_indexes,
                        wave_stats,
                    ) = _activate_local_fleet_work_conserving(
                        prepared,
                        remaining_indexes,
                        release,
                        fleet_journal,
                        committed,
                        concurrency=resolved_concurrency,
                        deadline_seconds=deadline_seconds,
                    )
                failures += wave_failures
                timings["waves"].append(
                    {
                        "wave": wave_number,
                        "kind": "work-conserving",
                        "members": len(committed) - committed_before_wave,
                        **wave_stats,
                        "seconds": round(
                            time.monotonic() - wave_started, 3
                        ),
                    }
                )
        else:
            # Docker and single-instance updates retain the established wave
            # behavior. The parked-drain scheduler is deliberately local-only.
            waves = _activation_waves(
                len(prepared),
                concurrency=resolved_concurrency,
                canary_count=resolved_canary_count,
            )
            for wave_number, member_indexes in enumerate(waves, start=1):
                if failures:
                    break
                wave_kind = (
                    "canary"
                    if resolved_canary_count and wave_number == 1
                    else "parallel"
                )
                ui.info(
                    f"Starting {wave_kind} activation wave {wave_number}/"
                    f"{len(waves)} for {len(member_indexes)} Silicon(s)."
                )
                wave_started = time.monotonic()
                activation_futures: dict[
                    Future[dict], tuple[
                        int, registry.Install, TransactionalUpdater
                    ]
                ] = {}
                for member_index in member_indexes:
                    install, updater, preflight = prepared[member_index]
                    if fleet_journal is not None:
                        fleet_journal.member(
                            member_index, state="activating", error=""
                        )
                    ui.info(
                        f"Task-safely activating the pre-staged update for "
                        f"'{install.name}'..."
                    )
                with _transaction_signal_guard(), ThreadPoolExecutor(
                    max_workers=len(member_indexes),
                    thread_name_prefix="silicon-activate",
                ) as executor:
                    for member_index in member_indexes:
                        install, updater, preflight = prepared[member_index]
                        future = executor.submit(
                            updater.run,
                            release,
                            deadline=(
                                time.time() + deadline_seconds
                                if deadline_seconds is not None
                                else None
                            ),
                            prepared=preflight,
                            lock_held=True,
                            defer_retention=fleet_journal is not None,
                        )
                        activation_futures[future] = (
                            member_index,
                            install,
                            updater,
                        )
                    for future in as_completed(activation_futures):
                        member_index, install, updater = (
                            activation_futures[future]
                        )
                        try:
                            result = future.result()
                            committed.append(
                                (
                                    member_index,
                                    install,
                                    updater,
                                    str(result["transaction_id"]),
                                )
                            )
                            if fleet_journal is not None:
                                fleet_journal.member(
                                    member_index,
                                    state="committed",
                                    update_transaction_id=str(
                                        result["transaction_id"]
                                    ),
                                    error="",
                                )
                            ui.success(
                                f"'{install.name}' updated transactionally "
                                f"({result['transaction_id']})"
                            )
                        except Exception as exc:
                            failures += 1
                            if fleet_journal is not None:
                                member_state = "failed"
                                try:
                                    if (
                                        updater.status().get(
                                            "active_transaction"
                                        )
                                        is None
                                    ):
                                        member_state = "compensated"
                                except Exception:
                                    pass
                                fleet_journal.member(
                                    member_index,
                                    state=member_state,
                                    error=str(exc),
                                )
                                fleet_journal.set_state("COMPENSATING")
                            ui.error(
                                f"Update failed safely for '{install.name}': "
                                f"{exc}"
                            )
                timings["waves"].append(
                    {
                        "wave": wave_number,
                        "kind": wave_kind,
                        "members": len(member_indexes),
                        "seconds": round(
                            time.monotonic() - wave_started, 3
                        ),
                    }
                )
        timings["activation"] = round(
            time.monotonic() - activation_started,
            3,
        )
        if failures and fleet_journal is not None:
            for index, member in enumerate(fleet_journal.value["members"]):
                if member["state"] == "pending":
                    fleet_journal.member(
                        index, state="compensated", error=""
                    )
        if not failures and fleet_journal is not None:
            fleet_journal.set_state("COMMITTED")
            ui.success(
                f"Fleet activation committed in "
                f"{time.monotonic() - activation_started:.1f}s."
            )
            ui.info(
                "Fleet activation committed; pruning superseded update "
                "assets in the background phase."
            )
            retention_started = time.monotonic()
            with ThreadPoolExecutor(
                max_workers=min(resolved_concurrency, len(committed)),
                thread_name_prefix="silicon-retention",
            ) as executor:
                cleanup_futures = {
                    executor.submit(
                        updater.finalize_retention,
                        transaction_id,
                    ): install
                    for (
                        _member_index,
                        install,
                        updater,
                        transaction_id,
                    ) in committed
                }
                for future in as_completed(cleanup_futures):
                    install = cleanup_futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        ui.warn(
                            f"Deferred cleanup for '{install.name}' will be "
                            f"retried by retention later: {exc}"
                        )
            timings["retention"] = round(
                time.monotonic() - retention_started,
                3,
            )
            ui.success(
                f"Fleet update finished in "
                f"{time.monotonic() - rollout_started:.1f}s."
            )
        if failures and committed:
            ui.warn(
                "Rolling update did not complete; restoring already updated "
                "fleet members in reverse order."
            )
            rollback_targets = list(reversed(committed))
            for (
                member_index,
                _install,
                _updater,
                _source_transaction,
            ) in rollback_targets:
                if fleet_journal is not None:
                    fleet_journal.member(
                        member_index,
                        state="compensating",
                        error="",
                    )
            with ThreadPoolExecutor(
                max_workers=_compensation_worker_count(
                    resolved_concurrency,
                    len(rollback_targets),
                ),
                thread_name_prefix="silicon-compensate",
            ) as executor:
                rollback_futures = {
                    executor.submit(
                        _start_fleet_compensation,
                        updater,
                        source_transaction,
                        deadline_seconds=deadline_seconds,
                    ): (member_index, install)
                    for (
                        member_index,
                        install,
                        updater,
                        source_transaction,
                    ) in rollback_targets
                }
                for future in as_completed(rollback_futures):
                    member_index, install = rollback_futures[future]
                    try:
                        rollback_result = future.result()
                        if fleet_journal is not None:
                            fleet_journal.member(
                                member_index,
                                state="compensated",
                                rollback_transaction_id=str(
                                    rollback_result["transaction_id"]
                                ),
                                error="",
                            )
                        ui.success(
                            f"Restored '{install.name}' to its prior generation."
                        )
                    except Exception as exc:
                        if fleet_journal is not None:
                            fleet_journal.member(
                                member_index,
                                state="failed",
                                error=str(exc),
                            )
                            fleet_journal.set_state("NEEDS_ATTENTION")
                        ui.error(
                            f"Fleet compensation for '{install.name}' needs "
                            f"operator resume: {exc}"
                        )
        if failures and fleet_journal is not None and all(
            member["state"] in {"compensated", "failed"}
            for member in fleet_journal.value["members"]
        ):
            if all(
                member["state"] == "compensated"
                for member in fleet_journal.value["members"]
            ):
                fleet_journal.set_state("COMPENSATED")
            else:
                fleet_journal.set_state("NEEDS_ATTENTION")
    finally:
        for reservation in reversed(acquired):
            reservation.release()
    if failures:
        raise SystemExit(2)
    timings["total"] = round(time.monotonic() - rollout_started, 3)
    return {
        "schema": 1,
        "status": "succeeded",
        "release": release.manifest.identity.version,
        "installations": len(installs),
        "concurrency": resolved_concurrency,
        "canary_count": resolved_canary_count,
        "timings_seconds": timings,
    }


def _parse_duration(value: str) -> float:
    units = {"s": 1.0, "m": 60.0, "h": 3600.0}
    raw = value.strip().lower()
    suffix = raw[-1:] if raw[-1:] in units else "s"
    number = raw[:-1] if raw[-1:] in units else raw
    try:
        seconds = float(number) * units[suffix]
    except ValueError as exc:
        raise UpdateError(f"invalid update deadline: {value}") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise UpdateError("update deadline must be a finite positive duration")
    if seconds > 7 * 24 * 60 * 60:
        raise UpdateError("update deadline cannot exceed 7 days")
    return seconds


def _fleet_option(
    supplied: int | str | None,
    *,
    env_name: str,
    default: int,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    raw: object = supplied
    if raw is None:
        raw = os.environ.get(env_name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise UpdateError(f"{label} must be an integer") from exc
    if value < minimum or value > maximum:
        raise UpdateError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def _activation_waves(
    member_count: int,
    *,
    concurrency: int,
    canary_count: int,
) -> list[list[int]]:
    if member_count <= 0:
        return []
    canaries = min(canary_count, member_count)
    waves: list[list[int]] = []
    offset = 0
    if canaries:
        waves.append(list(range(canaries)))
        offset = canaries
    while offset < member_count:
        end = min(offset + concurrency, member_count)
        waves.append(list(range(offset, end)))
        offset = end
    return waves


def _print_status(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def prewarm_release(target: str | None = None) -> dict[str, object]:
    """Fully prepare local-Python generations without entering maintenance."""

    started = time.monotonic()
    timings: dict[str, float] = {}
    release_started = time.monotonic()
    release = _fetch_latest(_cache())
    timings["resolve_release"] = round(time.monotonic() - release_started, 3)
    selected = _targets(target) if target else registry.installs()
    local_installs = [install for install in selected if not install.is_docker]
    skipped_docker = [install.name for install in selected if install.is_docker]
    if not local_installs:
        raise UpdateError(
            "no host-local Python Silicons were selected for prewarming"
        )
    roots = [Path(item.path) for item in registry.installs()]
    workers = min(DEFAULT_FLEET_CONCURRENCY, len(local_installs))
    prepared: list[str] = []
    preflight_started = time.monotonic()

    def prepare(install: registry.Install) -> str:
        updater = TransactionalUpdater(
            Path(install.path),
            _cache(),
            hooks=_hooks(install),
            all_instances=roots,
        )
        receipt = updater.load_preflight(release)
        if receipt is None:
            receipt = updater.preflight(release)
            updater.save_preflight(receipt, release)
        return install.name

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="silicon-prewarm-local",
    ) as executor:
        futures = {
            executor.submit(prepare, install): install
            for install in local_installs
        }
        for future in as_completed(futures):
            install = futures[future]
            try:
                prepared.append(future.result())
            except Exception as exc:
                raise UpdateError(
                    f"Python prewarm failed for '{install.name}': {exc}"
                ) from exc
    timings["preflight"] = round(
        time.monotonic() - preflight_started,
        3,
    )
    timings["total"] = round(time.monotonic() - started, 3)
    return {
        "schema": 1,
        "status": "succeeded",
        "release": release.manifest.identity.version,
        "runtime": "local-python",
        "prepared": sorted(prepared),
        "skipped_docker": sorted(skipped_docker),
        "timings_seconds": timings,
    }


def update_command(arguments: list[str]) -> None:
    try:
        _update_command(arguments)
    except RuntimeError as exc:
        ui.error(str(exc))
        raise SystemExit(2) from exc


def _update_command(arguments: list[str]) -> None:
    args = list(arguments)
    if args and args[0] in {"check", "trigger"}:
        trigger_update_check(args[1] if len(args) > 1 else None)
        return
    if args and args[0] == "prewarm":
        if len(args) > 2:
            raise UpdateError("Usage: silicon update prewarm [target]")
        prewarm_started = time.monotonic()
        try:
            payload = prewarm_release(args[1] if len(args) == 2 else None)
        except RuntimeError as exc:
            payload = {
                "schema": 1,
                "status": "failed",
                "detail": str(exc),
                "timings_seconds": {
                    "total": round(time.monotonic() - prewarm_started, 3),
                },
            }
            print(PREWARM_MARKER + json.dumps(payload, sort_keys=True))
            raise SystemExit(2) from exc
        print(PREWARM_MARKER + json.dumps(payload, sort_keys=True))
        return
    operation = "run"
    if args and args[0] in {"status", "cancel", "resume", "history", "rollback"}:
        operation = args.pop(0)
    dry_run = False
    deadline_seconds = None
    concurrency = None
    canary_count = None
    all_at_once = False
    positionals: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--dry-run":
            dry_run = True
        elif argument == "--deadline":
            if index + 1 >= len(args):
                raise UpdateError("--deadline requires a duration such as 30m")
            deadline_seconds = _parse_duration(args[index + 1])
            index += 1
        elif argument.startswith("--deadline="):
            deadline_seconds = _parse_duration(argument.split("=", 1)[1])
        elif argument == "--concurrency":
            if index + 1 >= len(args):
                raise UpdateError("--concurrency requires an integer")
            concurrency = _fleet_option(
                args[index + 1],
                env_name="SILICON_UPDATE_CONCURRENCY",
                default=DEFAULT_FLEET_CONCURRENCY,
                label="update concurrency",
                minimum=1,
                maximum=MAX_FLEET_CONCURRENCY,
            )
            index += 1
        elif argument.startswith("--concurrency="):
            concurrency = _fleet_option(
                argument.split("=", 1)[1],
                env_name="SILICON_UPDATE_CONCURRENCY",
                default=DEFAULT_FLEET_CONCURRENCY,
                label="update concurrency",
                minimum=1,
                maximum=MAX_FLEET_CONCURRENCY,
            )
        elif argument == "--canary-count":
            if index + 1 >= len(args):
                raise UpdateError("--canary-count requires an integer")
            canary_count = _fleet_option(
                args[index + 1],
                env_name="SILICON_UPDATE_CANARY_COUNT",
                default=DEFAULT_FLEET_CANARY_COUNT,
                label="canary count",
                minimum=0,
                maximum=MAX_FLEET_CONCURRENCY,
            )
            index += 1
        elif argument.startswith("--canary-count="):
            canary_count = _fleet_option(
                argument.split("=", 1)[1],
                env_name="SILICON_UPDATE_CANARY_COUNT",
                default=DEFAULT_FLEET_CANARY_COUNT,
                label="canary count",
                minimum=0,
                maximum=MAX_FLEET_CONCURRENCY,
            )
        elif argument == "--all-at-once":
            all_at_once = True
        elif argument.startswith("-"):
            raise UpdateError(f"unknown update option: {argument}")
        else:
            positionals.append(argument)
        index += 1
    target = positionals[0] if positionals else None
    transaction_id = positionals[1] if len(positionals) > 1 else None
    try:
        if operation == "run":
            update_instance(
                target,
                dry_run=dry_run,
                deadline_seconds=deadline_seconds,
                concurrency=concurrency,
                canary_count=canary_count,
                all_at_once=all_at_once,
            )
            return
        if operation == "status":
            fleet_history = [
                journal.value for journal in FleetJournal.history(REGISTRY_DIR)
            ]
            if target and registry.is_multi_target(target):
                _print_status(
                    {
                        "instances": {
                            install.name: _engine(install).status()
                            for install in _targets(target)
                        },
                        "fleet_transactions": fleet_history,
                    }
                )
            else:
                install = registry.resolve_one(target)
                updater_status = _engine(install).status()
                value = dict(updater_status)
                value["fleet_transactions"] = fleet_history
                _print_status(value)
            return
        if operation == "resume" and target and registry.is_multi_target(target):
            fleet = FleetJournal.active(REGISTRY_DIR)
            if fleet is None:
                raise UpdateError("no incomplete fleet update to resume")
            _print_status(
                _reconcile_incomplete_fleet(
                    fleet,
                    deadline_seconds=deadline_seconds,
                    concurrency=(
                        concurrency
                        if concurrency is not None
                        else DEFAULT_FLEET_CONCURRENCY
                    ),
                )
            )
            return
        install = registry.resolve_one(target)
        updater = _engine(install)
        if operation == "history":
            _print_status(updater.history())
        elif operation == "cancel":
            _print_status(updater.cancel(transaction_id))
        elif operation == "resume":
            _print_status(updater.resume(transaction_id))
        elif operation == "rollback":
            deadline = (
                time.time() + deadline_seconds
                if deadline_seconds is not None
                else None
            )
            with _transaction_signal_guard():
                _print_status(
                    updater.rollback(
                        deadline=deadline, transaction_id=transaction_id
                    )
                )
    except (UpdateError, FleetJournalError, ReleaseChannelError) as exc:
        ui.error(str(exc))
        raise SystemExit(2) from exc


def update_cli() -> None:
    """Upgrade the published CLI with the interpreter that owns this command."""

    ui.info("Updating silicon CLI...")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "install",
            "--upgrade",
            "silicon-cli",
        ]
    )
    if result.returncode == 0:
        try:
            installed = metadata.version("silicon-cli")
        except metadata.PackageNotFoundError as exc:
            raise UpdateError(
                "pip reported success but silicon-cli is not installed"
            ) from exc
        ui.success(f"CLI updated successfully ({installed})")
    else:
        ui.error("CLI update failed.")
        raise SystemExit(result.returncode)


def trigger_update_check(target: str | None) -> None:
    """Trigger the selected Stemcell's read-only release check."""

    install = registry.resolve_one(target)
    if install.is_docker:
        code = docker_runtime.run_silicon(
            install, ["update-check", install.name]
        )
        if code:
            raise SystemExit(code)
        return
    release_root = active_release_root(install.path)
    updater = release_root / "update.py"
    main_py = release_root / "main.py"
    if updater.exists():
        command = [python_run_cmd(install.path), str(updater)]
    elif main_py.exists():
        command = [python_run_cmd(install.path), str(main_py), "update-check"]
    else:
        ui.error(
            f"'{install.name}' does not contain update.py or main.py in its "
            "active generation."
        )
        raise SystemExit(1)
    ui.info(f"Triggering system update check for '{install.name}'...")
    result = subprocess.run(
        command,
        cwd=install.path,
        env=runtime_environment(install.path),
    )
    if result.returncode == 0:
        ui.success(f"Update check finished for '{install.name}'")
    else:
        ui.error(f"Update check failed for '{install.name}'.")
        raise SystemExit(result.returncode)
