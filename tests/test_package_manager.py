from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import interface_cli, package_manager, registry


def _install(name: str, runtime: str = "local") -> registry.Install:
    return registry.Install(
        index=0,
        name=name,
        path=f"/srv/{name}",
        pid_file=f"/srv/{name}/.silicon.pid",
        runtime=runtime,
        image="runtime@sha256:" + "a" * 64 if runtime == "docker" else "",
    )


class PackageManagerTests(unittest.TestCase):
    def test_interface_release_asset_metadata_is_pinned(self):
        spec = package_manager.PACKAGE_BY_KEY["silicon-interface"]

        self.assertEqual(package_manager.SILICON_INTERFACE_CLI_VERSION, "2.0.4")
        self.assertEqual(
            package_manager.SILICON_INTERFACE_CLI_RELEASE_URL,
            "https://github.com/teamofsilicons/silicon-interface-web/"
            "releases/download/interface-cli-v2.0.4/"
            "teamofsilicons-silicon-interface-cli-2.0.4.tgz",
        )
        self.assertEqual(
            package_manager.SILICON_INTERFACE_CLI_RELEASE_SHA256,
            "75c6c5439ef7f5d62635408f00ad9314"
            "999d397b844175e3dfcecbf822391073",
        )
        self.assertEqual(spec.latest_source, "embedded")

    def test_silicon_latest_version_comes_from_published_git_tags(self):
        with mock.patch.object(
            package_manager,
            "resolve_latest_published_git_release",
            return_value=SimpleNamespace(version="2.0.0"),
        ) as resolve, mock.patch.object(
            package_manager,
            "_http_json",
        ) as http:
            version, error = package_manager._latest_version(
                package_manager.PACKAGE_BY_KEY["silicon"]
            )

        self.assertEqual((version, error), ("2.0.0", ""))
        resolve.assert_called_once_with(package_manager.STEMCELL_GIT_URL)
        http.assert_not_called()

    def test_inventory_filters_instance_rows_but_keeps_server_host_rows(self):
        alpha = _install("alpha", "docker")
        beta = _install("beta")

        def identity(install):
            return {
                "silicon_id": f"sid-{install.name}",
                "team_slug": "team",
            }

        with (
            mock.patch.object(
                package_manager,
                "_latest_versions",
                return_value=({"codex": "2.0.0"}, []),
            ),
            mock.patch.object(
                package_manager,
                "_host_rows",
                return_value=[{"key": "codex", "location": "host", "status": "current"}],
            ),
            mock.patch.object(
                package_manager.registry,
                "installs",
                return_value=[alpha, beta],
            ),
            mock.patch.object(package_manager, "_glass_identity", side_effect=identity),
            mock.patch.object(
                package_manager,
                "_docker_rows",
                return_value=[{"key": "codex", "location": "docker-image", "status": "current"}],
            ) as docker_rows,
            mock.patch.object(package_manager, "_local_rows") as local_rows,
        ):
            result = package_manager.inventory(silicon_ids={"sid-alpha"})

        docker_rows.assert_called_once()
        local_rows.assert_not_called()
        self.assertEqual(result["installations"], 1)
        self.assertEqual(result["summary"]["total"], 2)

    def test_docker_package_update_rolls_published_runtime_and_host_copy(self):
        alpha = _install("alpha", "docker")
        with (
            mock.patch.object(
                package_manager,
                "_selected_installs",
                return_value=[alpha],
            ),
            mock.patch.object(package_manager.update, "update_instance") as update_instance,
            mock.patch.object(
                package_manager.registry,
                "installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager,
                "_update_host_package",
                return_value={"step": "host:codex", "status": "done"},
            ) as update_host,
            mock.patch.object(
                package_manager,
                "HostFileLock",
                return_value=nullcontext(),
            ),
        ):
            result = package_manager.update_package("codex")

        update_instance.assert_called_once_with("alpha")
        update_host.assert_called_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["steps"][0]["step"], "git-release")

    def test_running_local_silicon_blocks_shared_host_mutation(self):
        alpha = _install("alpha")
        with (
            mock.patch.object(
                package_manager,
                "_selected_installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.registry,
                "installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.process,
                "install_is_running",
                return_value=True,
            ),
            mock.patch.object(package_manager, "_update_host_package") as update_host,
            mock.patch.object(
                package_manager,
                "HostFileLock",
                return_value=nullcontext(),
            ),
        ):
            result = package_manager.update_package("codex")

        update_host.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["steps"][0]["status"], "blocked")

    def test_interface_package_update_installs_verified_release_asset(self):
        alpha = _install("alpha", "docker")
        payload = b"immutable Interface CLI 2.0.4 release asset"
        observed: dict[str, object] = {}

        def urlopen(request, **kwargs):
            observed["url"] = request.full_url
            return io.BytesIO(payload)

        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "lib" / "node_modules"
            script = (
                package_root
                / "@teamofsilicons"
                / "silicon-interface-cli"
                / "bin"
                / "silicon-interface.mjs"
            )
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env node\n", encoding="utf-8")

            def run(command, **kwargs):
                if command == ["/usr/bin/npm", "root", "-g"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{package_root}\n",
                        stderr="",
                    )
                if (
                    command[0] == "/usr/bin/node"
                    and Path(command[1]).resolve() == script.resolve()
                    and command[2:] == ["--version"]
                ):
                    return SimpleNamespace(
                        returncode=0,
                        stdout="2.0.4\n",
                        stderr="",
                    )
                observed["command"] = command
                observed["artifact"] = Path(command[-1]).read_bytes()
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def which(command):
                return {
                    "npm": "/usr/bin/npm",
                    "node": "/usr/bin/node",
                }.get(command)

            with (
                mock.patch.object(
                    package_manager,
                    "SILICON_INTERFACE_CLI_RELEASE_SHA256",
                    hashlib.sha256(payload).hexdigest(),
                ),
                mock.patch.object(
                    package_manager,
                    "_selected_installs",
                    return_value=[alpha],
                ),
                mock.patch.object(
                    package_manager.registry,
                    "installs",
                    return_value=[alpha],
                ),
                mock.patch.object(package_manager.update, "update_instance"),
                mock.patch.object(
                    package_manager,
                    "HostFileLock",
                    return_value=nullcontext(),
                ),
                mock.patch.object(
                    package_manager.shutil,
                    "which",
                    side_effect=which,
                ),
                mock.patch.object(
                    package_manager.urllib.request,
                    "urlopen",
                    side_effect=urlopen,
                ),
                mock.patch.object(
                    package_manager.subprocess,
                    "run",
                    side_effect=run,
                ),
            ):
                result = package_manager.update_package(
                    "silicon-interface"
                )

        command = observed["command"]
        self.assertIsInstance(command, list)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            observed["url"],
            package_manager.SILICON_INTERFACE_CLI_RELEASE_URL,
        )
        self.assertEqual(observed["artifact"], payload)
        self.assertEqual(
            command[:-1],
            [
                "/usr/bin/npm",
                "install",
                "-g",
                "--no-audit",
                "--no-fund",
            ],
        )
        self.assertTrue(command[-1].endswith("silicon-interface-cli-2.0.4.tgz"))
        self.assertNotIn("@latest", " ".join(command))
        self.assertNotIn(
            package_manager.PACKAGE_BY_KEY["silicon-interface"].package,
            command,
        )

    def test_interface_package_update_rejects_unverified_release_asset(self):
        payload = b"tampered Interface CLI release asset"
        with (
            mock.patch.object(
                package_manager.shutil,
                "which",
                return_value="/usr/bin/npm",
            ),
            mock.patch.object(
                package_manager.urllib.request,
                "urlopen",
                return_value=io.BytesIO(payload),
            ),
            mock.patch.object(package_manager.subprocess, "run") as run,
        ):
            result = package_manager._update_host_package(
                package_manager.PACKAGE_BY_KEY["silicon-interface"]
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("checksum mismatch", result["detail"])
        run.assert_not_called()

    def test_interface_update_reuses_verified_installer_for_local_copy(self):
        alpha = _install("alpha")
        verified = "/verified/lib/node_modules/@teamofsilicons/" \
            "silicon-interface-cli/bin/silicon-interface.mjs"
        with (
            mock.patch.object(
                package_manager,
                "_selected_installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.registry,
                "installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.process,
                "install_is_running",
                return_value=False,
            ),
            mock.patch.object(
                package_manager,
                "_update_host_package",
                return_value={
                    "step": "host:silicon-interface",
                    "status": "done",
                    "detail": "",
                    "_verified_source_script": verified,
                },
            ),
            mock.patch.object(
                package_manager.interface_cli,
                "setup",
            ) as setup,
            mock.patch.object(
                package_manager,
                "HostFileLock",
                return_value=nullcontext(),
            ),
        ):
            result = package_manager.update_package("silicon-interface")

        self.assertEqual(result["status"], "succeeded")
        setup.assert_called_once_with(
            alpha.path,
            required=True,
            force=True,
            source_script=verified,
        )

    def test_failed_verified_install_leaves_local_interface_intact(self):
        alpha = _install("alpha")
        with (
            mock.patch.object(
                package_manager,
                "_selected_installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.registry,
                "installs",
                return_value=[alpha],
            ),
            mock.patch.object(
                package_manager.process,
                "install_is_running",
                return_value=False,
            ),
            mock.patch.object(
                package_manager,
                "_update_host_package",
                return_value={
                    "step": "host:silicon-interface",
                    "status": "failed",
                    "detail": "checksum mismatch",
                },
            ),
            mock.patch.object(
                package_manager.interface_cli,
                "setup",
            ) as setup,
            mock.patch.object(
                package_manager,
                "HostFileLock",
                return_value=nullcontext(),
            ),
        ):
            result = package_manager.update_package("silicon-interface")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [step["status"] for step in result["steps"]],
            ["failed", "blocked"],
        )
        setup.assert_not_called()

    def test_json_inventory_has_a_stable_marker(self):
        payload = {"schema": 1, "packages": []}
        output = io.StringIO()
        with (
            mock.patch.object(package_manager, "inventory", return_value=payload),
            redirect_stdout(output),
        ):
            package_manager.package_command(["inventory", "--json"])

        line = output.getvalue().strip()
        self.assertTrue(line.startswith(package_manager.INVENTORY_MARKER))
        self.assertEqual(
            json.loads(line.removeprefix(package_manager.INVENTORY_MARKER)),
            payload,
        )

    def test_force_interface_setup_reinstalls_a_ready_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "silicon-interface.mjs"
            source.touch()
            with (
                mock.patch.object(interface_cli, "_node_major", return_value=22),
                mock.patch.object(interface_cli, "_installation_ready", return_value=True),
                mock.patch.object(interface_cli, "_stop_daemon") as stop,
                mock.patch.object(interface_cli, "_source_script", return_value=source),
                mock.patch.object(interface_cli, "_run", return_value=True) as run,
                mock.patch.object(
                    interface_cli.runtime_contract,
                    "verify_local_interface_install",
                ),
            ):
                ready = interface_cli.setup(
                    temporary,
                    required=True,
                    start_daemon=False,
                    force=True,
                )

        self.assertTrue(ready)
        stop.assert_called_once()
        run.assert_called_once()

    def test_force_interface_setup_fails_before_mutation_if_daemon_wont_stop(
        self,
    ):
        installer = Path("/verified/bin/silicon-interface.mjs")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(interface_cli, "_node_major", return_value=22),
                mock.patch.object(
                    interface_cli,
                    "_stop_daemon",
                    return_value=False,
                ) as stop,
                mock.patch.object(interface_cli, "_run") as run,
                self.assertRaisesRegex(
                    RuntimeError,
                    "could not be stopped safely",
                ),
            ):
                interface_cli.setup(
                    temporary,
                    required=True,
                    force=True,
                    source_script=installer,
                )

        stop.assert_called_once()
        run.assert_not_called()

    def test_interface_setup_can_reuse_a_verified_installer(self):
        installer = Path("/verified/bin/silicon-interface.mjs")
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(interface_cli, "_node_major", return_value=22),
                mock.patch.object(
                    interface_cli.shutil,
                    "which",
                    return_value="/usr/bin/node",
                ),
                mock.patch.object(interface_cli, "_stop_daemon"),
                mock.patch.object(interface_cli, "_source_script") as source,
                mock.patch.object(
                    interface_cli,
                    "_run",
                    return_value=True,
                ) as run,
                mock.patch.object(
                    interface_cli.runtime_contract,
                    "verify_local_interface_install",
                ),
            ):
                ready = interface_cli.setup(
                    temporary,
                    required=True,
                    start_daemon=False,
                    force=True,
                    source_script=installer,
                )

        self.assertTrue(ready)
        source.assert_not_called()
        run.assert_called_once_with(
            [
                "/usr/bin/node",
                str(installer),
                "install",
                str(Path(temporary).resolve()),
                "--no-daemon",
            ],
            Path(temporary).resolve(),
        )


if __name__ == "__main__":
    unittest.main()
