import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli import config


class ExtendInstallationTest(unittest.TestCase):
    def test_runtime_environment_prefers_active_generation_bin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / ".silicon" / "releases" / "release-1"
            python = (
                root
                / ".silicon"
                / "environments"
                / "environment-1"
                / "bin"
                / "python"
            )
            with (
                mock.patch.object(
                    config,
                    "active_release_root",
                    return_value=release,
                ),
                mock.patch.object(
                    config,
                    "active_environment_python",
                    return_value=str(python),
                ),
                mock.patch.dict(
                    os.environ,
                    {"PATH": f"/usr/bin{os.pathsep}{python.parent}"},
                    clear=False,
                ),
            ):
                environment = config.runtime_environment(root)

        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(python.resolve().parent),
        )
        self.assertEqual(
            environment["PATH"].split(os.pathsep).count(
                str(python.resolve().parent)
            ),
            1,
        )
        self.assertEqual(environment["SILICON_RELEASE_ROOT"], str(release))

    def test_cli_declares_exact_extend_dependency(self):
        pyproject = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        ).read_text(encoding="utf-8")

        self.assertIn('"silicon-extend==0.1.4"', pyproject)

    def test_container_entrypoint_prefers_generation_environment_bin(self):
        entrypoint = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "runtime-entrypoint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'export PATH="$(dirname "$environment_python"):$PATH"',
            entrypoint,
        )
        self.assertIn(
            'export PATH="$SILICON_ROOT/.venv/bin:$PATH"',
            entrypoint,
        )
        self.assertIn(
            'metadata.version("silicon-extend")',
            entrypoint,
        )
        self.assertIn(
            'name="silicon-extend"',
            entrypoint,
        )

    def test_runtime_image_declares_required_extend_version(self):
        dockerfile = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'SILICON_EXTEND_REQUIRED_VERSION="0.1.4"',
            dockerfile,
        )


if __name__ == "__main__":
    unittest.main()
