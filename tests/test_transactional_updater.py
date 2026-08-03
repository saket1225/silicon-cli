from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from silicon_cli.config import active_release_root
from silicon_cli.updater import cache as cache_module
from silicon_cli.updater import generation as generation_module
from silicon_cli.updater import overlay as overlay_module
from silicon_cli.updater.cache import ReleaseCache, runtime_platform_identity
from silicon_cli.updater.engine import (
    EngineHooks,
    TransactionalUpdater,
    UpdateConflict,
    UpdateError,
)
from silicon_cli.updater.generation import GenerationError
from silicon_cli.updater.io import hash_tree
from silicon_cli.updater.journal import (
    FailpointCrash,
    JournalCorruption,
    TransactionJournal,
)
from silicon_cli.updater.lock import InstanceLock, UpdateLocked
from silicon_cli.updater.maintenance import MaintenanceProtocol
from silicon_cli.updater.overlay import OverlayStore
from silicon_cli.updater.planner import apply_plan, build_plan, seed_legacy_snapshot
from silicon_cli.updater.release import (
    PUBLISHED_GIT_TRUST,
    ReleaseVerificationError,
    create_artifact,
    safe_extract,
)
from silicon_cli.updater.snapshot_adapter import (
    create_local_snapshot,
    restore_local_snapshot_in_place,
    verify_local_snapshot,
)


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.old = root / "old"
        self.instance = root / "instance"
        self.new = root / "new"
        self.cache = ReleaseCache(root / "cache")
        for directory in (self.old, self.instance, self.new):
            directory.mkdir()
        self._write(self.old, "main.py", "print('old')\n")
        self._write(self.old, "core/tool.py", "VALUE = 'old'\n")
        self._write(self.instance, "main.py", "print('old')\n")
        self._write(self.instance, "core/tool.py", "VALUE = 'old'\n")
        self._write(self.instance, "prompts/MEMORY.md", "important memory\n")
        self._write(self.instance, "silicon.json", '{"address":"ada"}\n')
        self._write(self.new, "main.py", "print('new')\n")
        self._write(self.new, "core/tool.py", "VALUE = 'new'\n")
        self._write(self.new, ".silicon-data-root-v1", "1\n")
        seed_legacy_snapshot(self.old, self.instance)
        self.release = create_artifact(
            self.new,
            root / "release.tar",
            revision="a" * 40,
            source_label="test@example.invalid@" + "a" * 40,
            trust="test-fixture",
        )
        self.services = {"main": True, "glass_agent": True}
        self.events: list[str] = []

    @staticmethod
    def _write(root: Path, relative: str, value: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def hooks(self, *, healthy: bool = True) -> EngineHooks:
        checkpoint_memory: dict[str, str] = {}
        health_checks = 0

        def stop():
            self.events.append("stop")
            self.services["main"] = False
            self.services["glass_agent"] = False

        def start(previous):
            self.events.append("start")
            self.services.update(previous)

        def checkpoint(transaction_id, _release_id):
            self.events.append("checkpoint")
            value = (
                self.instance / "prompts" / "MEMORY.md"
            ).read_text()
            store = self.instance / ".silicon" / "snapshots"
            store.mkdir(parents=True, exist_ok=True)
            manifest = store / (
                f"{transaction_id}-{len(checkpoint_memory) + 1}.json"
            )
            manifest.write_text('{"verified":true}\\n')
            checkpoint_memory[str(manifest.resolve())] = value
            return {
                "root_hash": "c" * 64,
                "manifest_path": str(manifest),
                "store": str(store),
            }

        def verify(checkpoint_value):
            self.events.append("verify-checkpoint")
            if not Path(checkpoint_value["manifest_path"]).is_file():
                raise RuntimeError("missing checkpoint")

        def restore(_checkpoint_value):
            self.events.append("restore-checkpoint")
            self._write(
                self.instance,
                "prompts/MEMORY.md",
                checkpoint_memory[str(_checkpoint_value["manifest_path"])],
            )

        def health(_previous):
            nonlocal health_checks
            health_checks += 1
            # A fixture-level failed candidate still models a healthy prior
            # generation after automatic recovery.
            return healthy or health_checks > 1

        return EngineHooks(
            service_state=lambda: dict(self.services),
            request_drain=lambda _tx, _deadline: self.events.append("drain"),
            await_quiescent=lambda _tx, _deadline, _cancel, _running: self.events.append(
                "quiescent"
            ),
            quiesce_delivery=lambda: self.events.append(
                "quiesce-delivery"
            ),
            stop_services=stop,
            start_services=start,
            health_check=health,
            reattach_maintenance=lambda _tx, _version, phase: self.events.append(
                f"reattach:{phase}"
            ),
            set_phase=lambda _tx, phase, _detail: self.events.append(phase),
            finish=lambda _tx, outcome: self.events.append(outcome),
            create_checkpoint=checkpoint,
            verify_checkpoint=verify,
            restore_checkpoint=restore,
            dependency_runner=lambda _command: 0,
        )


class ReleaseTests(unittest.TestCase):
    def test_parallel_release_cache_operations_wait_for_the_shared_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            store_entered = threading.Event()
            allow_store = threading.Event()
            original_store = fixture.cache._store_locked

            def slow_store(release):
                if not store_entered.is_set():
                    store_entered.set()
                    self.assertTrue(allow_store.wait(2))
                return original_store(release)

            with (
                mock.patch.object(
                    fixture.cache,
                    "_store_locked",
                    side_effect=slow_store,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first_store = executor.submit(
                    fixture.cache.store, fixture.release
                )
                self.assertTrue(store_entered.wait(2))
                second_store = executor.submit(
                    fixture.cache.store, fixture.release
                )
                time.sleep(0.05)
                self.assertFalse(second_store.done())
                allow_store.set()
                cached = first_store.result(timeout=2)
                self.assertEqual(
                    second_store.result(timeout=2).manifest,
                    cached.manifest,
                )

            extract_entered = threading.Event()
            allow_extract = threading.Event()
            original_extract = cache_module.safe_extract

            def slow_extract(*args, **kwargs):
                if not extract_entered.is_set():
                    extract_entered.set()
                    self.assertTrue(allow_extract.wait(2))
                return original_extract(*args, **kwargs)

            with (
                mock.patch.object(
                    cache_module,
                    "safe_extract",
                    side_effect=slow_extract,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first_extract = executor.submit(
                    fixture.cache.materialize,
                    cached,
                    fixture.root / "extract-one",
                )
                self.assertTrue(extract_entered.wait(2))
                second_extract = executor.submit(
                    fixture.cache.materialize,
                    cached,
                    fixture.root / "extract-two",
                )
                time.sleep(0.05)
                self.assertFalse(second_extract.done())
                allow_extract.set()
                first_extract.result(timeout=2)
                second_extract.result(timeout=2)

    def test_artifact_has_exact_identity_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            fixture.release.artifact.write_bytes(
                fixture.release.artifact.read_bytes() + b"tampered"
            )
            with self.assertRaises(ReleaseVerificationError):
                fixture.cache.store(fixture.release)

    def test_release_cache_rejects_linked_artifact(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = Fixture(root)
            cached = fixture.cache.store(fixture.release)
            cached.artifact.unlink()
            try:
                cached.artifact.symlink_to(fixture.release.artifact)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(
                ReleaseVerificationError, "cache file is unsafe"
            ):
                fixture.cache.load(
                    fixture.release.manifest.identity.tree_sha256
                )

    def test_dependency_cache_rejects_linked_environment_and_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()
            lockfile = candidate / "requirements.lock"
            lockfile.write_text("", encoding="utf-8")
            cache = ReleaseCache(root / "cache")
            requirement_hash = hashlib.sha256(
                lockfile.read_bytes()
            ).hexdigest()
            environment = cache.environments / (
                requirement_hash
                + "-"
                + runtime_platform_identity()["key"]
            )
            outside = root / "outside"
            outside.mkdir()
            cache.environments.mkdir(parents=True)
            try:
                environment.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                cache.prepare_environment(
                    candidate, runner=lambda _command: 0
                )

            environment.unlink()
            (environment / "bin").mkdir(parents=True)
            (environment / "bin" / "python").write_text(
                "", encoding="utf-8"
            )
            marker_target = root / "marker.json"
            marker_target.write_text("{}", encoding="utf-8")
            (environment / ".silicon-environment.json").symlink_to(
                marker_target
            )
            with self.assertRaisesRegex(RuntimeError, "linked readiness"):
                cache.prepare_environment(
                    candidate, runner=lambda _command: 0
                )

    def test_safe_extract_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bad.tar"
            with tarfile.open(archive, "w") as handle:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                handle.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(Exception):
                safe_extract(archive, root / "out")
            self.assertFalse((root / "escape").exists())

    def test_safe_extract_rejects_symlink_destination_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = Fixture(root)
            outside = root / "outside"
            outside.mkdir()
            destination = root / "linked-output"
            try:
                destination.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(
                ReleaseVerificationError, "unsafe extraction destination"
            ):
                safe_extract(
                    fixture.release.artifact,
                    destination,
                    fixture.release.manifest,
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_overlay_application_fsyncs_customization_before_publication(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            instance = root / "instance"
            base = root / "base"
            local = root / "local"
            destination = root / "destination"
            for directory in (instance, base, local):
                directory.mkdir()
            Fixture._write(base, "main.py", "print('base')\n")
            shutil.copytree(base, local, dirs_exist_ok=True)
            Fixture._write(local, "extensions/self.py", "VALUE = 1\n")
            os.chmod(local / "extensions" / "self.py", 0o755)
            store = OverlayStore(instance)
            overlay = store.capture(
                base,
                local,
                base_tree_sha256=hash_tree(base)[0],
            )
            shutil.copytree(base, destination)

            with (
                mock.patch.object(
                    overlay_module.os,
                    "fsync",
                    wraps=overlay_module.os.fsync,
                ) as file_fsync,
                mock.patch.object(
                    overlay_module,
                    "fsync_dir",
                    wraps=overlay_module.fsync_dir,
                ) as directory_fsync,
            ):
                store.apply(overlay["root_hash"], destination)

            target = destination / "extensions" / "self.py"
            self.assertEqual(target.read_text(), "VALUE = 1\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertGreaterEqual(file_fsync.call_count, 1)
            self.assertTrue(
                any(
                    call.args
                    and Path(call.args[0]).resolve()
                    == target.parent.resolve()
                    for call in directory_fsync.call_args_list
                )
            )

    def test_candidate_scripts_are_data_and_are_never_executed(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            Fixture._write(
                fixture.new,
                "scripts/silicon_update.py",
                "raise RuntimeError('candidate executed')\n",
            )
            release = create_artifact(
                fixture.new,
                fixture.root / "malicious.tar",
                revision="b" * 40,
                source_label="test",
                trust="test",
            )
            fixture.services = {"main": False, "glass_agent": False}
            with mock.patch("subprocess.run") as run:
                result = TransactionalUpdater(
                    fixture.instance, fixture.cache, hooks=fixture.hooks()
                ).run(release)
            self.assertEqual(result["state"], "COMMITTED")
            run.assert_not_called()

    def test_dependency_environment_requires_hash_pinned_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "requirements.txt").write_text("certifi\n")
            cache = ReleaseCache(root / "cache")
            with self.assertRaisesRegex(RuntimeError, "requirements.lock"):
                cache.prepare_environment(
                    candidate,
                    runner=lambda _command: 0,
                    environments_root=root / "environments",
                )

    def test_dependency_install_uses_lock_hashes_and_reuses_exact_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "requirements.txt").write_text("certifi\n")
            (candidate / "requirements.lock").write_text(
                "certifi==2026.7.22 --hash=sha256:" + "a" * 64 + "\n"
            )
            commands = []

            def runner(command):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    environment = Path(command[-1])
                    python = environment / (
                        "Scripts/python.exe"
                        if os.name == "nt"
                        else "bin/python"
                    )
                    python.parent.mkdir(parents=True)
                    python.write_text("")
                return 0

            cache = ReleaseCache(root / "cache")
            environment = cache.prepare_environment(
                candidate,
                runner=runner,
                environments_root=root / "environments",
            )
            pip_command = commands[-1]
            self.assertIn("--require-hashes", pip_command)
            self.assertEqual(pip_command[-1], str(candidate / "requirements.lock"))
            self.assertEqual(
                cache.prepare_environment(
                    candidate,
                    runner=lambda _command: self.fail(
                        "ready environment should be reused"
                    ),
                    environments_root=root / "environments",
                ),
                environment,
            )
            marker = json.loads(
                (
                    environment / ".silicon-environment.json"
                ).read_text()
            )
            runtime = marker["runtime"]
            self.assertTrue(runtime["implementation"])
            self.assertTrue(runtime["cache_tag"])
            self.assertTrue(runtime["soabi"])
            self.assertTrue(runtime["machine"])
            self.assertTrue(runtime["platform"])
            self.assertIn(runtime["key"], environment.name)

    def test_environment_with_different_abi_identity_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "requirements.lock").write_text(
                "certifi==2026.7.22 --hash=sha256:" + "a" * 64 + "\n"
            )
            commands = []

            def runner(command):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    python = Path(command[-1]) / (
                        "Scripts/python.exe"
                        if os.name == "nt"
                        else "bin/python"
                    )
                    python.parent.mkdir(parents=True)
                    python.write_text("")
                return 0

            cache = ReleaseCache(root / "cache")
            environment = cache.prepare_environment(
                candidate,
                runner=runner,
            )
            marker_path = environment / ".silicon-environment.json"
            marker = json.loads(marker_path.read_text())
            marker["runtime"]["machine"] = "different-machine"
            marker_path.write_text(json.dumps(marker))
            first_count = len(commands)
            rebuilt = cache.prepare_environment(
                candidate,
                runner=runner,
            )
            self.assertEqual(rebuilt, environment)
            self.assertGreater(len(commands), first_count)


class PlannerAndRecoveryTests(unittest.TestCase):
    def test_plan_preserves_local_edit_and_applies_unrelated_upstream_edit(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            Fixture._write(fixture.instance, "core/local.py", "LOCAL = True\n")
            plan = build_plan(fixture.old, fixture.instance, fixture.new)
            self.assertFalse(plan.conflicts)
            actions = {action.path: action.action for action in plan.actions}
            self.assertEqual(actions["core/local.py"], "preserve-local")
            self.assertEqual(actions["main.py"], "update-upstream")
            self.assertNotIn("prompts/MEMORY.md", actions)

    def test_plan_ignores_generated_node_modules_and_their_symlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            project = fixture.instance / "work" / "deck"
            project.mkdir(parents=True)
            Fixture._write(
                fixture.instance,
                "work/deck/package.json",
                '{"scripts":{"build":"vite"}}\n',
            )
            outside = Path(raw) / "generated-node-modules"
            outside.mkdir()
            try:
                (project / "node_modules").symlink_to(
                    outside,
                    target_is_directory=True,
                )
                (project / ".venv").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError:
                self.skipTest("symlinks are unavailable")
            cache = project / "__pycache__"
            cache.mkdir()
            (cache / "generated.cpython-314.pyc").write_bytes(b"generated")

            plan = build_plan(fixture.old, fixture.instance, fixture.new)

            self.assertFalse(plan.conflicts)
            actions = {action.path: action.action for action in plan.actions}
            self.assertEqual(
                actions["work/deck/package.json"],
                "preserve-local",
            )
            self.assertFalse(
                any(
                    generated in path
                    for path in actions
                    for generated in ("node_modules", ".venv", "__pycache__")
                ),
            )

    def test_identical_independent_addition_preserves_local_executable_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base = root / "base"
            local = root / "local"
            upstream = root / "upstream"
            candidate = root / "candidate"
            for directory in (base, local, upstream, candidate):
                directory.mkdir()
            for directory in (local, upstream, candidate):
                script = directory / "scripts" / "custom-tool"
                script.parent.mkdir()
                script.write_bytes(b"#!/bin/sh\nexit 0\n")
            os.chmod(local / "scripts" / "custom-tool", 0o755)
            os.chmod(upstream / "scripts" / "custom-tool", 0o644)
            os.chmod(candidate / "scripts" / "custom-tool", 0o644)

            plan = build_plan(base, local, upstream)

            self.assertFalse(plan.conflicts)
            action = next(
                action
                for action in plan.actions
                if action.path == "scripts/custom-tool"
            )
            self.assertEqual(action.action, "already-merged")
            apply_plan(plan, candidate, local)
            self.assertEqual(
                (candidate / "scripts" / "custom-tool").stat().st_mode & 0o777,
                0o755,
            )

    def test_overlapping_customization_conflicts_before_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            Fixture._write(fixture.instance, "main.py", "print('local')\n")
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            with self.assertRaises(UpdateConflict):
                updater.run(fixture.release)
            self.assertNotIn("stop", fixture.events)
            latest = updater.history()[0]
            self.assertEqual(latest["state"], "FAILED")

    def test_engine_requires_and_verifies_canonical_checkpoint_hook(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            self.assertLess(
                fixture.events.index("quiesce-delivery"),
                fixture.events.index("checkpoint"),
            )
            self.assertLess(
                fixture.events.index("checkpoint"), fixture.events.index("stop")
            )
            self.assertLess(
                fixture.events.index("verify-checkpoint"),
                fixture.events.index("stop"),
            )
            checkpoint = updater.history()[0]["metadata"]["recovery_checkpoint"]
            self.assertIn(".silicon/snapshots", checkpoint["manifest_path"])


class TransactionTests(unittest.TestCase):
    def test_activation_fsyncs_managed_marker_before_generation_pointer(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            writes = []
            original = generation_module.atomic_write_json

            def record(path, value, *args, **kwargs):
                writes.append(Path(path).name)
                return original(path, value, *args, **kwargs)

            with mock.patch.object(
                generation_module,
                "atomic_write_json",
                side_effect=record,
            ):
                updater.run(fixture.release)

            self.assertLess(
                writes.index("generation-managed-v1.json"),
                writes.index("current.json"),
            )

    def test_full_preflight_prepares_dependencies_without_a_maintenance_fence(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()
            environment = (
                fixture.instance / ".silicon" / "environments" / "prepared"
            )

            def prepare(_release, _runtime_image):
                fixture.events.append("prepare-environment")
                environment.mkdir(parents=True)
                return environment

            hooks.prepare_environment = prepare
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            result = updater.preflight(fixture.release)

            self.assertTrue(result["safe_to_apply"])
            self.assertEqual(
                result["generation"]["environment_path"],
                ".silicon/environments/prepared",
            )
            self.assertIn("prepare-environment", fixture.events)
            self.assertNotIn("drain", fixture.events)
            self.assertNotIn("stop", fixture.events)

    def test_update_orders_drain_checkpoint_stop_activate_start_validate(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            result = updater.run(fixture.release)
            self.assertEqual(result["state"], "COMMITTED")
            self.assertLess(
                fixture.events.index("quiescent"),
                fixture.events.index("quiesce-delivery"),
            )
            self.assertLess(
                fixture.events.index("quiesce-delivery"),
                fixture.events.index("checkpoint"),
            )
            self.assertLess(
                fixture.events.index("checkpoint"),
                fixture.events.index("stop"),
            )
            self.assertLess(fixture.events.index("stop"), fixture.events.index("start"))
            active = active_release_root(fixture.instance)
            self.assertEqual((active / "main.py").read_text(), "print('new')\n")
            pointer = json.loads(
                (
                    fixture.instance / ".silicon" / "current.json"
                ).read_text()
            )
            self.assertFalse(Path(pointer["release_path"]).is_absolute())
            self.assertEqual(
                (fixture.instance / "prompts/MEMORY.md").read_text(),
                "important memory\n",
            )

    def test_fleet_can_defer_retention_until_after_activation_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            removed = {"generations": ["old-generation"]}
            with mock.patch.object(
                updater.retention,
                "prune",
                return_value=removed,
            ) as prune:
                result = updater.run(
                    fixture.release,
                    defer_retention=True,
                )
                prune.assert_not_called()
                self.assertTrue(result["metadata"]["retention_deferred"])

                finalized = updater.finalize_retention(
                    str(result["transaction_id"])
                )

            prune.assert_called_once_with()
            self.assertFalse(
                finalized["metadata"]["retention_deferred"]
            )
            self.assertEqual(finalized["metadata"]["retention"], removed)

    def test_checkpoint_failure_restores_precheckpoint_delivery_state(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()

            def fail_checkpoint(_transaction_id, _release_id):
                fixture.events.append("checkpoint-failed")
                raise RuntimeError("snapshot unavailable")

            hooks.create_checkpoint = fail_checkpoint
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=hooks,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "snapshot unavailable",
            ):
                updater.run(fixture.release)

            self.assertLess(
                fixture.events.index("quiesce-delivery"),
                fixture.events.index("checkpoint-failed"),
            )
            self.assertIn("start", fixture.events)
            self.assertNotIn("stop", fixture.events)
            self.assertEqual(
                fixture.services,
                {"main": True, "glass_agent": True},
            )

    def test_legacy_seal_excludes_seed_data_from_recovery_source(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            base = fixture.instance / ".silicon-upstream" / "base"
            excluded = {
                "prompts/CONTACTS.md": "seed contacts\n",
                "prompts/LORE.md": "seed lore\n",
                "prompts/MEMORY.md": "seed memory\n",
                "prompts/memory/carbons/.gitkeep": "",
                "prompts/memory/projects/.gitkeep": "",
                "prompts/memory/silicons/.gitkeep": "",
                "silicon.json": '{"address":"seed"}\n',
            }
            for relative, content in excluded.items():
                Fixture._write(base, relative, content)

            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)

            sealed = updater.history()[0]["metadata"][
                "sealed_prior_generation"
            ]
            sealed_root = fixture.instance / sealed["release_path"]
            for relative in excluded:
                self.assertFalse((sealed_root / relative).exists())
            reconstructed = fixture.root / "reconstructed-legacy"
            updater._materialize_authenticated_generation(
                sealed, reconstructed
            )
            for relative in excluded:
                self.assertFalse((reconstructed / relative).exists())
            self.assertEqual(
                (fixture.instance / "prompts/MEMORY.md").read_text(),
                "important memory\n",
            )
            self.assertEqual(
                (fixture.instance / "silicon.json").read_text(),
                '{"address":"ada"}\n',
            )

    def test_failed_health_check_rolls_back_and_restores_services(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks(healthy=False)
            )
            with self.assertRaises(Exception):
                updater.run(fixture.release)
            self.assertEqual(
                active_release_root(fixture.instance), fixture.instance.resolve()
            )
            self.assertEqual(fixture.services, {"main": True, "glass_agent": True})
            self.assertEqual(updater.history()[0]["state"], "ROLLED_BACK")
            self.assertIn("restore-checkpoint", fixture.events)
            self.assertEqual(
                updater.history()[0]["metadata"]["checkpoint_recovery"]["state"],
                "restored",
            )

    def test_candidate_mutation_is_restored_before_prior_services_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks(healthy=False)
            original_start = hooks.start_services
            starts = 0

            def mutate_on_candidate_start(previous):
                nonlocal starts
                starts += 1
                original_start(previous)
                if starts == 1:
                    Fixture._write(
                        fixture.instance,
                        "prompts/MEMORY.md",
                        "candidate-corruption\n",
                    )

            hooks.start_services = mutate_on_candidate_start
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaises(UpdateError):
                updater.run(fixture.release)

            self.assertEqual(
                (fixture.instance / "prompts/MEMORY.md").read_text(),
                "important memory\n",
            )
            start_indices = [
                index
                for index, event in enumerate(fixture.events)
                if event == "start"
            ]
            self.assertLess(
                fixture.events.index("restore-checkpoint"), start_indices[-1]
            )

    def test_candidate_era_unknown_protected_files_remain_quarantined(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks(healthy=False)
            original_start = hooks.start_services
            starts = 0
            candidate_file = (
                fixture.instance
                / "prompts"
                / "memory"
                / "candidate-migration.md"
            )

            def start_and_migrate(previous):
                nonlocal starts
                starts += 1
                original_start(previous)
                if starts == 1:
                    Fixture._write(
                        fixture.instance,
                        "prompts/memory/candidate-migration.md",
                        "candidate-era state\n",
                    )

            def checkpoint(_transaction_id, release_id):
                return create_local_snapshot(
                    fixture.instance, release_id=release_id
                )

            def verify(value):
                verify_local_snapshot(
                    Path(value["manifest_path"]),
                    store=Path(value["store"]),
                )

            def restore(value):
                restore_local_snapshot_in_place(
                    fixture.instance,
                    Path(value["manifest_path"]),
                    store=Path(value["store"]),
                )

            hooks.start_services = start_and_migrate
            hooks.create_checkpoint = checkpoint
            hooks.verify_checkpoint = verify
            hooks.restore_checkpoint = restore
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaises(UpdateError):
                updater.run(fixture.release)

            self.assertFalse(candidate_file.exists())
            preservation = updater.history()[0]["metadata"][
                "candidate_era_preservation"
            ]
            self.assertEqual(preservation["state"], "verified")
            emergency = preservation["checkpoint"]
            manifest = verify_local_snapshot(
                fixture.instance / emergency["manifest_path"],
                store=fixture.instance / emergency["store"],
            )
            self.assertIn(
                "prompts/memory/candidate-migration.md",
                {entry["path"] for entry in manifest["files"]},
            )

    def test_unverified_candidate_quarantine_stops_before_destructive_restore(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks(healthy=False)
            normal_checkpoint = hooks.create_checkpoint
            calls = 0

            def fail_candidate_snapshot(transaction_id, release_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("snapshot store unavailable")
                return normal_checkpoint(transaction_id, release_id)

            hooks.create_checkpoint = fail_candidate_snapshot
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaisesRegex(UpdateError, "needs resume"):
                updater.run(fixture.release)

            incomplete = updater.history()[0]
            self.assertEqual(
                incomplete["metadata"]["candidate_era_preservation"]["state"],
                "failed",
            )
            self.assertNotIn("restore-checkpoint", fixture.events)
            self.assertEqual(
                fixture.services, {"main": False, "glass_agent": False}
            )

            resumed = updater.resume()
            self.assertEqual(resumed["state"], "ROLLED_BACK")
            self.assertEqual(
                resumed["metadata"]["candidate_era_preservation"]["state"],
                "verified",
            )

    def test_failed_checkpoint_restore_remains_resumable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks(healthy=False)
            normal_restore = hooks.restore_checkpoint
            restore_attempts = 0

            def fail_once(checkpoint):
                nonlocal restore_attempts
                restore_attempts += 1
                if restore_attempts == 1:
                    raise RuntimeError("ambiguous restore interruption")
                normal_restore(checkpoint)

            hooks.restore_checkpoint = fail_once
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaisesRegex(UpdateError, "needs resume"):
                updater.run(fixture.release)
            incomplete = updater.history()[0]
            self.assertEqual(incomplete["state"], "STARTED")
            self.assertEqual(
                incomplete["metadata"]["checkpoint_recovery"]["state"],
                "restoring",
            )

            resumed = updater.resume()
            self.assertEqual(resumed["state"], "ROLLED_BACK")
            self.assertEqual(
                resumed["metadata"]["checkpoint_recovery"],
                {
                    "state": "restored",
                    "attempts": 2,
                    "root_hash": "c" * 64,
                },
            )

    def test_failed_update_never_rewinds_live_maintenance_queue_state(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            state_path = (
                fixture.instance
                / "core"
                / "interface_state"
                / "maintenance.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text('{"epoch":1,"queued":["before"]}\n')
            hooks = fixture.hooks(healthy=False)
            original_start = hooks.start_services

            def start_and_advance(previous):
                original_start(previous)
                state_path.write_text(
                    '{"epoch":2,"queued":["before","during-update"]}\n'
                )

            hooks.start_services = start_and_advance
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaises(Exception):
                updater.run(fixture.release)
            self.assertEqual(
                json.loads(state_path.read_text()),
                {"epoch": 2, "queued": ["before", "during-update"]},
            )

    def test_partial_stop_failure_restores_prior_services_and_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()
            normal_stop = hooks.stop_services
            attempts = 0

            def fail_first_stop():
                nonlocal attempts
                attempts += 1
                normal_stop()
                if attempts == 1:
                    raise RuntimeError("injected partial stop failure")

            hooks.stop_services = fail_first_stop
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaises(RuntimeError):
                updater.run(fixture.release)
            self.assertEqual(
                active_release_root(fixture.instance), fixture.instance.resolve()
            )
            self.assertEqual(
                fixture.services, {"main": True, "glass_agent": True}
            )
            self.assertEqual(updater.history()[0]["state"], "ROLLED_BACK")

    def test_resume_after_crash_at_stop_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            with self.assertRaises(FailpointCrash):
                updater.run(fixture.release, failpoint="STOPPED")
            self.assertFalse(fixture.services["main"])
            result = updater.resume()
            self.assertEqual(result["state"], "COMMITTED")
            self.assertTrue(fixture.services["main"])
            self.assertIn("reattach:updating", fixture.events)
            self.assertNotEqual(
                active_release_root(fixture.instance), fixture.instance
            )

    def test_resume_repairs_marker_only_activation_crash(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            with self.assertRaises(FailpointCrash):
                updater.run(fixture.release, failpoint="STOPPED")
            marker = (
                fixture.instance
                / ".silicon"
                / "generation-managed-v1.json"
            )
            marker.write_text(
                json.dumps({"schema": 1, "managed_at": 1}),
                encoding="utf-8",
            )
            self.assertFalse(
                (fixture.instance / ".silicon" / "current.json").exists()
            )

            result = updater.resume()
            self.assertEqual(result["state"], "COMMITTED")
            self.assertNotEqual(
                active_release_root(fixture.instance),
                fixture.instance.resolve(),
            )

    def test_resume_after_crash_after_activation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            with self.assertRaises(FailpointCrash):
                updater.run(fixture.release, failpoint="ACTIVATED")
            result = updater.resume()
            self.assertEqual(result["state"], "COMMITTED")

    def test_every_durable_failpoint_resumes_to_one_complete_generation(self):
        phases = [
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
        ]
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                fixture = Fixture(Path(raw))
                updater = TransactionalUpdater(
                    fixture.instance, fixture.cache, hooks=fixture.hooks()
                )
                with self.assertRaises(FailpointCrash):
                    updater.run(fixture.release, failpoint=phase)
                result = updater.resume()
                self.assertEqual(result["state"], "COMMITTED")

    def test_fresh_update_refuses_to_overlap_any_nonterminal_transaction(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            existing = TransactionJournal.create(
                fixture.instance, {"operation": "update"}
            )
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            with self.assertRaises(UpdateError):
                updater.run(fixture.release)
            self.assertEqual(
                [
                    item
                    for item in updater.history()
                    if item["state"] not in {
                        "COMMITTED",
                        "FAILED",
                        "CANCELLED",
                        "ROLLED_BACK",
                    }
                ][0]["transaction_id"],
                existing.transaction_id,
            )

    def test_keyboard_interrupt_safely_cancels_before_stop_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()
            hooks.await_quiescent = (
                lambda _tx, _deadline, _cancel, _running: (
                    _ for _ in ()
                ).throw(KeyboardInterrupt())
            )
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaises(Exception):
                updater.run(fixture.release)
            self.assertEqual(updater.history()[0]["state"], "CANCELLED")
            self.assertEqual(
                fixture.services, {"main": True, "glass_agent": True}
            )

    def test_late_self_edit_after_drain_is_never_silently_lost(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()

            def quiesce(_tx, _deadline, _cancel, _running):
                fixture.events.append("quiescent")
                Fixture._write(
                    fixture.instance,
                    "core/tool.py",
                    "VALUE = 'authored during drain'\n",
                )

            hooks.await_quiescent = quiesce
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaisesRegex(
                UpdateError, "authored new source changes"
            ):
                updater.run(fixture.release)
            self.assertNotIn("stop", fixture.events)
            self.assertEqual(
                (fixture.instance / "core/tool.py").read_text(),
                "VALUE = 'authored during drain'\n",
            )
            self.assertEqual(updater.history()[0]["state"], "FAILED")

    def test_missing_data_root_capability_aborts_before_drain(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            (fixture.new / ".silicon-data-root-v1").unlink()
            release = create_artifact(
                fixture.new,
                fixture.root / "no-capability.tar",
                revision="e" * 40,
                source_label="test",
                trust="test",
            )
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            self.assertFalse(updater.plan(release)["safe_to_apply"])
            with self.assertRaises(UpdateError):
                updater.run(release)
            self.assertNotIn("drain", fixture.events)

    def test_stopped_legacy_instance_updates_through_offline_fence(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            fixture.services = {"main": False, "glass_agent": False}

            class NoGlass:
                update_id = ""

                def begin(self, *_args):
                    fixture.events.append("glass-preparing")

                def set_queued_count(self, *_args):
                    pass

                def report(self, phase, **_kwargs):
                    fixture.events.append(f"glass-{phase}")

                def finish(self, outcome):
                    fixture.events.append(f"glass-{outcome}")

                def reattach(self, *_args):
                    pass

            protocol = MaintenanceProtocol(
                fixture.instance,
                glass=NoGlass(),
                legacy_offline_safe=lambda: not any(
                    fixture.services.values()
                ),
            )
            hooks = fixture.hooks()
            hooks.begin_maintenance = protocol.begin
            hooks.reattach_maintenance = protocol.reattach
            hooks.request_drain = protocol.request_drain
            hooks.await_quiescent = (
                lambda tx, deadline, cancelled, running: (
                    protocol.await_quiescent(
                        tx,
                        deadline,
                        cancelled,
                        services_running=running,
                    )
                )
            )
            hooks.cancel_drain = protocol.cancel
            hooks.set_phase = protocol.set_phase
            hooks.finish = protocol.finish
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )

            result = updater.run(fixture.release)
            self.assertEqual(result["state"], "COMMITTED")
            self.assertNotEqual(
                active_release_root(fixture.instance),
                fixture.instance.resolve(),
            )
            self.assertIn("checkpoint", fixture.events)
            self.assertIn("glass-draining", fixture.events)
            self.assertFalse(
                (
                    fixture.instance
                    / ".silicon"
                    / "maintenance"
                    / "legacy-offline.json"
                ).exists()
            )

    def test_rollback_reactivates_prior_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            fixture.events.clear()
            result = updater.rollback()
            self.assertEqual(result["state"], "COMMITTED")
            self.assertLess(
                fixture.events.index("quiesce-delivery"),
                fixture.events.index("checkpoint"),
            )
            self.assertLess(
                fixture.events.index("checkpoint"),
                fixture.events.index("stop"),
            )
            self.assertEqual(
                active_release_root(fixture.instance), fixture.instance.resolve()
            )

    def test_rollback_carries_post_activation_self_customizations(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            first = active_release_root(fixture.instance)
            Fixture._write(first, "extensions/self.py", "VALUE = 'first'\n")

            newer = fixture.root / "newer"
            shutil.copytree(fixture.new, newer)
            Fixture._write(newer, "core/tool.py", "VALUE = 'newer'\n")
            second_release = create_artifact(
                newer,
                fixture.root / "second.tar",
                revision="b" * 40,
                source_label="test",
                trust="test",
            )
            updater.run(second_release)
            second = active_release_root(fixture.instance)
            Fixture._write(second, "extensions/self.py", "VALUE = 'latest'\n")

            result = updater.rollback()
            rolled_back = active_release_root(fixture.instance)
            self.assertEqual(result["state"], "COMMITTED")
            self.assertEqual(
                (rolled_back / "extensions/self.py").read_text(),
                "VALUE = 'latest'\n",
            )
            self.assertNotEqual(rolled_back, first)
            self.assertTrue(
                result["metadata"]["rollback_customization_delta"][
                    "root_hash"
                ]
            )

    def test_rollback_conflict_preserves_delta_and_never_drains(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            newer = fixture.root / "newer"
            shutil.copytree(fixture.new, newer)
            Fixture._write(newer, "core/tool.py", "VALUE = 'newer'\n")
            second_release = create_artifact(
                newer,
                fixture.root / "second.tar",
                revision="b" * 40,
                source_label="test",
                trust="test",
            )
            updater.run(second_release)
            active = active_release_root(fixture.instance)
            Fixture._write(active, "core/tool.py", "VALUE = 'self edit'\n")
            fixture.events.clear()

            with self.assertRaises(UpdateConflict):
                updater.rollback()
            self.assertNotIn("drain", fixture.events)
            failed = updater.history()[0]
            self.assertEqual(failed["state"], "FAILED")
            delta = failed["metadata"]["rollback_customization_delta"]
            self.assertTrue(
                updater.overlays.verify(delta["root_hash"])["files"]
            )

    def test_corrupt_journal_blocks_status_and_new_updates(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            journal = (
                fixture.instance
                / ".silicon"
                / "transactions"
                / "corrupt.json"
            )
            journal.write_text('{"schema":1,"transaction_id":"other"}')
            with self.assertRaises(JournalCorruption):
                updater.status()
            with self.assertRaises(JournalCorruption):
                updater.run(fixture.release)
            self.assertNotIn("drain", fixture.events)

    def test_dangling_generation_pointer_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            pointer = fixture.instance / ".silicon" / "current.json"
            try:
                pointer.symlink_to(
                    fixture.instance / ".silicon" / "missing-pointer-target"
                )
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(
                GenerationError, "not a regular file"
            ):
                updater.status()

    def test_deleted_managed_pointer_never_downgrades_to_flat_code(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            pointer = fixture.instance / ".silicon" / "current.json"
            marker = (
                fixture.instance
                / ".silicon"
                / "generation-managed-v1.json"
            )
            self.assertTrue(marker.is_file())
            pointer.unlink()
            with self.assertRaisesRegex(
                GenerationError, "refusing to downgrade"
            ):
                updater.status()

    def test_generation_release_symlink_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            active = active_release_root(fixture.instance)
            real = active.with_name(active.name + "-real")
            active.rename(real)
            try:
                active.symlink_to(real, target_is_directory=True)
            except OSError:
                real.rename(active)
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(
                GenerationError, "symbolic link"
            ):
                updater.status()

    def test_journal_event_timestamps_must_be_monotonic_and_bounded(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            journal = TransactionJournal.create(instance)
            value = json.loads(journal.path.read_text(encoding="utf-8"))
            value["events"][0]["at"] = value["updated_at"] + 1
            journal.path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                JournalCorruption, "timestamps are inconsistent"
            ):
                TransactionJournal.load(journal.path)

    def test_explicit_rollback_failure_restores_the_current_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            current = active_release_root(fixture.instance)
            current_tree = hash_tree(current)[0]
            hooks = fixture.hooks()
            normal_stop = hooks.stop_services
            attempts = 0

            def fail_first_stop():
                nonlocal attempts
                attempts += 1
                normal_stop()
                if attempts == 1:
                    raise RuntimeError("rollback stop failed")

            hooks.stop_services = fail_first_stop
            updater.hooks = hooks
            with self.assertRaises(RuntimeError):
                updater.rollback()
            recovered = active_release_root(fixture.instance)
            self.assertEqual(hash_tree(recovered)[0], current_tree)
            self.assertNotEqual(recovered, current)
            self.assertEqual(
                fixture.services, {"main": True, "glass_agent": True}
            )
            self.assertEqual(updater.history()[0]["state"], "ROLLED_BACK")

    def test_explicit_rollback_rebuilds_a_tampered_dormant_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            first = active_release_root(fixture.instance)
            expected = hash_tree(first)[0]

            newer = fixture.root / "newer-authenticated"
            shutil.copytree(fixture.new, newer)
            Fixture._write(newer, "core/tool.py", "VALUE = 'newer'\n")
            second = create_artifact(
                newer,
                fixture.root / "newer-authenticated.tar",
                revision="b" * 40,
                source_label="test",
                trust="test",
            )
            updater.run(second)
            Fixture._write(first, "main.py", "print('tampered dormant')\n")

            result = updater.rollback()
            recovered = active_release_root(fixture.instance)
            self.assertEqual(result["state"], "COMMITTED")
            self.assertEqual(hash_tree(recovered)[0], expected)
            self.assertNotIn("tampered dormant", (recovered / "main.py").read_text())

    def test_automatic_recovery_rebuilds_tampered_prior_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            prior = active_release_root(fixture.instance)
            expected = hash_tree(prior)[0]

            newer = fixture.root / "failed-candidate"
            shutil.copytree(fixture.new, newer)
            Fixture._write(newer, "core/tool.py", "VALUE = 'candidate'\n")
            second = create_artifact(
                newer,
                fixture.root / "failed-candidate.tar",
                revision="c" * 40,
                source_label="test",
                trust="test",
            )
            hooks = fixture.hooks(healthy=False)
            normal_start = hooks.start_services
            starts = 0

            def tamper_prior_when_candidate_starts(previous):
                nonlocal starts
                starts += 1
                normal_start(previous)
                if starts == 1:
                    Fixture._write(
                        prior, "main.py", "print('tampered prior')\n"
                    )

            hooks.start_services = tamper_prior_when_candidate_starts
            updater.hooks = hooks
            with self.assertRaises(UpdateError):
                updater.run(second)

            recovered = active_release_root(fixture.instance)
            self.assertEqual(updater.history()[0]["state"], "ROLLED_BACK")
            self.assertEqual(hash_tree(recovered)[0], expected)
            self.assertNotIn("tampered prior", (recovered / "main.py").read_text())

    def test_signed_sequence_floor_survives_explicit_runtime_rollback(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))

            def sequenced(source: Path, sequence: int, name: str):
                (source / "silicon-release.json").write_text(
                    json.dumps(
                        {
                            "identity": {
                                "version": name,
                                "sequence": sequence,
                                "tree_sha256": "",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                release = create_artifact(
                    source,
                    fixture.root / f"{name}.tar",
                    revision=(str(sequence) * 40)[:40],
                    source_label="glass",
                    trust="signed-ed25519",
                )
                return replace(
                    release,
                    manifest=replace(
                        release.manifest,
                        runtime_image=(
                            "registry.example/silicon@sha256:" + "1" * 64
                        ),
                    ),
                )

            high_source = fixture.root / "sequence-high"
            shutil.copytree(fixture.new, high_source)
            high = sequenced(high_source, 20, "v20")
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(high)
            updater.rollback()

            old_source = fixture.root / "sequence-old"
            shutil.copytree(fixture.new, old_source)
            Fixture._write(old_source, "core/tool.py", "VALUE = 'old'\n")
            old = sequenced(old_source, 19, "v19")
            with self.assertRaisesRegex(
                UpdateError, "highest published release previously accepted"
            ):
                updater.run(old)
            floor = json.loads(
                (
                    fixture.instance
                    / ".silicon"
                    / "release-sequence-floor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["sequence"], 20)

    def test_published_git_floor_rejects_downgrade_and_rewritten_tag(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))

            def published(
                source: Path,
                *,
                version: str,
                sequence: int,
                revision: str,
                tag_object: str,
                artifact_name: str,
            ):
                return create_artifact(
                    source,
                    fixture.root / artifact_name,
                    revision=revision,
                    source_label=(
                        "git+https://github.com/teamofsilicons/"
                        "silicon-stemcell.git@refs/tags/"
                        f"v{version}#{tag_object}"
                    ),
                    trust=PUBLISHED_GIT_TRUST,
                    version=version,
                    sequence=sequence,
                    runtime_image=(
                        "registry.example/silicon@sha256:" + "1" * 64
                    ),
                )

            high_source = fixture.root / "git-high"
            shutil.copytree(fixture.new, high_source)
            high = published(
                high_source,
                version="2.0.0",
                sequence=2_000_001,
                revision="a" * 40,
                tag_object="b" * 40,
                artifact_name="git-high.tar",
            )
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=fixture.hooks(),
            )
            updater.run(high)
            updater.rollback()

            old_source = fixture.root / "git-old"
            shutil.copytree(fixture.new, old_source)
            Fixture._write(old_source, "core/tool.py", "VALUE = 'older'\n")
            old = published(
                old_source,
                version="1.9.0",
                sequence=1_009_001,
                revision="c" * 40,
                tag_object="d" * 40,
                artifact_name="git-old.tar",
            )
            with self.assertRaisesRegex(
                UpdateError,
                "highest published release previously accepted",
            ):
                updater.run(old)

            rewritten_source = fixture.root / "git-rewritten"
            shutil.copytree(fixture.new, rewritten_source)
            Fixture._write(
                rewritten_source,
                "core/tool.py",
                "VALUE = 'rewritten'\n",
            )
            rewritten = published(
                rewritten_source,
                version="2.0.0",
                sequence=2_000_001,
                revision="e" * 40,
                tag_object="f" * 40,
                artifact_name="git-rewritten.tar",
            )
            with self.assertRaisesRegex(
                UpdateError,
                "reused for different immutable content",
            ):
                updater.run(rewritten)

    def test_legacy_floor_migrates_by_version_and_never_allows_git_downgrade(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            signed_source = fixture.root / "signed-v2"
            shutil.copytree(fixture.new, signed_source)
            (signed_source / "silicon-release.json").write_text(
                json.dumps(
                    {
                        "identity": {
                            "version": "2.0.0",
                            "sequence": 1,
                            "tree_sha256": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            signed = create_artifact(
                signed_source,
                fixture.root / "signed-v2.tar",
                revision="a" * 64,
                source_label="glass",
                trust="signed-ed25519",
                runtime_image=(
                    "registry.example/silicon@sha256:" + "1" * 64
                ),
            )
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=fixture.hooks(),
            )
            updater.run(signed)

            # Model an installation whose durable floor predates schema 2.
            floor_path = (
                fixture.instance
                / ".silicon"
                / "release-sequence-floor.json"
            )
            floor_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "sequence": 1,
                        "tree_sha256": signed.manifest.identity.tree_sha256,
                        "recorded_at": 1.0,
                    }
                ),
                encoding="utf-8",
            )

            git_source = fixture.root / "git-v1"
            shutil.copytree(fixture.new, git_source)
            Fixture._write(git_source, "core/tool.py", "VALUE = 'older'\n")
            git_release = create_artifact(
                git_source,
                fixture.root / "git-v1.tar",
                revision="b" * 40,
                source_label=(
                    "git+https://github.com/teamofsilicons/"
                    "silicon-stemcell.git@refs/tags/v1.9.0#"
                    + "c" * 40
                ),
                trust=PUBLISHED_GIT_TRUST,
                version="1.9.0",
                sequence=1_009_001,
                runtime_image=(
                    "registry.example/silicon@sha256:" + "1" * 64
                ),
            )
            with self.assertRaisesRegex(
                UpdateError,
                "older than the highest published release",
            ):
                updater.run(git_release)

    def test_newer_git_version_replaces_legacy_floor_namespace(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            signed_source = fixture.root / "signed-v1"
            shutil.copytree(fixture.new, signed_source)
            (signed_source / "silicon-release.json").write_text(
                json.dumps(
                    {
                        "identity": {
                            "version": "1.9.0",
                            "sequence": 99,
                            "tree_sha256": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            signed = create_artifact(
                signed_source,
                fixture.root / "signed-v1.tar",
                revision="d" * 64,
                source_label="glass",
                trust="signed-ed25519",
                runtime_image=(
                    "registry.example/silicon@sha256:" + "2" * 64
                ),
            )
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=fixture.hooks(),
            )
            updater.run(signed)
            (
                fixture.instance
                / ".silicon"
                / "release-sequence-floor.json"
            ).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "sequence": 99,
                        "tree_sha256": signed.manifest.identity.tree_sha256,
                        "recorded_at": 1.0,
                    }
                ),
                encoding="utf-8",
            )

            git_source = fixture.root / "git-v2"
            shutil.copytree(fixture.new, git_source)
            Fixture._write(git_source, "core/tool.py", "VALUE = 'newer'\n")
            git_release = create_artifact(
                git_source,
                fixture.root / "git-v2.tar",
                revision="e" * 40,
                source_label=(
                    "git+https://github.com/teamofsilicons/"
                    "silicon-stemcell.git@refs/tags/v2.0.0#"
                    + "f" * 40
                ),
                trust=PUBLISHED_GIT_TRUST,
                version="2.0.0",
                sequence=2_000_001,
                runtime_image=(
                    "registry.example/silicon@sha256:" + "2" * 64
                ),
            )
            updater.run(git_release)

            floor = json.loads(
                (
                    fixture.instance
                    / ".silicon"
                    / "release-sequence-floor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["schema"], 2)
            self.assertEqual(floor["version"], "2.0.0")
            self.assertEqual(floor["trust"], PUBLISHED_GIT_TRUST)

    def test_unsigned_sequence_cannot_poison_signed_release_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))

            unsigned_source = fixture.root / "unsigned-high"
            shutil.copytree(fixture.new, unsigned_source)
            (unsigned_source / "silicon-release.json").write_text(
                json.dumps(
                    {
                        "identity": {
                            "version": "unsigned-999",
                            "sequence": 999,
                            "tree_sha256": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            unsigned = create_artifact(
                unsigned_source,
                fixture.root / "unsigned-high.tar",
                revision="d" * 40,
                source_label="git@example.invalid/repo@commit",
                trust="derived-local-git",
            )
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(unsigned)
            floor_path = (
                fixture.instance
                / ".silicon"
                / "release-sequence-floor.json"
            )
            self.assertFalse(floor_path.exists())

            signed_source = fixture.root / "signed-low"
            shutil.copytree(fixture.new, signed_source)
            Fixture._write(
                signed_source, "core/tool.py", "VALUE = 'signed'\n"
            )
            (signed_source / "silicon-release.json").write_text(
                json.dumps(
                    {
                        "identity": {
                            "version": "v1",
                            "sequence": 1,
                            "tree_sha256": "",
                        }
                    }
                ),
                encoding="utf-8",
            )
            signed = create_artifact(
                signed_source,
                fixture.root / "signed-low.tar",
                revision="e" * 64,
                source_label="glass",
                trust="signed-ed25519",
            )
            signed = replace(
                signed,
                manifest=replace(
                    signed.manifest,
                    runtime_image=(
                        "registry.example/silicon@sha256:" + "2" * 64
                    ),
                ),
            )
            updater.run(signed)
            self.assertEqual(
                json.loads(floor_path.read_text(encoding="utf-8"))["sequence"],
                1,
            )

    def test_pre_stop_cleanup_finishes_carbon_lease_even_if_cancel_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            hooks = fixture.hooks()
            finished: list[str] = []
            hooks.request_drain = lambda _tx, _deadline: (_ for _ in ()).throw(
                RuntimeError("drain request failed")
            )
            hooks.cancel_drain = lambda _tx: (_ for _ in ()).throw(
                RuntimeError("local cancel failed")
            )
            hooks.finish = lambda _tx, outcome: finished.append(outcome)
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=hooks
            )
            with self.assertRaisesRegex(RuntimeError, "drain request failed"):
                updater.run(fixture.release)
            self.assertEqual(finished, ["failed"])
            latest = updater.history()[0]
            self.assertEqual(latest["state"], "FAILED")
            self.assertIn(
                "local_drain_cleanup",
                latest["metadata"]["maintenance_cleanup_warning"],
            )

    def test_interrupted_explicit_rollback_resumes_from_stop_intent(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance, fixture.cache, hooks=fixture.hooks()
            )
            updater.run(fixture.release)
            hooks = fixture.hooks()
            normal_stop = hooks.stop_services
            attempts = 0

            def crash_after_first_stop():
                nonlocal attempts
                attempts += 1
                normal_stop()
                if attempts == 1:
                    raise FailpointCrash("power loss during rollback stop")

            hooks.stop_services = crash_after_first_stop
            updater.hooks = hooks
            with self.assertRaises(FailpointCrash):
                updater.rollback()
            self.assertEqual(updater.history()[0]["state"], "STOPPING")
            result = updater.resume()
            self.assertEqual(result["state"], "COMMITTED")
            self.assertEqual(
                active_release_root(fixture.instance), fixture.instance.resolve()
            )

    def test_instance_lock_rejects_concurrent_owner(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            first = InstanceLock(instance, "one")
            first.acquire()
            try:
                with self.assertRaises(UpdateLocked):
                    InstanceLock(instance, "two").acquire()
            finally:
                first.release()

    def test_cancel_marker_is_durable(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            journal = TransactionJournal.create(instance)
            journal.request_cancel()
            loaded = TransactionJournal.load(journal.path)
            self.assertTrue(loaded.cancellation_requested())


if __name__ == "__main__":
    unittest.main()
