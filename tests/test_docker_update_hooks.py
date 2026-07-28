from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import registry, update
from silicon_cli.updater import EngineHooks, UpdateError
from silicon_cli.updater.maintenance import MaintenanceError


class DockerUpdateHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "ada"
        (self.root / ".silicon").mkdir(parents=True)
        self.install = registry.Install(
            index=0,
            name="ada",
            path=str(self.root),
            pid_file=str(self.root / ".silicon.pid"),
            runtime="docker",
            service="silicon-ada",
            compose_file=str(self.root.parent / "compose.yml"),
            image="example/silicon:latest",
            container_name="silicon-ada",
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def signed_release():
        identity = {
            "version": "2.0.0",
            "revision": "a" * 64,
            "sequence": 2,
            "tree_sha256": "b" * 64,
            "artifact_sha256": "c" * 64,
            "source": "glass",
            "trust": "signed-ed25519",
        }
        return SimpleNamespace(
            manifest=SimpleNamespace(
                identity=SimpleNamespace(to_dict=lambda: dict(identity)),
                runtime_image=(
                    "ghcr.io/teamofsilicons/silicon-runtime@sha256:"
                    + "d" * 64
                ),
            )
        )

    def test_hooks_capture_and_restore_exact_docker_service_state(self):
        with (
            mock.patch.object(
                update.docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                update.docker_runtime, "silicon_running", return_value=False
            ),
            mock.patch.object(
                update.docker_runtime,
                "glass_agent_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "interface_daemon_running",
                return_value=False,
            ),
            mock.patch.object(update.docker_runtime, "restore_one") as restore,
        ):
            hooks = update._hooks(self.install)
            state = hooks.service_state()
            hooks.start_services(state)

        self.assertEqual(
            state,
            {
                "container": True,
                "main": False,
                "glass_agent": True,
                "interface": False,
            },
        )
        restore.assert_called_once_with(
            self.install,
            container=True,
            main=False,
            glass_agent=True,
            interface=False,
            reconcile=False,
            allow_legacy_fence=False,
        )

    def test_hooks_restore_listener_only_docker_service_state(self):
        with (
            mock.patch.object(
                update.docker_runtime,
                "container_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "silicon_running",
                return_value=False,
            ),
            mock.patch.object(
                update.docker_runtime,
                "glass_agent_running",
                return_value=False,
            ),
            mock.patch.object(
                update.docker_runtime,
                "interface_daemon_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "restore_one",
            ) as restore,
        ):
            hooks = update._hooks(self.install)
            state = hooks.service_state()
            hooks.start_services(state)

        self.assertEqual(
            state,
            {
                "container": True,
                "main": False,
                "glass_agent": False,
                "interface": True,
            },
        )
        restore.assert_called_once_with(
            self.install,
            container=True,
            main=False,
            glass_agent=False,
            interface=True,
            reconcile=False,
            allow_legacy_fence=False,
        )

    def test_hooks_quiesce_docker_interface_before_checkpoint(self):
        with (
            mock.patch.object(
                update.docker_runtime,
                "maintenance_coordinator_available",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "container_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "interface_daemon_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "stop_interface_daemon",
            ) as stop_interface,
        ):
            update._hooks(self.install).quiesce_delivery()

        stop_interface.assert_called_once_with(
            self.install,
            required=True,
        )

    def test_stop_hook_fails_closed_if_container_survives(self):
        with (
            mock.patch.object(
                update.docker_runtime,
                "container_running",
                side_effect=[True, True],
            ),
            mock.patch.object(update.docker_runtime, "stop_one") as stop,
        ):
            hooks = update._hooks(self.install)
            with self.assertRaisesRegex(UpdateError, "did not stop cleanly"):
                hooks.stop_services()

        stop.assert_called_once_with(self.install, full=True)

    def test_running_legacy_docker_is_rejected_before_glass_maintenance(self):
        with (
            mock.patch.object(
                update.docker_runtime,
                "maintenance_coordinator_available",
                return_value=False,
            ),
            mock.patch.object(
                update.docker_runtime,
                "container_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "silicon_running",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "glass_agent_running",
                return_value=True,
            ),
            mock.patch.object(
                update.MaintenanceProtocol,
                "begin",
            ) as glass_begin,
        ):
            hooks = update._hooks(self.install)
            with self.assertRaisesRegex(
                MaintenanceError,
                "no task-safe coordinator",
            ):
                hooks.begin_maintenance("tx-legacy", "2.0.0")

        glass_begin.assert_not_called()

    def test_schema_floor_error_falls_back_to_bootstrap_snapshot(self):
        bootstrap = {
            "manifest_path": str(self.root / "manifest.json"),
            "store": str(self.root / ".silicon" / "snapshots"),
            "root_hash": "a" * 64,
            "provider": "silicon-cli-bootstrap",
        }
        failed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Snapshot release sequence floor is invalid.",
        )
        with (
            mock.patch.object(
                update.docker_runtime,
                "run_active_python",
                return_value=failed,
            ),
            mock.patch(
                "silicon_cli.updater.snapshot_adapter.create_local_snapshot",
                return_value=bootstrap,
            ) as create_local,
        ):
            created = update._hooks(self.install).create_checkpoint(
                "tx-1",
                "generation-0",
            )

        self.assertEqual(created, bootstrap)
        create_local.assert_called_once_with(
            self.root.resolve(),
            release_id="generation-0:pre-update:tx-1",
        )

    def test_health_requires_stable_application_readiness(self):
        with (
            mock.patch.object(
                update.docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                update.docker_runtime, "silicon_ready", return_value=True
            ) as ready,
            mock.patch.object(
                update.docker_runtime,
                "glass_agent_running",
                return_value=False,
            ),
            mock.patch.object(update.time, "sleep"),
        ):
            hooks = update._hooks(self.install)
            self.assertTrue(
                hooks.health_check(
                    {"container": True, "main": True, "glass_agent": False}
                )
            )

        self.assertEqual(ready.call_count, 3)
        ready.assert_called_with(
            self.install,
            min_uptime=5.0,
            max_heartbeat_age=5.0,
        )

    def test_maintenance_and_checkpoint_use_active_runtime_and_portable_paths(self):
        maintenance_status = {
            "maintenance_id": "tx-1",
            "epoch": 7,
            "phase": "draining",
            "safe_to_stop": True,
            "active_count": 0,
            "queued_message_count": 2,
        }
        checkpoint = {
            "root_hash": "a" * 64,
            "manifest_path": (
                "/silicon/.silicon/snapshots/manifests/" + "a" * 64 + ".json"
            ),
            "store": "/silicon/.silicon/snapshots",
            "provider": "stemcell-canonical",
        }
        responses = [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(maintenance_status) + "\n",
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(checkpoint) + "\n",
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"root_hash": "a" * 64}) + "\n",
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"root_hash": "a" * 64}) + "\n",
                stderr="",
            ),
        ]
        with (
            mock.patch.object(
                update.docker_runtime,
                "maintenance_coordinator_available",
                return_value=True,
            ),
            mock.patch.object(
                update.docker_runtime,
                "run_active_python",
                side_effect=responses,
            ) as active_python,
        ):
            hooks = update._hooks(self.install)
            hooks.request_drain("tx-1", None)
            created = hooks.create_checkpoint("tx-1", "generation-0")
            hooks.verify_checkpoint(created)
            hooks.restore_checkpoint(created)

        self.assertEqual(
            created["manifest_path"],
            str(
                self.root.resolve()
                / ".silicon"
                / "snapshots"
                / "manifests"
                / ("a" * 64 + ".json")
            ),
        )
        self.assertEqual(
            created["store"],
            str(self.root.resolve() / ".silicon" / "snapshots"),
        )
        maintenance_args = active_python.call_args_list[0].args[1]
        self.assertEqual(
            maintenance_args[:5],
            ["-m", "core.maintenance", "--root", "/silicon", "request"],
        )
        verify_args = active_python.call_args_list[2].args[1]
        self.assertIn(
            "/silicon/.silicon/snapshots/manifests/" + "a" * 64 + ".json",
            verify_args,
        )
        self.assertIn("/silicon/.silicon/snapshots", verify_args)
        restore_args = active_python.call_args_list[3].args[1]
        self.assertIn("restore_local_snapshot_in_place", restore_args[1])

    def test_docker_environment_is_prepared_before_the_engine_drain(self):
        expected = self.root / ".silicon" / "environments" / "ready"
        release = self.root / ".silicon" / "releases" / "candidate"
        runtime_image = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "e" * 64
        )
        with (
            mock.patch.object(
                update.docker_runtime,
                "prepare_release_image",
            ) as prepare_image,
            mock.patch.object(
                update.docker_runtime,
                "prepare_environment",
                return_value=expected,
            ) as prepare,
        ):
            hooks = update._hooks(self.install)
            actual = hooks.prepare_environment(release, runtime_image)

        self.assertEqual(actual, expected)
        prepare_image.assert_called_once_with(runtime_image)
        prepare.assert_called_once_with(
            self.install,
            release,
            image=runtime_image,
        )

    def test_docker_status_and_resume_are_host_owned(self):
        updater = mock.Mock()
        updater.status.return_value = {"active_transaction": None}
        updater.resume.return_value = {"state": "COMMITTED"}
        with (
            mock.patch.object(update.registry, "resolve_one", return_value=self.install),
            mock.patch.object(update, "_engine", return_value=updater),
            mock.patch.object(update, "_print_status"),
            mock.patch.object(update.docker_runtime, "run_silicon") as delegated,
        ):
            update._update_command(["status", "ada"])
            update._update_command(["resume", "ada", "tx-1"])

        updater.status.assert_called_once_with()
        updater.resume.assert_called_once_with("tx-1")
        delegated.assert_not_called()

    def test_mixed_fleet_preflights_every_target_before_any_run(self):
        local_root = self.root.parent / "local"
        local_root.mkdir()
        local = registry.Install(
            index=1,
            name="local",
            path=str(local_root),
            pid_file=str(local_root / ".silicon.pid"),
        )
        events: list[str] = []

        def updater_for(instance, *_args, **_kwargs):
            name = Path(instance).name
            fake = mock.Mock()
            fake.preflight.side_effect = lambda _release, **_kwargs: (
                events.append(f"preflight:{name}")
                or {
                    "safe_to_apply": True,
                    "plan": {"conflicts": []},
                }
            )
            fake.run.side_effect = lambda _release, **_kwargs: (
                events.append(f"run:{name}")
                or {"transaction_id": f"tx-{name}"}
            )
            return fake

        cache = object()
        release = self.signed_release()
        with (
            mock.patch.object(
                update, "REGISTRY_DIR", self.root.parent / ".cli-state"
            ),
            mock.patch.object(update, "_targets", return_value=[local, self.install]),
            mock.patch.object(update, "_cache", return_value=cache),
            mock.patch.object(update, "_fetch_latest", return_value=release) as fetch,
            mock.patch.object(
                update.registry,
                "installs",
                return_value=[local, self.install],
            ),
            mock.patch.object(
                update.docker_runtime,
                "prepare_release_image",
                return_value={"image": release.manifest.runtime_image},
            ),
            mock.patch.object(
                update.docker_runtime,
                "verify_runtime_contract",
            ) as verify_runtime,
            mock.patch.object(update, "_hooks", return_value=EngineHooks()),
            mock.patch.object(
                update, "TransactionalUpdater", side_effect=updater_for
            ) as updater_class,
            mock.patch.object(update.ui, "info"),
            mock.patch.object(update.ui, "success"),
            mock.patch.object(update.docker_runtime, "run_silicon") as delegated,
        ):
            update.update_instance("group")

        self.assertEqual(
            events,
            [
                "preflight:local",
                "preflight:ada",
                "run:local",
                "run:ada",
            ],
        )
        verify_runtime.assert_called_once_with(
            {"image": release.manifest.runtime_image},
            release.manifest.runtime_image,
        )
        fetch.assert_called_once_with(cache)
        delegated.assert_not_called()
        expected_roots = [Path(local.path), Path(self.install.path)]
        for call in updater_class.call_args_list:
            self.assertEqual(call.kwargs["all_instances"], expected_roots)

    def test_mixed_fleet_staging_failure_prevents_every_activation(self):
        local_root = self.root.parent / "local"
        local_root.mkdir()
        local = registry.Install(
            index=1,
            name="local",
            path=str(local_root),
            pid_file=str(local_root / ".silicon.pid"),
        )
        events: list[str] = []
        local_updater = mock.Mock()
        docker_updater = mock.Mock()
        local_updater.preflight.side_effect = lambda _release, **_kwargs: (
            events.append("preflight:local")
            or {"safe_to_apply": True, "plan": {"conflicts": []}}
        )

        def fail_docker(_release, **_kwargs):
            events.append("preflight:ada")
            raise RuntimeError("runtime image or dependency build unavailable")

        docker_updater.preflight.side_effect = fail_docker
        release = self.signed_release()
        with (
            mock.patch.object(
                update, "REGISTRY_DIR", self.root.parent / ".cli-state"
            ),
            mock.patch.object(update, "_targets", return_value=[local, self.install]),
            mock.patch.object(update, "_cache", return_value=object()),
            mock.patch.object(update, "_fetch_latest", return_value=release),
            mock.patch.object(
                update.registry,
                "installs",
                return_value=[local, self.install],
            ),
            mock.patch.object(
                update.docker_runtime,
                "prepare_release_image",
                return_value={"image": release.manifest.runtime_image},
            ),
            mock.patch.object(
                update.docker_runtime,
                "verify_runtime_contract",
            ),
            mock.patch.object(update, "_hooks", return_value=EngineHooks()),
            mock.patch.object(
                update,
                "TransactionalUpdater",
                side_effect=[local_updater, docker_updater],
            ),
            mock.patch.object(update.ui, "info"),
            mock.patch.object(update.ui, "error"),
            self.assertRaises(SystemExit) as raised,
        ):
            update.update_instance("group")

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(events, ["preflight:local", "preflight:ada"])
        local_updater.run.assert_not_called()
        docker_updater.run.assert_not_called()

    def test_rolling_activation_stops_after_first_runtime_failure(self):
        installs = []
        updaters = []
        for index, name in enumerate(("one", "two", "three")):
            path = self.root.parent / name
            path.mkdir()
            installs.append(
                registry.Install(
                    index=index,
                    name=name,
                    path=str(path),
                    pid_file=str(path / ".silicon.pid"),
                )
            )
            updater = mock.Mock()
            updater.preflight.return_value = {
                "safe_to_apply": True,
                "plan": {"conflicts": []},
            }
            updater.run.return_value = {"transaction_id": f"tx-{name}"}
            updaters.append(updater)
        updaters[1].run.side_effect = RuntimeError("health check failed")
        release = self.signed_release()

        with (
            mock.patch.object(
                update, "REGISTRY_DIR", self.root.parent / ".cli-state"
            ),
            mock.patch.object(update, "_targets", return_value=installs),
            mock.patch.object(update, "_cache", return_value=object()),
            mock.patch.object(update, "_fetch_latest", return_value=release),
            mock.patch.object(update.registry, "installs", return_value=installs),
            mock.patch.object(update, "_hooks", return_value=EngineHooks()),
            mock.patch.object(
                update, "TransactionalUpdater", side_effect=updaters
            ),
            mock.patch.object(update.ui, "info"),
            mock.patch.object(update.ui, "success"),
            mock.patch.object(update.ui, "error"),
            self.assertRaises(SystemExit) as raised,
        ):
            update.update_instance("group")

        self.assertEqual(raised.exception.code, 2)
        updaters[0].run.assert_called_once()
        updaters[1].run.assert_called_once()
        updaters[2].run.assert_not_called()
        updaters[0].rollback.assert_called_once_with(
            deadline=None,
            transaction_id="tx-one",
            lock_held=True,
        )


if __name__ == "__main__":
    unittest.main()


class HealthBudgetTests(unittest.TestCase):
    """A restarted Silicon must be given time to actually come up.

    The health gate requires min_uptime=5s plus a fresh heartbeat plus three
    consecutive observations, and a booting Silicon calls Glass for provider
    keys first. When the budget was 12s a merely slow start read as an
    unhealthy one: recovery was declared failed and the interrupted transaction
    it left behind blocked every later update until resumed by hand.
    """

    def test_budgets_exceed_the_minimum_a_healthy_restart_needs(self):
        # min_uptime (5s) + three observations 0.5s apart, with headroom for
        # container start and the Glass round-trip during boot.
        floor = 5.0 + 3 * 0.5
        self.assertGreater(update.HEALTH_BUDGET_SECONDS, floor * 4)
        self.assertGreater(update.HEALTH_BUDGET_DOCKER_SECONDS, floor * 4)

    def test_docker_budget_is_not_tighter_than_local(self):
        # Docker adds container start on top of everything the local path does.
        self.assertGreaterEqual(
            update.HEALTH_BUDGET_DOCKER_SECONDS,
            update.HEALTH_BUDGET_SECONDS,
        )
