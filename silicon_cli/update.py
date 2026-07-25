"""CLI-owned transactional Silicon updates and CLI self-updates."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import docker_runtime, glassagent, process, registry, ui
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
from .updater.maintenance import MaintenanceError, MaintenanceProtocol
from .updater.release import FetchedRelease


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
        }

    maintenance = MaintenanceProtocol(
        Path(inst.path),
        legacy_offline_safe=lambda: not any(state().values()),
    )

    def stop_services() -> None:
        if maintenance.legacy_offline and any(state().values()):
            raise UpdateError(
                "legacy Silicon became active after its offline update fence; "
                "refusing to stop a possibly active task"
            )
        process.stop_one(inst.name, full=True)

    def start_services(previous: dict[str, bool]) -> None:
        if previous.get("main"):
            process.start_one(
                inst.name,
                start_agent=bool(previous.get("glass_agent")),
                reconcile_updates=False,
            )
        elif previous.get("glass_agent"):
            glassagent.start(inst.path)

    def healthy(previous: dict[str, bool]) -> bool:
        # Require three consecutive healthy observations. A process that merely
        # exists for one instant is not a successful restart.
        consecutive = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            main_ok = (
                process.runtime_ready(
                    inst.path,
                    inst.pid_file,
                    min_uptime=5.0,
                    max_heartbeat_age=5.0,
                )
                if previous.get("main")
                else not process.is_running(inst.pid_file)
            )
            agent_ok = glassagent.status(inst.path) == bool(
                previous.get("glass_agent")
            )
            if main_ok and agent_ok:
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
                "from core.backup import create_local_snapshot,verify_local_snapshot;"
                "root=Path(sys.argv[2]);"
                "store=root/'.silicon'/'snapshots';"
                "result=create_local_snapshot(root,release_id=sys.argv[3],store=store);"
                "verify_local_snapshot(result.manifest,store=store);"
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
            return snapshot_command(
                "create",
                {"release_id": snapshot_release},
            )
        except UpdateError as exc:
            message = str(exc)
            if not any(
                marker in message
                for marker in (
                    "cannot import name 'create_local_snapshot'",
                    'cannot import name "create_local_snapshot"',
                    "No module named 'core.backup'",
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
        value = parse_result(
            docker_runtime.run_active_python(
                inst,
                [
                    "-m",
                    "core.maintenance",
                    "--root",
                    docker_runtime.CONTAINER_PATH,
                    *arguments,
                ],
            ),
            "Docker task-safe maintenance coordinator",
        )
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
        }

    coordinator_available = docker_runtime.maintenance_coordinator_available(inst)
    maintenance = (
        MaintenanceProtocol(root, command=coordinator_command)
        if coordinator_available
        else MaintenanceProtocol(
            root,
            legacy_offline_safe=lambda: not any(state().values()),
        )
    )

    def begin_maintenance(transaction_id: str, target_version: str) -> None:
        # A running legacy container cannot truthfully enter a task-safe drain.
        # Reject before publishing a Carbon-visible Glass lease.
        if not coordinator_available and any(state().values()):
            raise MaintenanceError(
                "this legacy Docker Stemcell has no task-safe coordinator; "
                f"run `silicon stop --full {inst.name}` from the host, then "
                "retry the update"
            )
        maintenance.begin(transaction_id, target_version)

    def stop_services() -> None:
        if docker_runtime.container_running(inst):
            docker_runtime.stop_one(inst, full=True)
        if docker_runtime.container_running(inst):
            raise UpdateError(
                f"Docker container for '{inst.name}' did not stop cleanly"
            )

    def start_services(previous: dict[str, bool]) -> None:
        container = bool(
            previous.get(
                "container",
                previous.get("main") or previous.get("glass_agent"),
            )
        )
        docker_runtime.restore_one(
            inst,
            container=container,
            main=bool(previous.get("main")),
            glass_agent=bool(previous.get("glass_agent")),
            reconcile=False,
            allow_legacy_fence=maintenance.legacy_offline,
        )

    def healthy(previous: dict[str, bool]) -> bool:
        expected_container = bool(
            previous.get(
                "container",
                previous.get("main") or previous.get("glass_agent"),
            )
        )
        consecutive = 0
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            container = docker_runtime.container_running(inst)
            main = (
                docker_runtime.silicon_ready(
                    inst,
                    min_uptime=5.0,
                    max_heartbeat_age=5.0,
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
            if (
                container == expected_container
                and main == bool(previous.get("main"))
                and agent == bool(previous.get("glass_agent"))
            ):
                consecutive += 1
                if consecutive >= 3:
                    return True
            else:
                consecutive = 0
            time.sleep(0.5)
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
                "from core.backup import create_local_snapshot,verify_local_snapshot;"
                "root=Path(sys.argv[1]);"
                "store=root/'.silicon'/'snapshots';"
                "result=create_local_snapshot(root,release_id=sys.argv[2],store=store);"
                "verify_local_snapshot(result.manifest,store=store);"
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
            return snapshot_command(
                "create",
                {"release_id": snapshot_release},
            )
        except UpdateError as exc:
            message = str(exc)
            if not any(
                marker in message
                for marker in (
                    "cannot import name 'create_local_snapshot'",
                    'cannot import name "create_local_snapshot"',
                    "No module named 'core.backup'",
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


def _reconcile_incomplete_fleet(
    fleet: FleetJournal | None = None,
    *,
    deadline_seconds: float | None = None,
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
            if member["state"] not in {"activating", "failed"}:
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
                result = updater.resume(str(active["transaction_id"]))
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
        for index in reversed(range(len(updaters))):
            updater = updaters[index]
            member = fleet.value["members"][index]
            if member["state"] in {"pending", "failed", "activating"}:
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
            status = updater.status()
            active = status.get("active_transaction")
            rollback_result = None
            if isinstance(active, dict):
                metadata = active.get("metadata")
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("operation") != "rollback"
                    or metadata.get("source_transaction_id")
                    != source_transaction
                ):
                    raise UpdateError(
                        f"'{member['name']}' has an unrelated active "
                        "transaction during fleet compensation"
                    )
                rollback_result = updater.resume(
                    str(active["transaction_id"])
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
                    rollback_result = prior_rollback
                else:
                    rollback_result = updater.rollback(
                        deadline=(
                            time.time() + deadline_seconds
                            if deadline_seconds is not None
                            else None
                        ),
                        transaction_id=source_transaction,
                        lock_held=True,
                    )
            if rollback_result.get("state") != "COMMITTED":
                raise UpdateError(
                    f"fleet compensation for '{member['name']}' did not commit"
                )
            fleet.member(
                index,
                state="compensated",
                rollback_transaction_id=str(
                    rollback_result["transaction_id"]
                ),
                error="",
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


def update_instance(
    target: str | None,
    *,
    dry_run: bool = False,
    deadline_seconds: float | None = None,
) -> None:
    installs = _targets(target)
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
        )
    if len(installs) > 1 and target in {"all", "*"}:
        if not ui.confirm(
            "Are you sure you want to update: "
            + ", ".join(install.name for install in installs)
            + "?"
        ):
            return
    cache = _cache()
    release = _fetch_latest(cache)
    docker_installs = [install for install in installs if install.is_docker]
    if docker_installs and not dry_run:
        runtime_image = release.manifest.runtime_image
        if not runtime_image:
            raise UpdateError(
                "Docker updates require the published Stemcell Git tag to "
                "declare an immutable runtime_image digest"
            )
        runtime_config = docker_runtime.prepare_release_image(runtime_image)
        docker_runtime.verify_runtime_contract(
            runtime_config,
            runtime_image,
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
        return

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
        for install, updater in updaters:
            try:
                ui.info(
                    f"Pre-staging '{install.name}' while it continues "
                    "serving work..."
                )
                result = updater.preflight(release, lock_held=True)
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
                    prepared.append((install, updater, result))
            except UpdateConflict as exc:
                failures += 1
                ui.error(
                    f"Preflight for '{install.name}' has conflicts; no selected "
                    "Silicon has been stopped or activated: "
                    + ", ".join(exc.plan.conflicts)
                )
            except Exception as exc:
                failures += 1
                ui.error(
                    f"Preflight failed safely for '{install.name}': {exc}"
                )
        if failures:
            raise SystemExit(2)

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

        # Fleet locks retain the exact preflight assets until every rolling
        # activation commits. If a later member fails, already committed
        # members are transactionally compensated in reverse order.
        for member_index, (install, updater, preflight) in enumerate(prepared):
            try:
                if fleet_journal is not None:
                    fleet_journal.member(
                        member_index, state="activating", error=""
                    )
                ui.info(
                    f"Task-safely activating the pre-staged update for "
                    f"'{install.name}'..."
                )
                with _transaction_signal_guard():
                    result = updater.run(
                        release,
                        deadline=(
                            time.time() + deadline_seconds
                            if deadline_seconds is not None
                            else None
                        ),
                        prepared=preflight,
                        lock_held=True,
                    )
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
                        update_transaction_id=str(result["transaction_id"]),
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
                            updater.status().get("active_transaction")
                            is None
                        ):
                            # The engine either failed before its stop boundary
                            # or completed automatic rollback. No mutation from
                            # this member remains to compensate.
                            member_state = "compensated"
                    except Exception:
                        pass
                    fleet_journal.member(
                        member_index,
                        state=member_state,
                        error=str(exc),
                    )
                    fleet_journal.set_state("COMPENSATING")
                ui.error(f"Update failed safely for '{install.name}': {exc}")
                break
        if failures and fleet_journal is not None:
            for index, member in enumerate(fleet_journal.value["members"]):
                if member["state"] == "pending":
                    fleet_journal.member(
                        index, state="compensated", error=""
                    )
        if not failures and fleet_journal is not None:
            fleet_journal.set_state("COMMITTED")
        if failures and committed:
            ui.warn(
                "Rolling update did not complete; restoring already updated "
                "fleet members in reverse order."
            )
            for (
                member_index,
                install,
                updater,
                source_transaction,
            ) in reversed(committed):
                try:
                    if fleet_journal is not None:
                        fleet_journal.member(
                            member_index,
                            state="compensating",
                            error="",
                        )
                    rollback_result = updater.rollback(
                        deadline=(
                            time.time() + deadline_seconds
                            if deadline_seconds is not None
                            else None
                        ),
                        transaction_id=source_transaction,
                        lock_held=True,
                    )
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


def _print_status(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


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
    operation = "run"
    if args and args[0] in {"status", "cancel", "resume", "history", "rollback"}:
        operation = args.pop(0)
    dry_run = False
    deadline_seconds = None
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
        [sys.executable, "-m", "pip", "install", "--upgrade", "silicon-cli"]
    )
    if result.returncode == 0:
        ui.success("CLI updated to latest version")
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
