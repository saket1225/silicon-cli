from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli import glassagent


class _GateStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Process:
    def __init__(self, process_id: int = 4321) -> None:
        self.pid = process_id
        self.stdin = _GateStream()
        self.terminated = False
        self.killed = False
        self.waits: list[float] = []

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        return 0


class GlassAgentProcessTests(unittest.TestCase):
    def _configured_instance(self, root: Path) -> None:
        (root / ".glass.json").write_text("{}\n", encoding="utf-8")

    def _start_patches(self, proc: _Process):
        return (
            mock.patch.object(
                glassagent, "legacy_offline_update_fenced", return_value=False
            ),
            mock.patch.object(glassagent, "status", return_value=False),
            mock.patch.object(glassagent, "_identity", return_value="birth-1"),
            mock.patch.object(glassagent, "_spawn", return_value=(proc, True)),
            mock.patch.object(glassagent.ui, "info"),
        )

    def test_pid_reader_is_bounded_regular_and_never_follows_links(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / ".glass_agent.pid"
            pid_file.write_text("1234\n", encoding="ascii")
            self.assertEqual(glassagent.pid(str(root)), 1234)

            pid_file.write_bytes(b"1" * 33)
            self.assertIsNone(glassagent.pid(str(root)))

            pid_file.unlink()
            target = root / "unrelated"
            target.write_text("9999\n", encoding="ascii")
            try:
                pid_file.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            self.assertIsNone(glassagent.pid(str(root)))
            self.assertEqual(target.read_text(encoding="ascii"), "9999\n")

    def test_start_rejects_control_symlink_without_launching_or_overwriting(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._configured_instance(root)
            target = root / "unrelated"
            target.write_text("keep\n", encoding="utf-8")
            try:
                (root / ".glass_agent.pid").symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with (
                mock.patch.object(
                    glassagent,
                    "legacy_offline_update_fenced",
                    return_value=False,
                ),
                mock.patch.object(glassagent, "status", return_value=False),
                mock.patch.object(glassagent, "_spawn") as spawn,
                self.assertRaisesRegex(RuntimeError, "control path is unsafe"),
            ):
                glassagent.start(str(root))
            spawn.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    @unittest.skipIf(os.name == "nt", "POSIX file modes are required")
    def test_start_publishes_atomic_private_identity_then_pid_and_opens_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._configured_instance(root)
            proc = _Process()
            patches = self._start_patches(proc)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                glassagent.start(str(root))

            pid_file = root / ".glass_agent.pid"
            identity_file = root / ".glass_agent.pid.meta.json"
            self.assertEqual(pid_file.read_text(encoding="ascii"), "4321\n")
            self.assertIn('"identity": "birth-1"', identity_file.read_text())
            self.assertEqual(stat.S_IMODE(pid_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(identity_file.stat().st_mode), 0o600)
            self.assertEqual(proc.stdin.writes, [b"GO\n"])
            self.assertTrue(proc.stdin.closed)
            self.assertFalse(proc.terminated)

    def test_failed_publication_removes_partial_state_and_reaps_launcher(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._configured_instance(root)
            proc = _Process()
            patches = self._start_patches(proc)
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                mock.patch.object(
                    glassagent,
                    "_publish_pid",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                glassagent.start(str(root))

            self.assertFalse((root / ".glass_agent.pid").exists())
            self.assertFalse((root / ".glass_agent.pid.meta.json").exists())
            self.assertTrue(proc.terminated)
            self.assertTrue(proc.stdin.closed)
            self.assertEqual(proc.waits, [2])

    @unittest.skipIf(os.name == "nt", "launch gate is POSIX-only")
    def test_launch_gate_exits_on_parent_eof_before_exec(self):
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "unexpected-agent-start"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    glassagent._LAUNCH_GATE,
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"Path({str(marker)!r}).write_text('started')",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertIsNotNone(proc.stdin)
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=5), 75)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
