from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
