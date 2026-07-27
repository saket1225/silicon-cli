from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ACTIVATOR_PATH = (
    ROOT / "docker" / "runtime" / "activate-interface-cli.py"
)
SPEC = importlib.util.spec_from_file_location(
    "silicon_runtime_interface_activation",
    ACTIVATOR_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load Interface activation helper")
ACTIVATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACTIVATOR)


class InterfaceActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / ".silicon-interface"
        self.state.mkdir()
        (self.state / "inbox.jsonl").write_text(
            '{"event":"preserved"}\n',
            encoding="utf-8",
        )
        (self.state / "state.json").write_text(
            '{"cursor":"preserved"}\n',
            encoding="utf-8",
        )
        legacy_package = self.state / "package"
        legacy_package.mkdir()
        (legacy_package / "legacy-marker").write_text(
            "preserved",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runtime(self, name: str, version: str) -> Path:
        executable = self.root / name
        executable.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'if [ "${1:-}" = "--version" ]; then\n'
            f"  printf '%s\\n' '{version}'\n"
            "  exit 0\n"
            "fi\n"
            'if [ "${1:-}" = "root" ]; then\n'
            '  printf \'%s\\n\' "${SILICON_INTERFACE_ROOT:-}"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _version(self, command: str = "si") -> str:
        return ACTIVATOR._version(
            self.state / "bin" / command,
            cwd=self.root,
        )

    def test_selected_image_controls_forward_and_backward_activation(self):
        selected_runtime = self._runtime(
            "selected-image-silicon-interface",
            "2.0.2",
        )

        first = ACTIVATOR.activate(self.root, selected_runtime)
        self.assertEqual(first["version"], "2.0.2")
        self.assertEqual(self._version("si"), "2.0.2")
        self.assertEqual(self._version("silicon-interface"), "2.0.2")
        root_result = subprocess.run(
            [str(self.state / "bin" / "si"), "root"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            Path(root_result.stdout.strip()).resolve(),
            self.root.resolve(),
        )

        # A newer fixed image exposes its package at the same absolute path.
        self._runtime(selected_runtime.name, "2.0.3")
        second = ACTIVATOR.activate(self.root, selected_runtime)
        self.assertEqual(second["version"], "2.0.3")
        self.assertEqual(self._version(), "2.0.3")

        # Selecting the older fixed image and running its boot activator moves
        # backward through the same atomic launcher.
        self._runtime(selected_runtime.name, "2.0.2")
        rolled_back = ACTIVATOR.activate(self.root, selected_runtime)
        self.assertEqual(rolled_back["version"], "2.0.2")
        self.assertEqual(self._version(), "2.0.2")

        # The immediately preceding pre-fix image does not run this helper,
        # but it exposes the same absolute global path. The already-persisted
        # launcher therefore follows that image back to 2.0.1 by construction.
        self._runtime(selected_runtime.name, "2.0.1")
        self.assertEqual(self._version(), "2.0.1")

        self.assertEqual(
            (self.state / "inbox.jsonl").read_text(encoding="utf-8"),
            '{"event":"preserved"}\n',
        )
        self.assertEqual(
            (self.state / "state.json").read_text(encoding="utf-8"),
            '{"cursor":"preserved"}\n',
        )
        self.assertEqual(
            (self.state / "package" / "legacy-marker").read_text(
                encoding="utf-8"
            ),
            "preserved",
        )

    def test_invalid_selected_runtime_does_not_replace_existing_launchers(self):
        selected_runtime = self._runtime("selected-runtime", "2.0.2")
        ACTIVATOR.activate(self.root, selected_runtime)
        si_before = (self.state / "bin" / "si").read_bytes()
        interface_before = (
            self.state / "bin" / "silicon-interface"
        ).read_bytes()
        invalid = self.root / "invalid-runtime"
        invalid.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        invalid.chmod(0o755)

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError,
            "invalid Silicon Interface CLI",
        ):
            ACTIVATOR.activate(self.root, invalid)

        self.assertEqual(
            (self.state / "bin" / "si").read_bytes(),
            si_before,
        )
        self.assertEqual(
            (self.state / "bin" / "silicon-interface").read_bytes(),
            interface_before,
        )
        self.assertEqual(self._version(), "2.0.2")

    def test_interrupted_secondary_switch_keeps_canonical_si_runnable(self):
        runtime_a = self._runtime("runtime-a", "2.0.2")
        runtime_b = self._runtime("runtime-b", "2.0.3")
        ACTIVATOR.activate(self.root, runtime_a)
        si_before = (self.state / "bin" / "si").read_bytes()
        normal_write = ACTIVATOR._atomic_write
        attempts = 0

        def fail_before_si(path, payload, *, mode):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("simulated interruption")
            normal_write(path, payload, mode=mode)

        with (
            mock.patch.object(
                ACTIVATOR,
                "_atomic_write",
                side_effect=fail_before_si,
            ),
            self.assertRaisesRegex(OSError, "simulated interruption"),
        ):
            ACTIVATOR.activate(self.root, runtime_b)

        self.assertEqual(
            (self.state / "bin" / "si").read_bytes(),
            si_before,
        )
        self.assertEqual(self._version("si"), "2.0.2")
        self.assertEqual(self._version("silicon-interface"), "2.0.3")

    def test_symlinked_activation_directory_is_rejected(self):
        real_bin = self.root / "redirected-bin"
        real_bin.mkdir()
        (self.state / "bin").symlink_to(real_bin, target_is_directory=True)
        selected_runtime = self._runtime("selected-runtime", "2.0.2")

        with self.assertRaisesRegex(
            ACTIVATOR.ActivationError,
            "unsafe Interface activation directory",
        ):
            ACTIVATOR.activate(self.root, selected_runtime)

        self.assertEqual(list(real_bin.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
