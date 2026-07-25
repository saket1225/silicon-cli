from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli import process


class RuntimeChildHealthTests(unittest.TestCase):
    def _instance(self, root: Path) -> tuple[Path, Path]:
        (root / "main.py").write_text("print('ready')\n", encoding="utf-8")
        pid_file = root / ".silicon.pid"
        pid_file.write_text("100\n", encoding="utf-8")
        process.runtime_meta_file(root).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "supervisor_pid": 100,
                    "supervisor_identity": "supervisor-birth",
                    "child_pid": 101,
                    "child_identity": "child-birth",
                    "generation": str(root.resolve()),
                    "started_at": time.time() - 10,
                }
            ),
            encoding="utf-8",
        )
        return root, pid_file

    def test_requires_live_child_not_only_live_watchdog(self):
        with tempfile.TemporaryDirectory() as raw:
            root, pid_file = self._instance(Path(raw))
            with mock.patch.object(
                process,
                "_alive",
                side_effect=lambda pid: pid == 100,
            ), mock.patch.object(
                process,
                "_process_identity",
                side_effect=lambda pid: (
                    "supervisor-birth" if pid == 100 else ""
                ),
            ):
                self.assertTrue(process.is_running(str(pid_file)))
                self.assertFalse(
                    process.runtime_healthy(root, pid_file, min_uptime=5)
                )

    def test_requires_active_generation_and_stable_uptime(self):
        with tempfile.TemporaryDirectory() as raw:
            root, pid_file = self._instance(Path(raw))
            with (
                mock.patch.object(process, "_alive", return_value=True),
                mock.patch.object(
                    process,
                    "_process_identity",
                    side_effect=lambda pid: (
                        "supervisor-birth"
                        if pid == 100
                        else "child-birth"
                    ),
                ),
            ):
                self.assertTrue(
                    process.runtime_healthy(root, pid_file, min_uptime=5)
                )
                metadata = json.loads(
                    process.runtime_meta_file(root).read_text(encoding="utf-8")
                )
                metadata["started_at"] = time.time()
                process.runtime_meta_file(root).write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )
                self.assertFalse(
                    process.runtime_healthy(root, pid_file, min_uptime=5)
                )

                other = root / "other"
                other.mkdir()
                (other / "main.py").write_text("", encoding="utf-8")
                metadata["started_at"] = time.time() - 10
                metadata["generation"] = str(other)
                process.runtime_meta_file(root).write_text(
                    json.dumps(metadata),
                    encoding="utf-8",
                )
                self.assertFalse(
                    process.runtime_healthy(root, pid_file, min_uptime=5)
                )

    def test_rejects_linked_or_oversized_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root, pid_file = self._instance(Path(raw))
            metadata = process.runtime_meta_file(root)
            metadata.unlink()
            target = root / "elsewhere.json"
            target.write_text("{}\n", encoding="utf-8")
            try:
                metadata.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with (
                mock.patch.object(process, "_alive", return_value=True),
                mock.patch.object(
                    process,
                    "_process_identity",
                    side_effect=lambda pid: (
                        "supervisor-birth"
                        if pid == 100
                        else "child-birth"
                    ),
                ),
            ):
                self.assertIsNone(process.runtime_child_status(root, pid_file))

    def test_application_ready_requires_fresh_matching_child_heartbeat(self):
        with tempfile.TemporaryDirectory() as raw:
            root, pid_file = self._instance(Path(raw))
            health = root / ".silicon" / "runtime-health.json"
            health.parent.mkdir(parents=True)
            now = time.time()
            health.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "pid": 101,
                        "code_root": str(root.resolve()),
                        "ready": True,
                        "ready_at": now - 9,
                        "heartbeat_at": now,
                        "phase": "updating",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(process, "_alive", return_value=True),
                mock.patch.object(
                    process,
                    "_process_identity",
                    side_effect=lambda pid: (
                        "supervisor-birth"
                        if pid == 100
                        else "child-birth"
                    ),
                ),
            ):
                self.assertTrue(
                    process.runtime_ready(root, pid_file, min_uptime=5)
                )
                value = json.loads(health.read_text(encoding="utf-8"))
                value["heartbeat_at"] = now - 60
                health.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(
                    process.runtime_ready(root, pid_file, min_uptime=5)
                )

    def test_reused_supervisor_pid_is_not_treated_as_our_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root, pid_file = self._instance(Path(raw))
            with (
                mock.patch.object(process, "_alive", return_value=True),
                mock.patch.object(
                    process,
                    "_process_identity",
                    return_value="different-process-birth",
                ),
            ):
                self.assertFalse(process.is_running(str(pid_file)))
                self.assertIsNone(
                    process.runtime_child_status(root, pid_file)
                )

    def test_pid_reader_and_publication_reject_symbolic_links(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "unrelated"
            target.write_text("4242\n", encoding="utf-8")
            pid_file = root / ".silicon.pid"
            try:
                pid_file.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            self.assertIsNone(process.get_pid(str(pid_file)))
            with self.assertRaisesRegex(RuntimeError, "PID file is unsafe"):
                process._publish_pid(root, pid_file, 1234)
            self.assertEqual(target.read_text(encoding="utf-8"), "4242\n")

    def test_stop_sentinel_never_follows_a_symbolic_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "unrelated"
            target.write_text("keep\n", encoding="utf-8")
            sentinel = root / ".silicon.stop"
            try:
                sentinel.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(RuntimeError, "sentinel is unsafe"):
                process._publish_stop_sentinel(root)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_watchdog_waits_for_its_own_durable_pid_publication(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / ".silicon.pid"
            with (
                mock.patch.object(process.os, "getpid", return_value=321),
                mock.patch.object(
                    process,
                    "get_pid",
                    side_effect=[None, "321"],
                ),
                mock.patch.object(process.time, "sleep"),
            ):
                self.assertTrue(
                    process._await_watchdog_publication(
                        pid_file, timeout=1, poll_interval=0.001
                    )
                )

    def test_stale_watchdog_exits_when_a_new_parent_publishes_another_pid(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / ".silicon.pid"
            with (
                mock.patch.object(process.os, "getpid", return_value=321),
                mock.patch.object(process, "get_pid", return_value="654"),
            ):
                self.assertFalse(
                    process._await_watchdog_publication(pid_file, timeout=1)
                )

    def test_unpublished_watchdog_times_out_without_starting_silicon(self):
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / ".silicon.pid"
            with (
                mock.patch.object(process.os, "getpid", return_value=321),
                mock.patch.object(process, "get_pid", return_value=None),
            ):
                self.assertFalse(
                    process._await_watchdog_publication(pid_file, timeout=0)
                )

    def test_runtime_lifecycle_lock_serializes_concurrent_start_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entered = threading.Event()
            completed = threading.Event()

            def second_owner():
                with process._runtime_lifecycle_lock(
                    root, "second", timeout=2
                ):
                    entered.set()
                completed.set()

            with process._runtime_lifecycle_lock(root, "first", timeout=1):
                thread = threading.Thread(target=second_owner)
                thread.start()
                self.assertFalse(entered.wait(0.15))
            self.assertTrue(entered.wait(2))
            self.assertTrue(completed.wait(2))
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_corrupt_metadata_cannot_hide_live_watchdog_from_stop(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / ".silicon.pid"
            pid_file.write_text("123\n", encoding="utf-8")
            process.runtime_meta_file(root).write_text(
                "{corrupt", encoding="utf-8"
            )
            install = process.registry.Install(
                0, "ada", str(root), str(pid_file)
            )
            with (
                mock.patch.object(
                    process.registry, "resolve_one", return_value=install
                ),
                mock.patch.object(process, "is_running", return_value=False),
                mock.patch.object(
                    process, "_legacy_watchdog_matches", return_value=True
                ),
                mock.patch.object(process, "_alive", return_value=False),
                mock.patch.object(process, "kill_floaters"),
                mock.patch.object(process.os, "kill") as kill,
                mock.patch.object(process.glassagent, "stop"),
            ):
                process._stop_one_unlocked("ada", full=True)
            kill.assert_called_once_with(123, process.signal.SIGTERM)
            self.assertFalse(pid_file.exists())
            self.assertFalse(process.runtime_meta_file(root).exists())

    def test_corrupt_metadata_keeps_command_owned_watchdog_conservatively_active(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / ".silicon.pid"
            pid_file.write_text("123\n", encoding="utf-8")
            process.runtime_meta_file(root).write_text(
                "{corrupt", encoding="utf-8"
            )
            with mock.patch.object(
                process, "_legacy_watchdog_matches", return_value=True
            ):
                self.assertTrue(process.is_running(str(pid_file)))


if __name__ == "__main__":
    unittest.main()
