from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import docker_runtime, interface_cli, runtime_contract, sync


class RuntimeContractTests(unittest.TestCase):
    def test_docker_contract_covers_the_complete_external_toolchain(self):
        contract = runtime_contract.docker_contract()

        self.assertEqual(
            set(contract["commands"]),
            {
                "silicon",
                "silicon-browser",
                "silicon-extend",
                "silicon-interface",
                "si",
                "claude",
                "codex",
                "node",
                "npm",
                "python3",
                "git",
            },
        )
        self.assertEqual(
            set(contract["minimum_python_packages"]),
            {"silicon-cli", "silicon-browser", "silicon-extend"},
        )
        self.assertEqual(
            contract["exact_python_packages"],
            {"silicon-extend": "0.1.3"},
        )
        self.assertEqual(
            contract["minimum_command_versions"]["silicon-interface"],
            "2.0.2",
        )
        self.assertEqual(
            contract["minimum_command_versions"]["silicon-browser"],
            "1.1.1",
        )

    def test_docker_runtime_probe_uses_the_exact_signed_image(self):
        image = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "a" * 64
        )
        payload = {
            "failures": [],
            "versions": {"silicon-cli": "1.0.24", "node": "v22.0.0"},
        }
        with mock.patch.object(
            docker_runtime,
            "_run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload) + "\n",
                stderr="",
            ),
        ) as run:
            versions = docker_runtime.verify_runtime_contract(
                {"docker_sudo": False},
                image,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertEqual(
            command[command.index("--entrypoint") + 1],
            "/opt/silicon-runtime/bin/python",
        )
        self.assertIn(image, command)
        self.assertIn(runtime_contract.DOCKER_PROBE_SCRIPT, command)
        self.assertEqual(versions["silicon-cli"], "1.0.24")

    def test_docker_runtime_probe_fails_closed_on_outdated_dependency(self):
        image = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "b" * 64
        )
        payload = {
            "failures": ["codex: found 0.1.0, require 0.146.0 or newer"],
            "versions": {"codex": "0.1.0"},
        }
        with (
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps(payload) + "\n",
                    stderr="",
                ),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "codex: found 0.1.0",
            ),
        ):
            docker_runtime.verify_runtime_contract(
                {"docker_sudo": False},
                image,
            )

    def test_docker_runtime_inventory_reports_outdated_versions(self):
        payload = {
            "failures": ["codex is outdated"],
            "versions": {"codex": "0.1.0"},
        }
        with mock.patch.object(
            docker_runtime,
            "_run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout=json.dumps(payload) + "\n",
                stderr="",
            ),
        ):
            result = docker_runtime.inspect_runtime_contract(
                {"docker_sudo": False},
                "local-runtime:inventory",
            )

        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["versions"]["codex"], "0.1.0")

    def test_pull_verifies_docker_contract_before_login(self):
        image = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "c" * 64
        )
        prepared = SimpleNamespace(
            release=SimpleNamespace(
                manifest=SimpleNamespace(runtime_image=image)
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[str] = []
            with (
                mock.patch.object(
                    sync.docker_runtime,
                    "ensure_ready",
                    return_value={"root": str(root), "docker_sudo": False},
                ),
                mock.patch.object(
                    sync.docker_runtime,
                    "verify_runtime_contract",
                    side_effect=lambda *_args: events.append("verify"),
                ),
                mock.patch.object(
                    sync.docker_runtime,
                    "maybe_prompt_login",
                    side_effect=lambda *_args: events.append("login"),
                ),
            ):
                parent, selected = sync._prepare_runtime_for_release(
                    prepared,
                    use_docker=True,
                )

        self.assertEqual(events, ["verify", "login"])
        self.assertEqual(parent, root.resolve())
        self.assertEqual(selected, image)

    def test_local_pull_preflight_runs_before_returning_destination(self):
        prepared = SimpleNamespace()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(Path, "cwd", return_value=Path(temporary)),
            mock.patch.object(
                sync.runtime_contract,
                "verify_host_pull_runtime",
            ) as verify,
        ):
            parent, image = sync._prepare_runtime_for_release(
                prepared,
                use_docker=False,
            )

        verify.assert_called_once_with()
        self.assertEqual(parent, Path(temporary).resolve())
        self.assertEqual(image, "")

    def test_required_interface_setup_refuses_missing_node(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(interface_cli, "_node_major", return_value=None),
            self.assertRaisesRegex(RuntimeError, "node was not found"),
        ):
            interface_cli.setup(temporary, required=True)

    def test_interface_staging_install_disables_daemon(self):
        with mock.patch.object(
            interface_cli.shutil,
            "which",
            return_value="/usr/bin/npm",
        ):
            command = interface_cli._npm_install_command(
                Path("/tmp/silicon"),
                "@teamofsilicons/silicon-interface-cli",
                start_daemon=False,
            )

        self.assertIsNotNone(command)
        self.assertEqual(command[-1], "--no-daemon")


if __name__ == "__main__":
    unittest.main()
