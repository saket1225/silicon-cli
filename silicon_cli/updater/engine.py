"""Transactional, task-aware Silicon updater orchestration."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cache import ReleaseCache
from .checkpoint import normalize_checkpoint, resolve_checkpoint
from .generation import GenerationError, GenerationStore, ManagedPointerMissing
from .io import (
    atomic_write_bytes,
    atomic_write_json,
    ensure_real_directory,
    fsync_dir,
    hash_tree,
    regular_files,
)
from .journal import (
    ORDERED_STATES,
    TERMINAL_STATES,
    FailpointCrash,
    TransactionJournal,
)
from .lock import InstanceLock
from .overlay import OverlayStore
from .planner import UpdatePlan, apply_plan, build_plan
from .policy import RUNTIME_EXACT, RUNTIME_PREFIXES
from .release import (
    FetchedRelease,
    create_artifact,
)
from .retention import RetentionManager


class UpdateError(RuntimeError):
    pass


class UpdateConflict(UpdateError):
    def __init__(self, plan: UpdatePlan):
        super().__init__(
            "local customizations conflict with the new release: "
            + ", ".join(plan.conflicts)
        )
        self.plan = plan


class UpdateCancelled(UpdateError):
    pass


DATA_ROOT_CAPABILITY = ".silicon-data-root-v1"


@dataclass
class EngineHooks:
    """Integration boundary for process supervision and Stemcell maintenance."""

    service_state: Callable[[], dict[str, bool]] = lambda: {
        "main": False,
        "glass_agent": False,
    }
    request_drain: Callable[[str, float | None], None] = lambda _tx, _deadline: None
    await_quiescent: Callable[
        [str, float | None, Callable[[], bool], bool], None
    ] = lambda _tx, _deadline, _cancelled, _running: None
    cancel_drain: Callable[[str], None] = lambda _tx: None
    stop_services: Callable[[], None] = lambda: None
    start_services: Callable[[dict[str, bool]], None] = lambda _state: None
    health_check: Callable[[dict[str, bool]], bool] = lambda _state: True
    begin_maintenance: Callable[[str, str], None] = lambda _tx, _version: None
    reattach_maintenance: Callable[
        [str, str, str], None
    ] = lambda _tx, _version, _phase: None
    set_phase: Callable[[str, str, str], None] = lambda _tx, _phase, _detail: None
    finish: Callable[[str, str], None] = lambda _tx, _outcome: None
    create_checkpoint: Callable[[str, str], dict[str, str]] = (
        lambda _tx, _release: (_ for _ in ()).throw(
            UpdateError("no canonical recovery checkpoint provider is configured")
        )
    )
    verify_checkpoint: Callable[[dict[str, str]], None] = lambda _checkpoint: None
    restore_checkpoint: Callable[[dict[str, str]], None] = (
        lambda _checkpoint: (_ for _ in ()).throw(
            UpdateError("no canonical recovery checkpoint restorer is configured")
        )
    )
    dependency_runner: Callable[[list[str]], int] = lambda command: subprocess.run(
        command, check=False
    ).returncode
    prepare_environment: Callable[[Path, str], Path | None] | None = None


class TransactionalUpdater:
    def __init__(
        self,
        instance: Path,
        cache: ReleaseCache,
        *,
        hooks: EngineHooks | None = None,
        all_instances: list[Path] | None = None,
        keep_generations: int | None = None,
    ):
        self.instance = Path(instance).resolve(strict=True)
        state_root = ensure_real_directory(
            self.instance / ".silicon", root=self.instance
        )
        for name in ("transactions", "work", "releases", "overlays", "maintenance"):
            ensure_real_directory(state_root / name, root=state_root)
        self.cache = cache
        self.hooks = hooks or EngineHooks()
        self.generations = GenerationStore(self.instance)
        self.overlays = OverlayStore(self.instance)
        keep = (
            keep_generations
            if keep_generations is not None
            else int(os.environ.get("SILICON_UPDATE_RETAIN_GENERATIONS", "3"))
        )
        self.retention = RetentionManager(
            self.instance,
            self.cache.root,
            all_instances=all_instances or [self.instance],
            keep_generations=keep,
        )

    def _base_and_local(
        self, work: Path, current: dict[str, Any]
    ) -> tuple[Path, Path]:
        local = self.generations.resolve_release(current)
        upstream_digest = str(current.get("upstream_tree_sha256", ""))
        if upstream_digest:
            base_release = self.cache.load(upstream_digest)
            base = work / "base"
            self.cache.materialize(base_release, base)
            return base, local
        legacy_base = self.instance / ".silicon-upstream" / "base"
        if legacy_base.is_dir():
            return legacy_base, self.instance
        raise UpdateError(
            "This legacy installation has no trustworthy upstream base snapshot. "
            "A recovery checkpoint can protect it, but an automatic merge cannot "
            "distinguish source from local customization."
        )

    @staticmethod
    def _validate_python_sources(candidate: Path) -> None:
        """Compile syntax in memory without importing or executing candidate code."""

        for path in sorted(candidate.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_bytes()
                compile(source, str(path), "exec", dont_inherit=True)
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise UpdateError(
                    f"candidate Python validation failed for "
                    f"{path.relative_to(candidate)}: {exc}"
                ) from exc

    @staticmethod
    def _source_fingerprint(root: Path) -> str:
        digest, _files = hash_tree(
            root,
            excluded_prefixes=RUNTIME_PREFIXES,
            excluded_names=RUNTIME_EXACT,
        )
        return digest

    @staticmethod
    def _prune_source_exclusions(root: Path) -> None:
        """Remove living data and generated files from an immutable source tree."""

        all_files = dict(regular_files(root))
        source_files = {
            relative
            for relative, _path in regular_files(
                root,
                excluded_prefixes=RUNTIME_PREFIXES,
                excluded_names=RUNTIME_EXACT,
            )
        }
        for relative, path in all_files.items():
            if relative not in source_files:
                path.unlink()
                fsync_dir(path.parent)

    def plan(self, release: FetchedRelease) -> dict[str, Any]:
        cached = self.cache.store(release)
        dry_id = f"dry-run-{os.getpid()}-{int(time.time() * 1000)}"
        with InstanceLock(self.instance, dry_id):
            active_transaction = self._select_journal(None, active_only=True)
            if active_transaction is not None:
                raise UpdateError(
                    "cannot plan over an interrupted update; resume it first"
                )
            work = self.instance / ".silicon" / "work" / dry_id
            shutil.rmtree(work, ignore_errors=True)
            try:
                candidate = work / "candidate"
                self.cache.materialize(cached, candidate)
                current = self.generations.current()
                base, local = self._base_and_local(work, current)
                prior_overlay = str(current.get("overlay_root_hash") or "")
                if prior_overlay:
                    self.overlays.verify(prior_overlay)
                plan = build_plan(base, local, candidate)
                data_root_capable = (
                    candidate / DATA_ROOT_CAPABILITY
                ).is_file()
                sequence_safe = True
                try:
                    self._validate_release_sequence(current, cached)
                except UpdateError:
                    sequence_safe = False
                return {
                    "dry_run": True,
                    "release": cached.manifest.identity.to_dict(),
                    "current_generation": current,
                    "plan": plan.to_dict(),
                    "prerequisites": {
                        "data_root_capability": data_root_capable,
                        "release_sequence_safe": sequence_safe,
                    },
                    "safe_to_apply": (
                        not plan.conflicts
                        and data_root_capable
                        and sequence_safe
                    ),
                }
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def _validate_release_sequence(
        self, current: dict[str, Any], release: FetchedRelease
    ) -> None:
        try:
            self.generations.validate_release_candidate(
                release.manifest.identity,
                current=current,
            )
        except GenerationError as exc:
            raise UpdateError(str(exc)) from exc

    def _finish_before_stop(
        self,
        journal: TransactionJournal,
        outcome: str,
    ) -> None:
        """Clear both maintenance owners even if one cleanup path fails."""

        warnings: dict[str, str] = {}
        try:
            self.hooks.cancel_drain(journal.transaction_id)
        except Exception as exc:
            warnings["local_drain_cleanup"] = str(exc)
        try:
            self.hooks.finish(journal.transaction_id, outcome)
        except Exception as exc:
            warnings["carbon_maintenance_cleanup"] = str(exc)
        if warnings:
            journal.merge_metadata(maintenance_cleanup_warning=warnings)

    def _prepare_assets(
        self,
        cached: FetchedRelease,
        current: dict[str, Any],
        work: Path,
        *,
        journal: TransactionJournal | None = None,
        failpoint: str | None = None,
    ) -> tuple[UpdatePlan, dict[str, Any], str]:
        """Fully stage immutable code and dependencies without a maintenance fence."""

        candidate = work / "candidate"
        self.cache.materialize(cached, candidate)
        if journal is not None:
            journal.transition(
                "STAGED", "complete candidate extracted", failpoint=failpoint
            )
        base, local = self._base_and_local(work, current)
        source_before = self._source_fingerprint(local)
        prior_overlay = str(current.get("overlay_root_hash") or "")
        if prior_overlay:
            self.overlays.verify(prior_overlay)
        plan = build_plan(base, local, candidate)
        atomic_write_json(work / "plan.json", plan.to_dict())
        if journal is not None:
            journal.merge_metadata(plan=plan.to_dict())
            journal.transition(
                "PLANNED",
                f"{len(plan.actions)} actions, {len(plan.conflicts)} conflicts",
                failpoint=failpoint,
            )
        if plan.conflicts:
            raise UpdateConflict(plan)
        apply_plan(plan, candidate, local)
        if not (candidate / DATA_ROOT_CAPABILITY).is_file():
            raise UpdateError(
                "candidate does not declare complete SILICON_DATA_ROOT "
                "support; side-by-side activation would split mutable state"
            )
        self._validate_python_sources(candidate)
        overlay_base = work / "overlay-base"
        self.cache.materialize(cached, overlay_base)
        overlay = self.overlays.capture(
            overlay_base,
            candidate,
            base_tree_sha256=cached.manifest.identity.tree_sha256,
        )
        if journal is not None:
            journal.merge_metadata(customization_overlay=overlay)
        materialized_sha, _files = hash_tree(candidate)
        generation_id = (
            f"{cached.manifest.identity.tree_sha256[:16]}-{materialized_sha[:16]}"
        )
        final_release = self.generations.releases / generation_id
        if final_release.exists():
            existing_sha, _ = hash_tree(final_release)
            if existing_sha != materialized_sha:
                raise UpdateError(
                    f"generation identity collision at {final_release}"
                )
            shutil.rmtree(candidate)
        else:
            final_release.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, final_release)
            fsync_dir(final_release.parent)
        environment = self._prepare_dependency_environment(
            final_release,
            cached.manifest.runtime_image,
        )
        release_pointer = final_release.relative_to(self.instance).as_posix()
        environment_pointer = self._environment_pointer(environment)
        new_generation = {
            "generation_id": generation_id,
            "release_path": release_pointer,
            "upstream_tree_sha256": cached.manifest.identity.tree_sha256,
            "materialized_tree_sha256": materialized_sha,
            "environment_path": environment_pointer,
            "release": cached.manifest.identity.to_dict(),
            "overlay_root_hash": overlay["root_hash"],
            "runtime_image": cached.manifest.runtime_image,
        }
        if journal is not None:
            journal.merge_metadata(
                new_generation=new_generation,
                staged_release_path=release_pointer,
                environment_path=environment_pointer,
                source_generation_id=str(
                    current.get("generation_id") or ""
                ),
                source_tree_sha256=source_before,
            )
            journal.transition(
                "DEPENDENCIES_READY",
                "side-by-side environment prepared",
                failpoint=failpoint,
            )
        source_after = self._source_fingerprint(local)
        if source_after != source_before:
            raise UpdateError(
                "active Silicon source changed while the update was being "
                "prepared; nothing was stopped, so retry after the current "
                "task settles"
            )
        return plan, new_generation, source_before

    def _verify_update_source_unchanged(
        self, journal: TransactionJournal
    ) -> None:
        expected_generation = str(
            journal.metadata.get("source_generation_id") or ""
        )
        expected_tree = str(
            journal.metadata.get("source_tree_sha256") or ""
        )
        current = self.generations.current()
        if (
            not expected_generation
            or not expected_tree
            or current.get("generation_id") != expected_generation
        ):
            raise UpdateError(
                "active generation changed after update preflight"
            )
        actual = self._source_fingerprint(
            self.generations.resolve_release(current)
        )
        if actual != expected_tree:
            raise UpdateError(
                "Silicon authored new source changes after update preflight; "
                "nothing was stopped, so retry to merge the newer edits"
            )

    def _prepare_dependency_environment(
        self, release: Path, runtime_image: str = ""
    ) -> Path | None:
        """Build once per host/runtime unless an integration owns the env."""

        if self.hooks.prepare_environment is not None:
            return self.hooks.prepare_environment(release, runtime_image)
        return self.cache.prepare_environment(
            release,
            runner=self.hooks.dependency_runner,
        )

    def _environment_pointer(self, environment: Path | None) -> str:
        if environment is None:
            return ""
        resolved = environment.resolve(strict=True)
        instance_root = (
            self.instance / ".silicon" / "environments"
        ).resolve(strict=False)
        shared_root = self.cache.environments.resolve(strict=False)
        if resolved == instance_root or resolved == shared_root:
            raise UpdateError(
                "prepared dependency environment points at its store root"
            )
        if instance_root in resolved.parents:
            return resolved.relative_to(self.instance).as_posix()
        if shared_root in resolved.parents:
            return str(resolved)
        raise UpdateError(
            "prepared dependency environment escaped every trusted store"
        )

    def preflight(
        self,
        release: FetchedRelease,
        *,
        lock_held: bool = False,
    ) -> dict[str, Any]:
        """Complete every failure-prone staging step without draining services."""

        cached = self.cache.store(release)
        preflight_id = f"preflight-{os.getpid()}-{int(time.time() * 1000)}"
        lock_context = (
            nullcontext()
            if lock_held
            else InstanceLock(self.instance, preflight_id)
        )
        with lock_context:
            active_transaction = self._select_journal(None, active_only=True)
            if active_transaction is not None:
                raise UpdateError(
                    "cannot preflight over an interrupted update; resume it first"
                )
            current = self.generations.current()
            self._validate_release_sequence(current, cached)
            work = self.instance / ".silicon" / "work" / preflight_id
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            try:
                plan, generation, source_tree = self._prepare_assets(
                    cached, current, work
                )
                return {
                    "preflight": True,
                    "release": cached.manifest.identity.to_dict(),
                    "current_generation": current,
                    "plan": plan.to_dict(),
                    "generation": generation,
                    "source_generation_id": str(
                        current.get("generation_id") or ""
                    ),
                    "source_tree_sha256": source_tree,
                    "safe_to_apply": True,
                }
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def run(
        self,
        release: FetchedRelease,
        *,
        deadline: float | None = None,
        failpoint: str | None = None,
        prepared: dict[str, Any] | None = None,
        lock_held: bool = False,
    ) -> dict[str, Any]:
        cached = self.cache.store(release)
        txid = TransactionJournal.new_id()
        activated = False
        stopped = False
        lock_context = (
            nullcontext()
            if lock_held
            else InstanceLock(self.instance, txid)
        )
        with lock_context:
            active_transaction = self._select_journal(None, active_only=True)
            if active_transaction is not None:
                raise UpdateError(
                    "an interrupted update transaction already exists "
                    f"({active_transaction.transaction_id}, "
                    f"{active_transaction.state}); run `silicon update resume` "
                    "before starting another update"
                )
            current = self.generations.current()
            self._validate_release_sequence(current, cached)
            service_state = self.hooks.service_state()
            journal = TransactionJournal.create(
                self.instance,
                {
                    "operation": "update",
                    "release": cached.manifest.identity.to_dict(),
                    "prior_generation": current,
                    "prior_service_state": service_state,
                },
                transaction_id=txid,
            )
            work = self.instance / ".silicon" / "work" / txid
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            try:
                journal.transition(
                    "RESOLVED",
                    f"verified {cached.manifest.identity.tree_sha256}",
                    failpoint=failpoint,
                )
                if prepared is None:
                    _plan, new_generation, _source_tree = (
                        self._prepare_assets(
                            cached,
                            current,
                            work,
                            journal=journal,
                            failpoint=failpoint,
                        )
                    )
                else:
                    (
                        _plan,
                        new_generation,
                    ) = self._adopt_preflight(
                        prepared,
                        cached,
                        current,
                        journal,
                        failpoint=failpoint,
                    )
                if current.get("kind") == "legacy-flat":
                    self._seal_legacy_prior(work, journal)

                self.hooks.begin_maintenance(
                    txid, cached.manifest.identity.version
                )
                self.hooks.request_drain(txid, deadline)
                journal.transition(
                    "DRAIN_REQUESTED",
                    "maintenance fence requested",
                    failpoint=failpoint,
                )
                self.hooks.await_quiescent(
                    txid,
                    deadline,
                    journal.cancellation_requested,
                    bool(service_state.get("main")),
                )
                if journal.cancellation_requested():
                    raise UpdateCancelled("update cancellation requested")
                journal.transition(
                    "QUIESCENT", "all work leases released", failpoint=failpoint
                )
                self._verify_update_source_unchanged(journal)

                self.hooks.set_phase(
                    txid, "checkpointing", "Securing canonical protected data"
                )
                checkpoint = normalize_checkpoint(
                    self.instance,
                    self.hooks.create_checkpoint(
                        txid,
                        str(current.get("generation_id", "legacy-flat")),
                    ),
                )
                self.hooks.verify_checkpoint(
                    resolve_checkpoint(self.instance, checkpoint)
                )
                journal.merge_metadata(recovery_checkpoint=checkpoint)
                journal.transition(
                    "CHECKPOINTED",
                    "canonical local recovery snapshot verified",
                    failpoint=failpoint,
                )

                if journal.cancellation_requested():
                    raise UpdateCancelled("update cancellation requested")
                journal.transition(
                    "STOPPING",
                    "cancel boundary crossed; revalidating maintenance fence",
                    failpoint=failpoint,
                )
                self.hooks.set_phase(
                    txid, "updating", "Stopping services at a safe boundary"
                )
                stopped = True
                self.hooks.stop_services()
                journal.transition(
                    "STOPPED", "previous services stopped", failpoint=failpoint
                )

                new_generation = self._authenticated_activation_generation(
                    journal, new_generation
                )
                journal.merge_metadata(new_generation=new_generation)
                previous = self.generations.activate(new_generation)
                activated = True
                journal.merge_metadata(prior_generation=previous)
                journal.transition(
                    "ACTIVATED",
                    f"activated {new_generation['generation_id']}",
                    failpoint=failpoint,
                )

                self.hooks.start_services(service_state)
                stopped = False
                journal.transition(
                    "STARTED", "prior service state restored", failpoint=failpoint
                )
                self.hooks.set_phase(
                    txid, "validating", "Checking the new Silicon generation"
                )
                if not self.hooks.health_check(service_state):
                    raise UpdateError("new generation failed its health check")
                journal.transition(
                    "VALIDATED", "new generation is healthy", failpoint=failpoint
                )
                self.hooks.finish(txid, "committed")
                journal.transition(
                    "COMMITTED", "update committed", failpoint=failpoint
                )
                try:
                    journal.merge_metadata(retention=self.retention.prune())
                except Exception as cleanup_error:
                    journal.merge_metadata(retention_warning=str(cleanup_error))
                journal.clear_cancel()
                return journal.value
            except (UpdateCancelled, InterruptedError, KeyboardInterrupt) as exc:
                crossed_stop_boundary = journal.state in {
                    "STOPPING",
                    "STOPPED",
                    "ACTIVATED",
                    "STARTED",
                    "VALIDATED",
                }
                if activated or crossed_stop_boundary:
                    self._recover_after_stop_boundary(journal, service_state, exc)
                else:
                    if stopped:
                        self.hooks.start_services(service_state)
                    self._finish_before_stop(journal, "cancelled")
                    journal.transition("CANCELLED", str(exc))
                raise UpdateCancelled(str(exc)) from exc
            except Exception as exc:
                crossed_stop_boundary = journal.state in {
                    "STOPPING",
                    "STOPPED",
                    "ACTIVATED",
                    "STARTED",
                    "VALIDATED",
                }
                if activated or crossed_stop_boundary:
                    self._recover_after_stop_boundary(journal, service_state, exc)
                else:
                    if stopped:
                        self.hooks.start_services(service_state)
                    self._finish_before_stop(journal, "failed")
                    journal.transition("FAILED", str(exc))
                raise
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def _adopt_preflight(
        self,
        prepared: dict[str, Any],
        cached: FetchedRelease,
        current: dict[str, Any],
        journal: TransactionJournal,
        *,
        failpoint: str | None = None,
    ) -> tuple[UpdatePlan, dict[str, Any]]:
        """Attach a fleet-reserved immutable preflight to a transaction."""

        if (
            not isinstance(prepared, dict)
            or prepared.get("preflight") is not True
            or prepared.get("safe_to_apply") is not True
            or prepared.get("release") != cached.manifest.identity.to_dict()
            or prepared.get("current_generation") != current
        ):
            raise UpdateError(
                "fleet preflight no longer matches this instance or release"
            )
        source_generation = str(
            prepared.get("source_generation_id") or ""
        )
        source_tree = str(prepared.get("source_tree_sha256") or "")
        if (
            source_generation != str(current.get("generation_id") or "")
            or not source_tree
            or self._source_fingerprint(
                self.generations.resolve_release(current)
            )
            != source_tree
        ):
            raise UpdateError(
                "Silicon source changed after fleet preflight; no maintenance "
                "fence was entered for this instance"
            )
        raw_generation = prepared.get("generation")
        raw_plan = prepared.get("plan")
        if not isinstance(raw_generation, dict) or not isinstance(raw_plan, dict):
            raise UpdateError("fleet preflight is missing prepared assets")
        generation = dict(raw_generation)
        validation = {
            **generation,
            "schema": 1,
            "kind": "immutable-release",
            "activated_at": time.time(),
        }
        self.generations.validate(validation)
        if (
            generation.get("upstream_tree_sha256")
            != cached.manifest.identity.tree_sha256
        ):
            raise UpdateError("fleet preflight generation has the wrong release")
        conflicts = raw_plan.get("conflicts")
        actions = raw_plan.get("actions")
        if (
            not isinstance(conflicts, list)
            or conflicts
            or not isinstance(actions, list)
        ):
            raise UpdateError("fleet preflight plan is invalid")
        plan = UpdatePlan()
        journal.transition(
            "STAGED",
            "adopted fleet-reserved immutable candidate",
            failpoint=failpoint,
        )
        journal.merge_metadata(plan=raw_plan)
        journal.transition(
            "PLANNED",
            f"{len(actions)} preflight actions, 0 conflicts",
            failpoint=failpoint,
        )
        journal.merge_metadata(
            new_generation=generation,
            staged_release_path=str(generation.get("release_path") or ""),
            environment_path=str(generation.get("environment_path") or ""),
            source_generation_id=source_generation,
            source_tree_sha256=source_tree,
        )
        journal.transition(
            "DEPENDENCIES_READY",
            "fleet-reserved dependencies already prepared",
            failpoint=failpoint,
        )
        return plan, generation

    def _recover_after_stop_boundary(
        self,
        journal: TransactionJournal,
        service_state: dict[str, bool],
        cause: BaseException,
    ) -> None:
        phase_error = ""
        try:
            self.hooks.set_phase(
                journal.transaction_id,
                "rolled_back",
                "Restoring the previous generation",
            )
        except Exception as exc:
            phase_error = str(exc)
        try:
            self._rollback_runtime(journal, service_state)
        except Exception as recovery_error:
            journal.merge_metadata(
                recovery_error=str(recovery_error),
                original_error=str(cause),
                maintenance_phase_error=phase_error,
            )
            raise UpdateError(
                "update failed after the stop boundary and automatic recovery "
                f"needs resume: {recovery_error}"
            ) from cause
        self.hooks.finish(journal.transaction_id, "rolled_back")
        journal.transition("ROLLED_BACK", str(cause))
        if phase_error:
            journal.merge_metadata(maintenance_phase_warning=phase_error)

    def _rollback_runtime(
        self, journal: TransactionJournal, service_state: dict[str, bool]
    ) -> None:
        self.hooks.stop_services()
        self._preserve_candidate_era_data(journal)
        prior = journal.metadata["prior_generation"]
        flat_prior: dict[str, Any] | None = None
        if prior.get("kind") == "legacy-flat":
            flat_prior = prior
            prior = journal.metadata.get("sealed_prior_generation")
            if not isinstance(prior, dict):
                raise UpdateError(
                    "legacy recovery has no authenticated sealed code "
                    "generation; refusing to execute the writable flat tree"
                )
        prior = self._authenticated_recovery_generation(journal, prior)
        if flat_prior is not None:
            self._restore_authenticated_flat_source(
                self.generations.resolve_release(prior),
                expected_tree_sha256=str(
                    prior["materialized_tree_sha256"]
                ),
            )
            self.generations.restore(flat_prior)
        else:
            self.generations.restore(prior)
        checkpoint = journal.metadata.get("recovery_checkpoint")
        if not isinstance(checkpoint, dict):
            raise UpdateError(
                "post-boundary recovery has no verified mutable-data checkpoint"
            )
        recovery = journal.metadata.get("checkpoint_recovery")
        if not isinstance(recovery, dict) or recovery.get("state") != "restored":
            attempts = (
                int(recovery.get("attempts", 0))
                if isinstance(recovery, dict)
                else 0
            )
            journal.merge_metadata(
                checkpoint_recovery={
                    "state": "restoring",
                    "attempts": attempts + 1,
                    "root_hash": checkpoint.get("root_hash", ""),
                }
            )
            resolved = resolve_checkpoint(self.instance, checkpoint)
            self.hooks.verify_checkpoint(resolved)
            self.hooks.restore_checkpoint(resolved)
            # Verify the immutable source once more after restoration. A crash
            # before this durable marker safely repeats the idempotent restore.
            self.hooks.verify_checkpoint(resolved)
            journal.merge_metadata(
                checkpoint_recovery={
                    "state": "restored",
                    "attempts": attempts + 1,
                    "root_hash": checkpoint.get("root_hash", ""),
                }
            )
        self.hooks.start_services(service_state)
        if not self.hooks.health_check(service_state):
            raise UpdateError(
                "the previous generation did not become healthy after recovery"
            )

    def _preserve_candidate_era_data(
        self, journal: TransactionJournal
    ) -> None:
        """Best-effort quarantine of protected writes made before validation.

        Recovery must restore the pre-activation checkpoint, but a candidate's
        startup or migration may already have written protected data. Capture a
        second, verified immutable snapshot while every service is stopped so
        those writes remain recoverable even when the live tree is rewound.
        """

        existing = journal.metadata.get("candidate_era_preservation")
        if isinstance(existing, dict) and existing.get("state") == "verified":
            return
        recovery = journal.metadata.get("checkpoint_recovery")
        if isinstance(recovery, dict) and recovery.get("state") == "restored":
            journal.merge_metadata(
                candidate_era_preservation={
                    "state": "not_available",
                    "detail": (
                        "pre-update checkpoint was already restored before "
                        "candidate-era preservation metadata was recorded"
                    ),
                }
            )
            return
        attempts = (
            int(existing.get("attempts", 0))
            if isinstance(existing, dict)
            else 0
        )
        journal.merge_metadata(
            candidate_era_preservation={
                "state": "capturing",
                "attempts": attempts + 1,
            }
        )
        try:
            active = self.generations.current()
        except GenerationError:
            active = journal.metadata.get("new_generation") or {}
        try:
            checkpoint = normalize_checkpoint(
                self.instance,
                self.hooks.create_checkpoint(
                    journal.transaction_id,
                    "candidate-era:"
                    + str(active.get("generation_id") or "unknown"),
                ),
            )
            resolved = resolve_checkpoint(self.instance, checkpoint)
            self.hooks.verify_checkpoint(resolved)
        except Exception as exc:
            journal.merge_metadata(
                candidate_era_preservation={
                    "state": "failed",
                    "attempts": attempts + 1,
                    "error": str(exc),
                }
            )
            raise UpdateError(
                "automatic recovery stopped before restoring protected data "
                "because candidate-era writes could not be preserved and "
                f"verified: {exc}"
            ) from exc
        journal.merge_metadata(
            candidate_era_preservation={
                "state": "verified",
                "attempts": attempts + 1,
                "checkpoint": checkpoint,
            }
        )

    def resume(self, transaction_id: str | None = None) -> dict[str, Any]:
        journal = self._select_journal(transaction_id, active_only=True)
        if journal is None:
            raise UpdateError("no incomplete update transaction to resume")
        if journal.metadata.get("operation") == "rollback":
            return self._resume_rollback(journal)
        txid = journal.transaction_id
        metadata = journal.metadata
        release_digest = metadata.get("release", {}).get("tree_sha256")
        if not release_digest:
            raise UpdateError("transaction has no verified release identity")
        restart_release: FetchedRelease | None = None
        with InstanceLock(self.instance, txid):
            state = journal.state
            service_state = metadata.get("prior_service_state") or {
                "main": False,
                "glass_agent": False,
            }
            version = str(
                metadata.get("release", {}).get("version")
                if isinstance(metadata.get("release"), dict)
                else ""
            )
            resume_phase = (
                "validating"
                if state in {"STARTED", "VALIDATED"}
                else "updating"
            )
            self.hooks.reattach_maintenance(
                txid, version or release_digest[:12], resume_phase
            )
            try:
                active = self.generations.current()
            except ManagedPointerMissing:
                if state != "STOPPED":
                    raise
                # Activation can crash after publishing the durable managed
                # marker but before current.json. The STOPPED journal contains
                # both authoritative generations, so resume can complete it
                # without guessing or downgrading to flat code.
                active = {}
            prior = metadata.get("prior_generation")
            recovery_started = bool(
                metadata.get("recovery_error")
                or metadata.get("checkpoint_recovery")
                or metadata.get("candidate_era_preservation")
            )
            if (
                state in {"ACTIVATED", "STARTED", "VALIDATED"}
                and isinstance(prior, dict)
                and (
                    recovery_started
                    or active.get("generation_id")
                    == prior.get("generation_id")
                )
            ):
                self._recover_after_stop_boundary(
                    journal,
                    service_state,
                    UpdateError(
                        "resuming interrupted recovery on the prior generation"
                    ),
                )
                return journal.value
            if ORDERED_STATES.index(state) < ORDERED_STATES.index("STOPPING"):
                self.hooks.start_services(service_state)
                self._finish_before_stop(journal, "cancelled")
                journal.transition(
                    "FAILED", "superseded by a fresh transaction during resume"
                )
                restart_release = self.cache.load(release_digest)
            elif state == "STOPPING":
                self.hooks.set_phase(
                    txid, "updating", "Resuming stop at a safe boundary"
                )
                self.hooks.stop_services()
                journal.transition("STOPPED", "service stop resumed")
                state = journal.state
            if restart_release is None and state == "STOPPED":
                activation = self._authenticated_activation_generation(
                    journal, metadata["new_generation"]
                )
                journal.merge_metadata(new_generation=activation)
                self.generations.activate(
                    activation,
                    previous=metadata["prior_generation"],
                )
                journal.transition("ACTIVATED", "activation resumed after interruption")
                state = journal.state
            if restart_release is None and state == "ACTIVATED":
                self.hooks.start_services(service_state)
                journal.transition("STARTED", "service restoration resumed")
                state = journal.state
            if restart_release is None and state == "STARTED":
                self.hooks.set_phase(
                    txid, "validating", "Checking resumed update generation"
                )
                if not self.hooks.health_check(service_state):
                    self._recover_after_stop_boundary(
                        journal,
                        service_state,
                        UpdateError("resumed generation failed health check"),
                    )
                    return journal.value
                journal.transition("VALIDATED", "resumed generation is healthy")
                state = journal.state
            if restart_release is None and state == "VALIDATED":
                self.hooks.finish(txid, "committed")
                journal.transition(
                    "COMMITTED", "resumed transaction committed"
                )
                try:
                    journal.merge_metadata(retention=self.retention.prune())
                except Exception as cleanup_error:
                    journal.merge_metadata(retention_warning=str(cleanup_error))
                journal.clear_cancel()
            result = journal.value
        if restart_release is not None:
            return self.run(restart_release)
        return result

    def _resume_rollback(self, journal: TransactionJournal) -> dict[str, Any]:
        txid = journal.transaction_id
        metadata = journal.metadata
        services = metadata.get("prior_service_state") or {
            "main": False,
            "glass_agent": False,
        }
        desired = metadata.get("new_generation")
        current_before = metadata.get("prior_generation")
        if not isinstance(desired, dict) or not isinstance(current_before, dict):
            raise UpdateError("rollback transaction has incomplete generation metadata")
        target_version = str(
            desired.get("release", {}).get("version")
            if isinstance(desired.get("release"), dict)
            else desired.get("generation_id", "rollback")
        )
        restart_source = str(metadata.get("source_transaction_id") or "")
        restart = False
        with InstanceLock(self.instance, txid):
            state = journal.state
            resume_phase = (
                "validating"
                if state in {"STARTED", "VALIDATED"}
                else "updating"
            )
            self.hooks.reattach_maintenance(
                txid, target_version[:64], resume_phase
            )
            active = self.generations.current()
            recovery_started = bool(
                metadata.get("recovery_error")
                or metadata.get("checkpoint_recovery")
                or metadata.get("candidate_era_preservation")
            )
            if (
                state in {"ACTIVATED", "STARTED", "VALIDATED"}
                and (
                    recovery_started
                    or active.get("generation_id")
                    == current_before.get("generation_id")
                )
            ):
                self._recover_after_stop_boundary(
                    journal,
                    services,
                    UpdateError(
                        "resuming interrupted rollback recovery on its source"
                    ),
                )
                return journal.value
            try:
                if ORDERED_STATES.index(state) < ORDERED_STATES.index("STOPPING"):
                    self.hooks.start_services(services)
                    self._finish_before_stop(journal, "cancelled")
                    journal.transition(
                        "FAILED",
                        "superseded by a fresh rollback transaction during resume",
                    )
                    restart = True
                elif state == "STOPPING":
                    self.hooks.set_phase(
                        txid, "updating", "Resuming rollback stop boundary"
                    )
                    self.hooks.stop_services()
                    journal.transition("STOPPED", "rollback service stop resumed")
                    state = journal.state
                if not restart and state == "STOPPED":
                    if desired.get("kind") == "legacy-flat":
                        self.generations.restore(desired)
                    else:
                        activation = (
                            self._authenticated_activation_generation(
                                journal, desired
                            )
                        )
                        journal.merge_metadata(new_generation=activation)
                        flat_target = metadata.get(
                            "rollback_flat_generation"
                        )
                        if isinstance(flat_target, dict):
                            self._restore_authenticated_flat_source(
                                self.generations.resolve_release(activation),
                                expected_tree_sha256=str(
                                    activation[
                                        "materialized_tree_sha256"
                                    ]
                                ),
                            )
                            self.generations.restore(flat_target)
                        else:
                            self.generations.restore(activation)
                    journal.transition(
                        "ACTIVATED", "rollback generation activation resumed"
                    )
                    state = journal.state
                if not restart and state == "ACTIVATED":
                    self.hooks.start_services(services)
                    journal.transition(
                        "STARTED", "rollback service restoration resumed"
                    )
                    state = journal.state
                if not restart and state == "STARTED":
                    self.hooks.set_phase(
                        txid, "validating", "Checking resumed rollback generation"
                    )
                    if not self.hooks.health_check(services):
                        raise UpdateError(
                            "resumed rollback generation failed health check"
                        )
                    journal.transition(
                        "VALIDATED", "resumed rollback generation is healthy"
                    )
                    state = journal.state
                if not restart and state == "VALIDATED":
                    self.hooks.finish(txid, "committed")
                    journal.transition(
                        "COMMITTED", "resumed rollback transaction committed"
                    )
                    try:
                        journal.merge_metadata(retention=self.retention.prune())
                    except Exception as cleanup_error:
                        journal.merge_metadata(
                            retention_warning=str(cleanup_error)
                        )
                result = journal.value
            except Exception as exc:
                if journal.state in {
                    "STOPPING",
                    "STOPPED",
                    "ACTIVATED",
                    "STARTED",
                    "VALIDATED",
                }:
                    self._recover_after_stop_boundary(journal, services, exc)
                raise
        if restart:
            return self.rollback(transaction_id=restart_source or None)
        return result

    def _reconstruct_activation_baseline(
        self,
        generation: dict[str, Any],
        work: Path,
    ) -> tuple[Path, Path, str]:
        """Recreate a generation exactly as it was when first activated."""

        if generation.get("kind", "immutable-release") != "immutable-release":
            raise UpdateError(
                "the active generation has no immutable activation baseline"
            )
        upstream = str(generation.get("upstream_tree_sha256") or "")
        overlay_hash = str(generation.get("overlay_root_hash") or "")
        expected = str(generation.get("materialized_tree_sha256") or "")
        if not upstream or not overlay_hash or not expected:
            raise UpdateError(
                "the active generation lacks a complete customization identity"
            )
        baseline = work / "active-at-activation"
        self._materialize_authenticated_generation(generation, baseline)
        actual = expected
        active = self.generations.resolve_release(generation)
        if active.is_symlink() or not active.is_dir():
            raise UpdateError("the active generation source is missing or unsafe")
        return baseline, active, actual

    def _legacy_rollback_release(
        self,
        work: Path,
    ) -> tuple[FetchedRelease, Path]:
        """Convert a CLI-seeded legacy base into a cache-verifiable release."""

        base = self.instance / ".silicon-upstream" / "base"
        if base.is_symlink() or not base.is_dir():
            raise UpdateError(
                "legacy rollback target has no trustworthy CLI merge base"
            )
        tree_digest, _files = hash_tree(base)
        artifact = work / "legacy-base.tar"
        release = create_artifact(
            base,
            artifact,
            revision=tree_digest,
            source_label="legacy-cli-seed",
            trust="legacy-cli-seed",
        )
        if release.manifest.identity.tree_sha256 != tree_digest:
            raise UpdateError("legacy rollback base changed while being secured")
        return self.cache.store(release), base

    def _materialize_authenticated_generation(
        self,
        generation: dict[str, Any],
        destination: Path,
    ) -> FetchedRelease:
        """Rebuild a generation only from authenticated immutable inputs."""

        if generation.get("kind", "immutable-release") != "immutable-release":
            raise UpdateError(
                "cannot authenticate a non-immutable generation source"
            )
        upstream = str(generation.get("upstream_tree_sha256") or "")
        overlay_hash = str(generation.get("overlay_root_hash") or "")
        expected = str(generation.get("materialized_tree_sha256") or "")
        if (
            len(upstream) != 64
            or len(overlay_hash) != 64
            or len(expected) != 64
            or any(
                char not in "0123456789abcdef"
                for digest in (upstream, overlay_hash, expected)
                for char in digest
            )
        ):
            raise UpdateError(
                "generation has an invalid immutable recovery identity"
            )
        release = self.cache.load(upstream)
        if release.manifest.identity.to_dict() != generation.get("release"):
            raise UpdateError(
                "generation release metadata disagrees with the verified cache"
            )
        if release.manifest.runtime_image != str(
            generation.get("runtime_image") or ""
        ):
            raise UpdateError(
                "generation runtime image disagrees with the published release"
            )
        shutil.rmtree(destination, ignore_errors=True)
        self.cache.materialize(release, destination)
        self.overlays.apply(overlay_hash, destination)
        actual, _files = hash_tree(destination)
        if actual != expected:
            shutil.rmtree(destination, ignore_errors=True)
            raise UpdateError(
                "reconstructed generation does not match its durable identity"
            )
        return release

    def _seal_legacy_prior(
        self,
        work: Path,
        journal: TransactionJournal,
    ) -> dict[str, Any]:
        """Create a code-only immutable recovery source for a flat install."""

        release, _base = self._legacy_rollback_release(work)
        candidate = work / "sealed-legacy-prior"
        self.cache.materialize(release, candidate)
        self._prune_source_exclusions(candidate)
        sanitized_tree, _files = hash_tree(candidate)
        if sanitized_tree != release.manifest.identity.tree_sha256:
            sanitized_artifact = work / "legacy-code-base.tar"
            release = self.cache.store(
                create_artifact(
                    candidate,
                    sanitized_artifact,
                    revision=sanitized_tree,
                    source_label="legacy-cli-code-base",
                    trust="legacy-cli-seed",
                )
            )
        overlay = self.overlays.capture(
            candidate,
            self.instance,
            base_tree_sha256=release.manifest.identity.tree_sha256,
        )
        self.overlays.apply(overlay["root_hash"], candidate)
        materialized, _files = hash_tree(candidate)
        if materialized != self._source_fingerprint(self.instance):
            raise UpdateError(
                "legacy recovery source changed while it was being sealed"
            )
        generation_id = (
            f"legacy-{release.manifest.identity.tree_sha256[:16]}-"
            f"{materialized[:16]}"
        )
        final = self.generations.releases / generation_id
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise UpdateError("sealed legacy recovery target is unsafe")
            existing, _files = hash_tree(final)
            if existing != materialized:
                raise UpdateError("sealed legacy recovery identity collision")
            shutil.rmtree(candidate)
        else:
            os.replace(candidate, final)
            fsync_dir(final.parent)
        generation = {
            "schema": 1,
            "generation_id": generation_id,
            "kind": "immutable-release",
            "release_path": final.relative_to(self.instance).as_posix(),
            "upstream_tree_sha256": release.manifest.identity.tree_sha256,
            "materialized_tree_sha256": materialized,
            "environment_path": "",
            "release": release.manifest.identity.to_dict(),
            "overlay_root_hash": overlay["root_hash"],
            "runtime_image": release.manifest.runtime_image,
            "activated_at": time.time(),
        }
        self.generations.validate(generation)
        journal.merge_metadata(sealed_prior_generation=generation)
        return generation

    def _authenticated_generation_clone(
        self,
        journal: TransactionJournal,
        generation: dict[str, Any],
        *,
        purpose: str,
    ) -> dict[str, Any]:
        """Stage a fresh tree without trusting a writable prepared copy."""

        work = (
            self.instance
            / ".silicon"
            / "work"
            / f"{purpose}-{journal.transaction_id}"
        )
        work.mkdir(parents=True, exist_ok=True)
        candidate = work / "candidate"
        self._materialize_authenticated_generation(generation, candidate)
        materialized = str(generation["materialized_tree_sha256"])
        upstream = str(generation["upstream_tree_sha256"])
        generation_id = (
            f"{upstream[:16]}-{materialized[:16]}-{purpose}-"
            f"{journal.transaction_id[-12:]}"
        )
        final = self.generations.releases / generation_id
        if final.exists() or final.is_symlink():
            if final.is_symlink() or not final.is_dir():
                raise UpdateError("authenticated recovery target is unsafe")
            existing, _files = hash_tree(final)
            if existing != materialized:
                raise UpdateError(
                    "authenticated recovery generation was modified"
                )
            shutil.rmtree(candidate)
        else:
            os.replace(candidate, final)
            fsync_dir(final.parent)
        cloned = {
            key: value
            for key, value in generation.items()
            if key not in {"schema", "kind", "activated_at"}
        }
        cloned.update(
            {
                "schema": 1,
                "generation_id": generation_id,
                "kind": "immutable-release",
                "release_path": final.relative_to(self.instance).as_posix(),
                "activated_at": time.time(),
            }
        )
        self.generations.validate(cloned)
        journal.merge_metadata(
            **{f"authenticated_{purpose}_generation": cloned}
        )
        shutil.rmtree(work, ignore_errors=True)
        return cloned

    def _authenticated_recovery_generation(
        self,
        journal: TransactionJournal,
        prior: dict[str, Any],
    ) -> dict[str, Any]:
        return self._authenticated_generation_clone(
            journal, prior, purpose="recovery"
        )

    def _authenticated_activation_generation(
        self,
        journal: TransactionJournal,
        generation: dict[str, Any],
    ) -> dict[str, Any]:
        return self._authenticated_generation_clone(
            journal, generation, purpose="activation"
        )

    def _restore_authenticated_flat_source(
        self,
        source: Path,
        *,
        expected_tree_sha256: str,
    ) -> None:
        """Idempotently restore legacy code while leaving living data intact."""

        source = source.resolve(strict=True)
        actual, _files = hash_tree(source)
        if actual != expected_tree_sha256:
            raise UpdateError("authenticated legacy recovery source changed")
        source_files = dict(regular_files(source))
        local_files = dict(
            regular_files(
                self.instance,
                excluded_prefixes=RUNTIME_PREFIXES,
                excluded_names=RUNTIME_EXACT,
            )
        )
        for relative, source_path in source_files.items():
            target = self.instance / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = source_path.stat().st_mode & 0o777
            atomic_write_bytes(target, source_path.read_bytes(), mode=mode)
        for relative, target in sorted(
            local_files.items(), reverse=True
        ):
            if relative not in source_files:
                target.unlink(missing_ok=True)
                fsync_dir(target.parent)
        if self._source_fingerprint(self.instance) != expected_tree_sha256:
            raise UpdateError(
                "legacy flat source did not match after authenticated restore"
            )

    def _prepare_rollback_generation(
        self,
        current: dict[str, Any],
        desired: dict[str, Any],
        work: Path,
        journal: TransactionJournal,
    ) -> dict[str, Any]:
        """Carry post-activation self-edits onto a new rollback generation."""

        journal.transition("RESOLVED", "rollback source generations resolved")
        activation_base, active, activation_hash = (
            self._reconstruct_activation_baseline(current, work)
        )
        source_before, _files = hash_tree(active)
        delta_overlay = self.overlays.capture(
            activation_base,
            active,
            base_tree_sha256=activation_hash,
        )
        source_after, _files = hash_tree(active)
        if source_after != source_before:
            raise UpdateError(
                "active Silicon source changed while rollback customizations "
                "were being captured; retry after the current task settles"
            )
        journal.merge_metadata(
            rollback_customization_delta=delta_overlay,
            rollback_source_tree_sha256=source_before,
        )
        delta_manifest = self.overlays.verify(delta_overlay["root_hash"])
        delta_has_changes = bool(
            delta_manifest["files"] or delta_manifest["tombstones"]
        )

        candidate = work / "rollback-candidate"
        if desired.get("kind") == "immutable-release":
            upstream = str(desired.get("upstream_tree_sha256") or "")
            if not upstream:
                raise UpdateError(
                    "rollback target has no verified upstream release identity"
                )
            target_release = self.cache.load(upstream)
            target_base = work / "rollback-upstream"
            self.cache.materialize(target_release, target_base)
            self._materialize_authenticated_generation(desired, candidate)
        elif desired.get("kind") == "legacy-flat":
            target_release, legacy_base = self._legacy_rollback_release(work)
            target_base = work / "rollback-upstream"
            self.cache.materialize(target_release, target_base)
            shutil.copytree(target_base, candidate)
            # Recreate supported legacy source customizations without copying
            # mutable state or updater internals into the code generation.
            legacy_plan = build_plan(legacy_base, self.instance, candidate)
            if legacy_plan.conflicts:
                raise UpdateConflict(legacy_plan)
            apply_plan(legacy_plan, candidate, self.instance)
        else:
            raise UpdateError("rollback target generation kind is unsupported")

        if current.get("kind") == "immutable-release":
            current_upstream = work / "rollback-current-upstream"
            current_release = self.cache.load(
                str(current.get("upstream_tree_sha256") or "")
            )
            self.cache.materialize(current_release, current_upstream)
            established_plan = build_plan(
                current_upstream,
                activation_base,
                candidate,
            )
            journal.merge_metadata(
                rollback_established_customization_plan=(
                    established_plan.to_dict()
                )
            )
            if established_plan.conflicts:
                raise UpdateConflict(established_plan)
            apply_plan(established_plan, candidate, activation_base)
        journal.transition("STAGED", "rollback target copied side by side")

        plan = build_plan(activation_base, active, candidate)
        journal.merge_metadata(rollback_plan=plan.to_dict())
        journal.transition(
            "PLANNED",
            f"{len(plan.actions)} rollback merge actions, "
            f"{len(plan.conflicts)} conflicts",
        )
        if plan.conflicts:
            raise UpdateConflict(plan)
        apply_plan(plan, candidate, active)
        if not (candidate / DATA_ROOT_CAPABILITY).is_file():
            flat_target = journal.metadata.get("rollback_flat_generation")
            if isinstance(flat_target, dict):
                # The authenticated candidate will be copied back into the
                # legacy flat code locations only after services stop. Living
                # data remains in place and is restored from the checkpoint.
                pass
            elif desired.get("kind") == "legacy-flat" and not delta_has_changes:
                # A pre-data-root legacy target can only be reactivated in its
                # original flat form.  This compatibility path is safe solely
                # when there are no post-activation source edits to carry.
                shutil.rmtree(candidate, ignore_errors=True)
                journal.merge_metadata(
                    rollback_target_generation=desired,
                    new_generation=desired,
                    rollback_customization_overlay=delta_overlay,
                    staged_release_path="",
                    environment_path=str(
                        desired.get("environment_path") or ""
                    ),
                )
                journal.transition(
                    "DEPENDENCIES_READY",
                    "unchanged pre-data-root legacy rollback target verified",
                )
                return desired
            else:
                raise UpdateError(
                    "rollback target does not support the separate durable data "
                    "root; the self-authored delta remains preserved as overlay "
                    f"{delta_overlay['root_hash']} and was not hidden"
                )
        self._validate_python_sources(candidate)

        target_upstream = target_release.manifest.identity.tree_sha256
        overlay = self.overlays.capture(
            target_base,
            candidate,
            base_tree_sha256=target_upstream,
        )
        materialized_sha, _files = hash_tree(candidate)
        generation_id = (
            f"{target_upstream[:16]}-{materialized_sha[:16]}"
        )
        final_release = self.generations.releases / generation_id
        if final_release.exists():
            if final_release.is_symlink() or not final_release.is_dir():
                raise UpdateError("rollback generation target is unsafe")
            existing_sha, _files = hash_tree(final_release)
            if existing_sha != materialized_sha:
                raise UpdateError(
                    f"rollback generation identity collision at {final_release}"
                )
            shutil.rmtree(candidate)
        else:
            final_release.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, final_release)
            fsync_dir(final_release.parent)

        environment = self._prepare_dependency_environment(
            final_release,
            target_release.manifest.runtime_image,
        )
        environment_pointer = self._environment_pointer(environment)

        generation = {
            "generation_id": generation_id,
            "release_path": final_release.relative_to(self.instance).as_posix(),
            "upstream_tree_sha256": target_upstream,
            "materialized_tree_sha256": materialized_sha,
            "environment_path": environment_pointer,
            "release": target_release.manifest.identity.to_dict(),
            "overlay_root_hash": overlay["root_hash"],
            "runtime_image": target_release.manifest.runtime_image,
        }
        journal.merge_metadata(
            rollback_target_generation=desired,
            new_generation=generation,
            rollback_customization_overlay=overlay,
            staged_release_path=generation["release_path"],
            environment_path=environment_pointer,
        )
        journal.transition(
            "DEPENDENCIES_READY",
            "rollback customizations and dependencies prepared",
        )
        return generation

    def _verify_rollback_source_unchanged(
        self, journal: TransactionJournal
    ) -> None:
        expected = str(
            journal.metadata.get("rollback_source_tree_sha256") or ""
        )
        prior = journal.metadata.get("prior_generation")
        current = self.generations.current()
        if (
            not expected
            or not isinstance(prior, dict)
            or current.get("generation_id") != prior.get("generation_id")
        ):
            raise UpdateError(
                "active generation changed after rollback was prepared"
            )
        actual, _files = hash_tree(self.generations.resolve_release(current))
        if actual != expected:
            raise UpdateError(
                "Silicon authored new source changes after rollback preflight; "
                "nothing was stopped, so retry to preserve the newer edits"
            )

    def rollback(
        self,
        *,
        deadline: float | None = None,
        transaction_id: str | None = None,
        lock_held: bool = False,
    ) -> dict[str, Any]:
        txid = TransactionJournal.new_id()
        lock_context = (
            nullcontext()
            if lock_held
            else InstanceLock(self.instance, txid)
        )
        with lock_context:
            active_transaction = self._select_journal(None, active_only=True)
            if active_transaction is not None:
                raise UpdateError(
                    "an interrupted update transaction must be resumed before rollback"
                )
            source = self._select_journal(
                transaction_id, committed_only=True
            )
            if source is None or not source.metadata.get("prior_generation"):
                raise UpdateError(
                    "no committed update generation is available to roll back"
                )
            desired = source.metadata["prior_generation"]
            rollback_flat_generation: dict[str, Any] | None = None
            if desired.get("kind") == "legacy-flat":
                sealed = source.metadata.get("sealed_prior_generation")
                if not isinstance(sealed, dict):
                    raise UpdateError(
                        "the legacy rollback target predates authenticated "
                        "recovery sealing and cannot be executed safely"
                    )
                rollback_flat_generation = desired
                desired = sealed
            current = self.generations.current()
            if desired.get("generation_id") == current.get("generation_id"):
                raise UpdateError(
                    "the requested prior generation is already active"
                )
            services = self.hooks.service_state()
            journal = TransactionJournal.create(
                self.instance,
                {
                    "operation": "rollback",
                    "source_transaction_id": source.transaction_id,
                    "prior_generation": current,
                    "new_generation": desired,
                    "prior_service_state": services,
                    **(
                        {"rollback_flat_generation": rollback_flat_generation}
                        if rollback_flat_generation is not None
                        else {}
                    ),
                },
                transaction_id=txid,
            )
            work = self.instance / ".silicon" / "work" / txid
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            try:
                prepared_generation = self._prepare_rollback_generation(
                    current,
                    desired,
                    work,
                    journal,
                )
                target_version = str(
                    prepared_generation.get("release", {}).get("version")
                    or prepared_generation.get("generation_id")
                    or "rollback"
                )
                self.hooks.begin_maintenance(txid, target_version[:64])
                self.hooks.request_drain(txid, deadline)
                journal.transition(
                    "DRAIN_REQUESTED", "rollback maintenance fence requested"
                )
                self.hooks.await_quiescent(
                    txid,
                    deadline,
                    journal.cancellation_requested,
                    bool(services.get("main")),
                )
                if journal.cancellation_requested():
                    raise UpdateCancelled("rollback cancellation requested")
                journal.transition("QUIESCENT", "all work leases released")
                self._verify_rollback_source_unchanged(journal)
                self.hooks.set_phase(
                    txid, "checkpointing", "Securing canonical protected data"
                )
                checkpoint = normalize_checkpoint(
                    self.instance,
                    self.hooks.create_checkpoint(
                        txid, str(current.get("generation_id", "unknown"))
                    ),
                )
                self.hooks.verify_checkpoint(
                    resolve_checkpoint(self.instance, checkpoint)
                )
                journal.merge_metadata(recovery_checkpoint=checkpoint)
                journal.transition(
                    "CHECKPOINTED", "pre-rollback recovery point verified"
                )
                if journal.cancellation_requested():
                    raise UpdateCancelled("rollback cancellation requested")
                journal.transition(
                    "STOPPING", "rollback cancel boundary crossed"
                )
                self.hooks.set_phase(
                    txid, "updating", "Stopping at the verified safe boundary"
                )
                self.hooks.stop_services()
                journal.transition("STOPPED", "current services stopped")
                if prepared_generation.get("kind") == "legacy-flat":
                    self.generations.restore(prepared_generation)
                else:
                    prepared_generation = (
                        self._authenticated_activation_generation(
                            journal, prepared_generation
                        )
                    )
                    journal.merge_metadata(
                        new_generation=prepared_generation
                    )
                    flat_target = journal.metadata.get(
                        "rollback_flat_generation"
                    )
                    if isinstance(flat_target, dict):
                        self._restore_authenticated_flat_source(
                            self.generations.resolve_release(
                                prepared_generation
                            ),
                            expected_tree_sha256=str(
                                prepared_generation[
                                    "materialized_tree_sha256"
                                ]
                            ),
                        )
                        self.generations.restore(flat_target)
                    else:
                        self.generations.activate(
                            prepared_generation,
                            allow_release_rollback=True,
                        )
                journal.transition(
                    "ACTIVATED",
                    "rollback generation with preserved customizations activated",
                )
                self.hooks.start_services(services)
                journal.transition("STARTED", "prior service state restored")
                self.hooks.set_phase(
                    txid, "validating", "Checking the rollback generation"
                )
                if not self.hooks.health_check(services):
                    raise UpdateError("rollback target failed health check")
                journal.transition("VALIDATED", "rollback target is healthy")
                self.hooks.finish(txid, "committed")
                journal.transition("COMMITTED", "rollback committed")
                try:
                    journal.merge_metadata(retention=self.retention.prune())
                except Exception as cleanup_error:
                    journal.merge_metadata(retention_warning=str(cleanup_error))
                return journal.value
            except (UpdateCancelled, InterruptedError, KeyboardInterrupt) as exc:
                if journal.state in {
                    "STOPPING",
                    "STOPPED",
                    "ACTIVATED",
                    "STARTED",
                    "VALIDATED",
                }:
                    self._recover_after_stop_boundary(journal, services, exc)
                else:
                    self._finish_before_stop(journal, "cancelled")
                    journal.transition("CANCELLED", str(exc))
                raise UpdateCancelled(str(exc)) from exc
            except Exception as exc:
                if journal.state in {
                    "STOPPING",
                    "STOPPED",
                    "ACTIVATED",
                    "STARTED",
                    "VALIDATED",
                }:
                    self._recover_after_stop_boundary(journal, services, exc)
                else:
                    self._finish_before_stop(journal, "failed")
                    journal.transition("FAILED", str(exc))
                raise
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def cancel(self, transaction_id: str | None = None) -> dict[str, Any]:
        journal = self._select_journal(transaction_id, active_only=True)
        if journal is None:
            raise UpdateError("no active update transaction to cancel")
        if journal.state in {
            "STOPPING",
            "STOPPED",
            "ACTIVATED",
            "STARTED",
            "VALIDATED",
        }:
            raise UpdateError(
                "the update has crossed the stop boundary; use resume so it can "
                "commit or roll back safely"
            )
        journal.request_cancel()
        self.hooks.cancel_drain(journal.transaction_id)
        return journal.value

    def status(self) -> dict[str, Any]:
        history = TransactionJournal.history(self.instance)
        active = next((j for j in history if j.state not in TERMINAL_STATES), None)
        latest = history[0] if history else None
        return {
            "current_generation": self.generations.current(),
            "active_transaction": active.value if active else None,
            "latest_transaction": latest.value if latest else None,
        }

    def history(self) -> list[dict[str, Any]]:
        return [journal.value for journal in TransactionJournal.history(self.instance)]

    def _select_journal(
        self,
        transaction_id: str | None,
        *,
        active_only: bool = False,
        committed_only: bool = False,
    ) -> TransactionJournal | None:
        for journal in TransactionJournal.history(self.instance):
            if transaction_id and journal.transaction_id != transaction_id:
                continue
            if active_only and journal.state in TERMINAL_STATES:
                continue
            if committed_only and journal.state != "COMMITTED":
                continue
            return journal
        return None


__all__ = [
    "EngineHooks",
    "FailpointCrash",
    "TransactionalUpdater",
    "UpdateCancelled",
    "UpdateConflict",
    "UpdateError",
]
