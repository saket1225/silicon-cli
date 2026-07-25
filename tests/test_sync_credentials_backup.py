from __future__ import annotations

import json
import os
import signal
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import cli, registry, sync


class CredentialStorageTests(unittest.TestCase):
    def test_pull_seeds_one_private_silicon_credential_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            secret = "scs_live_canonical"
            (target / ".env").write_text(
                "# keep this\n"
                "GLASS_API_KEY=scs_live_old\n"
                "SILICON_UPDATE_AUTH_KEY=scs_live_older\n"
                "UNRELATED=value\n"
            )
            (target / "env.py").write_text(
                'GLASS_API_KEY = "scs_live_old"\n'
                'SILICON_UPDATE_AUTH_KEY = "scs_live_older"\n'
                'BROWSER_PROFILE = "ada"\n'
            )
            (target / "silicon.json").write_text(
                json.dumps(
                    {
                        "glass": {
                            "api_key": "scs_live_old",
                            "silicon_api_key": "scs_live_older",
                            "custom": "preserved",
                        }
                    }
                )
            )

            sync._seed_glass_files(
                target,
                server="https://glass.example",
                api_key=secret,
                silicon={"silicon_id": "sil-1", "name": "Ada"},
                instance_name="ada",
                provider_key_env={"OPENAI_API_KEY": "provider-secret"},
            )

            glass_path = target / ".glass.json"
            glass = json.loads(glass_path.read_text())
            self.assertEqual(glass["api_key"], secret)
            self.assertNotIn("silicon_api_key", glass)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(glass_path.stat().st_mode), 0o600)

            dotenv = (target / ".env").read_text()
            env_py = (target / "env.py").read_text()
            silicon_json = (target / "silicon.json").read_text()
            self.assertNotIn("GLASS_API_KEY=", dotenv)
            self.assertNotIn("SILICON_UPDATE_AUTH_KEY=", dotenv)
            self.assertIn("UNRELATED=value", dotenv)
            self.assertIn("OPENAI_API_KEY=provider-secret", dotenv)
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE((target / ".env").stat().st_mode),
                    0o600,
                )
            self.assertIn('GLASS_API_KEY = ""', env_py)
            self.assertNotIn("SILICON_UPDATE_AUTH_KEY", env_py)
            self.assertIn('BROWSER_PROFILE = "ada"', env_py)
            self.assertNotIn(secret, dotenv + env_py + silicon_json)

            public_glass = json.loads(silicon_json)["glass"]
            self.assertEqual(public_glass["custom"], "preserved")
            self.assertEqual(public_glass["server_url"], "https://glass.example")
            self.assertNotIn("api_key", public_glass)
            self.assertNotIn("silicon_api_key", public_glass)

