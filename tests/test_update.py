from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import registry, update
from silicon_cli.updater import snapshot_adapter


class CliUpdateTests(unittest.TestCase):
    def test_script_update_upgrades_published_package_with_owning_interpreter(self):
        with (
            mock.patch.object(
                update.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            mock.patch.object(update.ui, "info"),
            mock.patch.object(update.ui, "success"),
        ):
            update.update_cli()

        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "silicon-cli",
            ]
        )

    def test_update_command_routes_dry_run_without_stopping_services(self):
        with (
            mock.patch.object(update, "update_instance") as update_instance,
        ):
            update.update_command(["--dry-run", "ada"])

        update_instance.assert_called_once_with(
            "ada",
            dry_run=True,
            deadline_seconds=None,
        )

    def test_removed_unsigned_git_flag_fails_closed(self):
        with (
            mock.patch.object(update, "update_instance") as update_instance,
            self.assertRaises(SystemExit),
        ):
            update.update_command(["--allow-unsigned-git", "ada"])

        update_instance.assert_not_called()

    def test_update_deadline_rejects_nonfinite_and_unbounded_values(self):
        for value in ("nan", "inf", "169h"):
            with self.subTest(value=value), self.assertRaises(
                update.UpdateError
            ):
                update._parse_duration(value)
        self.assertEqual(update._parse_duration("30m"), 1800.0)

    def test_hooks_capture_and_restore_main_and_agent_separately(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(update.process, "is_running", return_value=True),
            mock.patch.object(update.glassagent, "status", return_value=False),
            mock.patch.object(update.process, "start_one") as start,
        ):
            hooks = update._hooks(inst)
            self.assertEqual(
                hooks.service_state(),
                {
                    "main": True,
                    "glass_agent": False,
                    "interface": False,
                },
            )
            hooks.start_services(
                {
                    "main": True,
                    "glass_agent": False,
                    "interface": False,
                }
            )

        start.assert_called_once_with(
            "ada", start_agent=False, reconcile_updates=False
        )

    def test_hooks_restore_listener_only_service_state(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(update.process, "is_running", return_value=False),
            mock.patch.object(update.glassagent, "status", return_value=False),
            mock.patch.object(
                update.interface_cli,
                "daemon_running",
                return_value=True,
            ),
            mock.patch.object(
                update.interface_cli,
                "start_daemon",
                return_value=True,
            ) as start_interface,
        ):
            hooks = update._hooks(inst)
            state = hooks.service_state()
            hooks.start_services(state)

        self.assertEqual(
            state,
            {
                "main": False,
                "glass_agent": False,
                "interface": True,
            },
        )
        start_interface.assert_called_once_with(
            inst.path,
            required=True,
        )

    def test_hooks_quiesce_interface_before_protected_checkpoint(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(
                update.interface_cli,
                "daemon_running",
                return_value=True,
            ),
            mock.patch.object(
                update.interface_cli,
                "stop_daemon",
            ) as stop_interface,
        ):
            update._hooks(inst).quiesce_delivery()

        stop_interface.assert_called_once_with(
            inst.path,
            required=True,
        )

    def test_same_instance_task_cannot_deadlock_updating_itself(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(update, "_targets", return_value=[inst]),
            mock.patch.dict(
                update.os.environ,
                {"SILICON_DATA_ROOT": "/tmp/ada"},
                clear=False,
            ),
            mock.patch.object(update, "_fetch_latest") as fetch,
            self.assertRaisesRegex(update.UpdateError, "would deadlock"),
        ):
            update.update_instance("ada")
        fetch.assert_not_called()

    def test_invalid_git_release_fails_cleanly_before_any_stop_or_drain(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(update, "_targets", return_value=[inst]),
            mock.patch.object(update.FleetJournal, "active", return_value=None),
            mock.patch.object(update, "_cache", return_value=object()),
            mock.patch.object(
                update,
                "_fetch_latest",
                side_effect=update.ReleaseChannelError(
                    "published Stemcell has no immutable runtime_image digest"
                ),
            ),
            mock.patch.object(
                update,
                "TransactionalUpdater",
            ) as updater,
            mock.patch.object(update.process, "stop_one") as stop,
            mock.patch.object(
                update.MaintenanceProtocol,
                "request_drain",
            ) as drain,
            mock.patch.object(update.ui, "error") as error,
            self.assertRaises(SystemExit) as raised,
        ):
            update.update_command(["ada"])

        self.assertEqual(raised.exception.code, 2)
        updater.assert_not_called()
        stop.assert_not_called()
        drain.assert_not_called()
        error.assert_called_once()

    def test_runtime_preflight_failure_is_reported_without_a_traceback(self):
        with (
            mock.patch.object(
                update,
                "update_instance",
                side_effect=RuntimeError(
                    "published runtime contract verification failed"
                ),
            ),
            mock.patch.object(update.ui, "error") as error,
            self.assertRaises(SystemExit) as raised,
        ):
            update.update_command(["ada"])

        self.assertEqual(raised.exception.code, 2)
        error.assert_called_once_with(
            "published runtime contract verification failed"
        )

    def test_startup_reconciles_nonterminal_update_before_work(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        updater = mock.Mock()
        updater.status.return_value = {
            "active_transaction": {
                "transaction_id": "tx-1",
                "state": "ACTIVATED",
            }
        }
        updater.resume.return_value = {"state": "ROLLED_BACK"}
        with (
            mock.patch.object(update, "_engine", return_value=updater),
            mock.patch.object(update.ui, "warn"),
            mock.patch.object(update.ui, "success"),
        ):
            update.reconcile_before_start(inst)
        updater.resume.assert_called_once_with("tx-1")

    def test_local_update_health_requires_stable_main_child(self):
        inst = registry.Install(
            index=0,
            name="ada",
            path="/tmp/ada",
            pid_file="/tmp/ada/.silicon.pid",
        )
        with (
            mock.patch.object(
                update.process, "runtime_ready", return_value=True
            ) as runtime_ready,
            mock.patch.object(update.glassagent, "status", return_value=False),
            mock.patch.object(update.time, "sleep"),
        ):
            hooks = update._hooks(inst)
            self.assertTrue(
                hooks.health_check({"main": True, "glass_agent": False})
            )

        self.assertEqual(runtime_ready.call_count, 3)
        runtime_ready.assert_called_with(
            inst.path,
            inst.pid_file,
            min_uptime=5.0,
            max_heartbeat_age=5.0,
        )

    def test_local_bootstrap_checkpoint_has_an_in_place_restorer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".silicon").mkdir()
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
            )
            checkpoint = {
                "provider": "silicon-cli-bootstrap",
                "root_hash": "a" * 64,
                "manifest_path": str(root / ".silicon" / "snapshot.json"),
                "store": str(root / ".silicon"),
            }
            with mock.patch.object(
                snapshot_adapter,
                "restore_local_snapshot_in_place",
                return_value={"root_hash": "a" * 64},
            ) as restore:
                update._hooks(inst).restore_checkpoint(checkpoint)

        restore.assert_called_once_with(
            root,
            Path(checkpoint["manifest_path"]),
            store=Path(checkpoint["store"]),
        )

    def test_post_commit_retention_runs_active_canonical_snapshot_gc(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = root / ".silicon" / "releases" / "active"
            release.mkdir(parents=True)
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
            )
            completed = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"manifests":["'
                    + "a" * 64
                    + '"],"objects":["'
                    + "b" * 64
                    + '"]}\n'
                ),
                stderr="",
            )
            with (
                mock.patch.object(
                    update,
                    "active_release_root",
                    return_value=release,
                ),
                mock.patch.object(
                    update,
                    "python_run_cmd",
                    return_value="/python",
                ),
                mock.patch.object(
                    update,
                    "runtime_environment",
                    return_value={"SILICON_DATA_ROOT": str(root)},
                ),
                mock.patch.object(
                    update.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                result = update._canonical_snapshot_gc(inst)()

        self.assertEqual(
            result,
            {
                "manifests": ["a" * 64],
                "objects": ["b" * 64],
            },
        )
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/python")
        self.assertIn("garbage_collect_referenced_snapshots", command[2])
        self.assertEqual(command[-1], str(root))
        self.assertEqual(run.call_args.kwargs["cwd"], release)


if __name__ == "__main__":
    unittest.main()