class CanonicalBackupTests(unittest.TestCase):
    @staticmethod
    def _write_generation_pointer(root: Path) -> None:
        tree = "a" * 64
        (root / ".silicon" / "current.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "immutable-release",
                    "generation_id": "generation-1",
                    "release_path": ".silicon/releases/generation-1",
                    "upstream_tree_sha256": tree,
                    "materialized_tree_sha256": tree,
                    "environment_path": "",
                    "release": {
                        "version": "2.0.0",
                        "revision": "b" * 64,
                        "sequence": 2,
                        "tree_sha256": tree,
                        "artifact_sha256": "c" * 64,
                        "source": "glass",
                        "trust": "signed-ed25519",
                    },
                    "overlay_root_hash": "d" * 64,
                    "activated_at": 1,
                }
            )
        )

    def test_manifest_backup_runs_the_instance_backup_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".backupsilicon").write_text("prompts/MEMORY.md\n")
            (root / "core").mkdir()
            (root / "core" / "backup.py").write_text("# canonical implementation\n")
            completed = SimpleNamespace(returncode=0)
            with (
                mock.patch.object(
                    sync,
                    "python_run_cmd",
                    return_value="/instance/python",
                ),
                mock.patch.object(
                    sync.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
                mock.patch.object(
                    sync.backup_runtime,
                    "capture_active_customizations",
                ) as capture,
            ):
                result = sync._manifest_backup_now(
                    str(root),
                    note="manual",
                    instance_name="ada",
                )

            self.assertTrue(result)
            args = run.call_args.args[0]
            self.assertEqual(args[0], "/instance/python")
            self.assertEqual(args[-2:], [str(root.resolve()), "manual"])
            self.assertEqual(run.call_args.kwargs["cwd"], root.resolve())
            self.assertEqual(
                run.call_args.kwargs["env"]["SILICON_DATA_ROOT"],
                str(root.resolve()),
            )
            capture.assert_called_once_with(root.resolve())

    def test_old_stemcell_has_an_actionable_update_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(sync.ui, "error") as error,
                mock.patch.object(sync.subprocess, "run") as run,
            ):
                result = sync._manifest_backup_now(
                    str(root),
                    note="manual",
                    instance_name="ada",
                )

            self.assertFalse(result)
            run.assert_not_called()
            self.assertIn("silicon update ada", error.call_args.args[0])

    def test_no_manifest_is_delegated_to_the_canonical_implementation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "core").mkdir()
            (root / "core" / "backup.py").write_text("# canonical implementation\n")
            with (
                mock.patch.object(sync, "python_run_cmd", return_value="/instance/python"),
                mock.patch.object(
                    sync.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1),
                ) as run,
                mock.patch.object(
                    sync.backup_runtime,
                    "capture_active_customizations",
                ),
            ):
                result = sync._manifest_backup_now(
                    str(root),
                    note="manual",
                    instance_name="ada",
                )

            self.assertFalse(result)
            run.assert_called_once()

    def test_push_routes_to_the_canonical_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".glass.json").write_text('{"api_key": "key"}\n')
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
            )
            with (
                mock.patch.object(sync.registry, "resolve_one", return_value=inst),
                mock.patch.object(
                    sync,
                    "_manifest_backup_now",
                    return_value=True,
                ) as backup_now,
                mock.patch.object(sync.ui, "info"),
                mock.patch.object(sync.ui, "success"),
            ):
                sync.push("ada", "now")

            backup_now.assert_called_once_with(
                str(root),
                note="manual",
                instance_name="ada",
                installation=inst,
            )

    def test_schedule_starts_only_after_a_canonical_backup_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".glass.json").write_text('{"api_key": "key"}\n')
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
            )
            with (
                mock.patch.object(sync.registry, "resolve_one", return_value=inst),
                mock.patch.object(sync, "_manifest_backup_now", return_value=True),
                mock.patch.object(sync, "_start_backup_loop") as start_loop,
                mock.patch.object(sync.ui, "info"),
            ):
                sync.push("ada", None)

            start_loop.assert_called_once_with(str(root), "ada")

    def test_schedule_command_does_not_backup_or_spawn_when_loop_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".glass.json").write_text('{"api_key": "key"}\n')
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
            )
            with (
                mock.patch.object(sync.registry, "resolve_one", return_value=inst),
                mock.patch.object(
                    sync,
                    "_backup_supervisor_info",
                    return_value={
                        "running": True,
                        "responsive": True,
                        "pid": 123,
                    },
                ),
                mock.patch.object(sync, "_manifest_backup_now") as backup_now,
                mock.patch.object(sync, "_start_backup_loop") as start_loop,
                mock.patch.object(sync.ui, "warn"),
            ):
                sync.push("ada", None)

            backup_now.assert_not_called()
            start_loop.assert_not_called()

    def test_background_loop_always_uses_the_internal_backup_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(
                    sync.subprocess,
                    "Popen",
                    return_value=SimpleNamespace(pid=123),
                ) as popen,
                mock.patch.object(sync.ui, "info"),
                mock.patch.object(sync.ui, "success"),
            ):
                sync._start_backup_loop(str(root), "ada")

            command = popen.call_args.args[0]
            self.assertEqual(command[:4], [
                sync.sys.executable,
                "-m",
                "silicon_cli.cli",
                "_backup_loop",
            ])
            self.assertEqual(Path(command[4]), root.resolve())
            self.assertEqual(command[5], "ada")
            self.assertEqual(len(command[6]), 32)
            self.assertNotIn("glass", command)

    def test_backup_imports_from_active_immutable_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ".silicon" / "releases" / "generation-1"
            (release / "core").mkdir(parents=True)
            (release / "main.py").write_text("# active\n")
            (release / "core" / "backup.py").write_text("# canonical\n")
            self._write_generation_pointer(root)
            with (
                mock.patch.object(
                    sync.backup_runtime,
                    "capture_active_customizations",
                ),
                mock.patch.object(
                    sync,
                    "python_run_cmd",
                    return_value="/instance/python",
                ),
                mock.patch.object(
                    sync.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0),
                ) as run,
            ):
                self.assertTrue(
                    sync._manifest_backup_now(
                        str(root),
                        note="manual",
                        instance_name="ada",
                    )
                )

            self.assertEqual(run.call_args.kwargs["cwd"], release.resolve())
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["SILICON_DATA_ROOT"], str(root.resolve()))
            self.assertEqual(
                environment["SILICON_RELEASE_ROOT"],
                str(release.resolve()),
            )

    def test_docker_backup_is_host_owned_and_runs_active_canonical_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ".silicon" / "releases" / "generation-1"
            (release / "core").mkdir(parents=True)
            (release / "main.py").write_text("# active\n")
            (release / "core" / "backup.py").write_text("# canonical\n")
            self._write_generation_pointer(root)
            inst = registry.Install(
                index=0,
                name="ada",
                path=str(root),
                pid_file=str(root / ".silicon.pid"),
                runtime="docker",
            )
            with (
                mock.patch.object(
                    sync.backup_runtime,
                    "capture_active_customizations",
                ) as capture,
                mock.patch.object(
                    sync.docker_runtime,
                    "run_active_python",
                    return_value=SimpleNamespace(returncode=0),
                ) as active_python,
                mock.patch.object(
                    sync.docker_runtime,
                    "run_silicon",
                ) as stale_cli,
            ):
                self.assertTrue(
                    sync._manifest_backup_now(
                        str(root),
                        installation=inst,
                        instance_name="ada",
                    )
                )

            capture.assert_called_once_with(root.resolve())
            stale_cli.assert_not_called()
            arguments = active_python.call_args.args[1]
            self.assertEqual(arguments[-2:], ["/silicon", "manual"])
            self.assertFalse(active_python.call_args.kwargs["capture"])


class BackupSupervisorTests(unittest.TestCase):
    def test_start_refuses_a_live_existing_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(
                    sync,
                    "_backup_supervisor_info",
                    return_value={
                        "running": True,
                        "responsive": False,
                        "pid": 123,
                    },
                ),
                mock.patch.object(sync.subprocess, "Popen") as popen,
                mock.patch.object(sync.ui, "warn"),
            ):
                self.assertFalse(sync._start_backup_loop(str(root), "ada"))
            popen.assert_not_called()

    def test_corrupt_lease_never_authorizes_signalling_a_probable_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / sync.BACKUP_PID_NAME).write_text(str(os.getpid()))
            with (
                mock.patch.object(
                    sync,
                    "_inspect_process_command",
                    return_value=(
                        True,
                        f"python -m silicon_cli.cli _backup_loop {root} ada token",
                    ),
                ),
                mock.patch.object(sync.os, "kill") as kill,
                mock.patch.object(sync.ui, "warn"),
            ):
                info = sync._backup_supervisor_info(root)
                self.assertTrue(info["running"])
                self.assertFalse(info["verified_identity"])
                self.assertFalse(sync._stop_backup_loop(str(root), "ada"))
            self.assertNotIn(
                mock.call(os.getpid(), signal.SIGTERM),
                kill.call_args_list,
            )

    def test_failed_cycle_retries_only_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            token = "a" * 32
            sync._install_backup_lease(
                root,
                name="ada",
                pid=os.getpid(),
                token=token,
            )
            waits = []

            def wait(_stop, seconds, heartbeat):
                waits.append(seconds)
                heartbeat()
                return len(waits) > 3

            with (
                mock.patch.object(
                    sync,
                    "BACKUP_RETRY_DELAYS",
                    (1.0, 2.0),
                ),
                mock.patch.object(
                    sync,
                    "_seconds_until_next_backup",
                    return_value=0.0,
                ),
                mock.patch.object(
                    sync,
                    "_wait_with_backup_heartbeat",
                    side_effect=wait,
                ),
                mock.patch.object(
                    sync,
                    "_manifest_backup_now",
                    return_value=False,
                ) as backup_now,
                mock.patch.object(sync, "_registered_install", return_value=None),
                mock.patch.object(sync.ui, "info"),
            ):
                sync.backup_loop(str(root), "ada", token)

            self.assertEqual(backup_now.call_count, 3)
            self.assertEqual(waits[:3], [0.0, 1.0, 2.0])
            self.assertFalse((root / sync.BACKUP_PID_NAME).exists())
            status = json.loads(
                (
                    root
                    / ".silicon"
                    / sync.BACKUP_STATUS_NAME
                ).read_text()
            )
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(status["consecutive_failures"], 3)

    def test_persisted_intent_reconciles_local_and_docker_without_duplicates(self):
        for runtime in ("local", "docker"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                inst = registry.Install(
                    index=0,
                    name=f"ada-{runtime}",
                    path=str(root),
                    pid_file=str(root / ".silicon.pid"),
                    runtime=runtime,
                )
                sync._set_backup_schedule(root, inst.name, enabled=True)
                with (
                    mock.patch.object(
                        sync,
                        "_backup_supervisor_info",
                        return_value={"running": False},
                    ),
                    mock.patch.object(
                        sync,
                        "_start_backup_loop",
                        return_value=True,
                    ) as start,
                ):
                    self.assertTrue(
                        sync.reconcile_backup_supervisor(inst, quiet=True)
                    )
                start.assert_called_once_with(
                    str(root),
                    inst.name,
                    persist_intent=False,
                    quiet=True,
                )

                with (
                    mock.patch.object(
                        sync,
                        "_backup_supervisor_info",
                        return_value={"running": True},
                    ),
                    mock.patch.object(sync, "_start_backup_loop") as duplicate,
                ):
                    self.assertTrue(
                        sync.reconcile_backup_supervisor(inst, quiet=True)
                    )
                duplicate.assert_not_called()

    def test_stop_persists_opt_out_even_when_no_process_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sync._set_backup_schedule(root, "ada", enabled=True)
            with (
                mock.patch.object(
                    sync,
                    "_backup_supervisor_info",
                    return_value={"running": False},
                ),
                mock.patch.object(sync.ui, "warn"),
            ):
                self.assertFalse(sync._stop_backup_loop(str(root), "ada"))
            self.assertFalse(sync._backup_schedule(root)["enabled"])

    def test_normal_cli_startup_reconciles_persisted_schedules(self):
        with (
            mock.patch.object(sync, "reconcile_backup_schedules") as reconcile,
            mock.patch.object(cli, "cmd_help"),
        ):
            cli.main(["help"])
        reconcile.assert_called_once_with(quiet=True)


if __name__ == "__main__":
    unittest.main()
