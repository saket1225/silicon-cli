from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import config, docker_runtime, process, registry, ui
from silicon_cli.updater.cache import runtime_platform_identity


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_registry_dir = registry.REGISTRY_DIR
        self.old_registry_file = registry.REGISTRY_FILE
        self.old_config_file = docker_runtime.CONFIG_FILE
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "SILICON_CONTAINER_MODE",
                "SILICON_RUNTIME",
                "SILICON_RUNTIME_DOCKER",
                "SILICON_DOCKER_ROOT",
                "SILICON_DOCKER_COMPOSE",
                "SILICON_DOCKER_SHARED_HOME",
                "SILICON_RUNTIME_IMAGE",
                "SILICON_DOCKER_SUDO",
                "SILICON_DOCKER_AUTO_INSTALL",
                "SILICON_DOCKER_ALLOW_UNPINNED_IMAGE",
            )
        }
        for key in self.old_env:
            os.environ.pop(key, None)
        # Most legacy command-shape tests use a short local tag. Production
        # defaults remain fail-closed; dedicated trust tests below clear this.
        os.environ["SILICON_DOCKER_ALLOW_UNPINNED_IMAGE"] = "1"
        registry.REGISTRY_DIR = self.root / ".silicon"
        registry.REGISTRY_FILE = registry.REGISTRY_DIR / "registry.json"
        docker_runtime.CONFIG_FILE = registry.REGISTRY_DIR / "docker.json"

    def tearDown(self):
        registry.REGISTRY_DIR = self.old_registry_dir
        registry.REGISTRY_FILE = self.old_registry_file
        docker_runtime.CONFIG_FILE = self.old_config_file
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_docker_config(self, image: str = "example/silicon:latest") -> dict:
        cfg = {
            "enabled": True,
            "root": str(self.root / "silicons"),
            "compose_file": str(self.root / "silicons" / "compose.yml"),
            "shared_home": str(self.root / "silicons" / ".shared-home"),
            "image": image,
        }
        docker_runtime.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        docker_runtime.CONFIG_FILE.write_text(json.dumps(cfg))
        return cfg

    def write_generation_pointer(
        self,
        instance: Path,
        *,
        generation_id: str = "generation-1",
        environment_path: str = "",
    ) -> None:
        tree = "a" * 64
        (instance / ".silicon" / "current.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "immutable-release",
                    "generation_id": generation_id,
                    "release_path": (
                        f".silicon/releases/{generation_id}"
                    ),
                    "upstream_tree_sha256": tree,
                    "materialized_tree_sha256": tree,
                    "environment_path": environment_path,
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

    def test_default_runtime_image_uses_published_registry(self):
        cfg = docker_runtime.load_config()

        self.assertEqual(cfg["image"], "")

    def test_generation_pointer_symlinks_never_downgrade_to_flat_code(self):
        instance = self.root / "silicons" / "ada"
        state = instance / ".silicon"
        state.mkdir(parents=True)
        (instance / "main.py").write_text("print('stale flat code')\n")
        pointer = state / "current.json"
        external = self.root / "external-pointer.json"
        external.write_text("{}\n")
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
        )

        for target in (external, self.root / "missing-pointer.json"):
            try:
                pointer.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(
                RuntimeError,
                "invalid Silicon generation pointer",
            ):
                config.active_release_root(instance)
            with self.assertRaisesRegex(
                RuntimeError,
                "invalid Silicon generation environment",
            ):
                config.active_environment_python(instance)
            with self.assertRaisesRegex(
                RuntimeError,
                "invalid active generation",
            ):
                docker_runtime.active_generation_runtime_image(inst)
            with self.assertRaisesRegex(
                RuntimeError,
                "invalid Silicon generation pointer",
            ):
                docker_runtime.maintenance_coordinator_available(inst)
            pointer.unlink()

    def test_unpinned_runtime_image_fails_closed_without_dev_opt_in(self):
        os.environ.pop("SILICON_DOCKER_ALLOW_UNPINNED_IMAGE", None)
        with self.assertRaisesRegex(RuntimeError, "immutable runtime image"):
            docker_runtime._require_runtime_image(
                "ghcr.io/teamofsilicons/silicon-runtime:latest",
                context="test runtime",
            )

    def test_environment_cannot_retarget_a_persisted_runtime_digest(self):
        persisted = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "a" * 64
        )
        override = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "b" * 64
        )
        self.write_docker_config(image=persisted)
        os.environ["SILICON_RUNTIME_IMAGE"] = override

        self.assertEqual(docker_runtime.load_config()["image"], persisted)

    def test_environment_digest_is_bootstrap_only_when_config_is_unbound(self):
        bootstrap = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "c" * 64
        )
        self.write_docker_config(image="")
        os.environ["SILICON_RUNTIME_IMAGE"] = bootstrap

        self.assertEqual(docker_runtime.load_config()["image"], bootstrap)

    def test_bind_release_runtime_persists_exact_digest_after_verified_pull(self):
        os.environ.pop("SILICON_DOCKER_ALLOW_UNPINNED_IMAGE", None)
        cfg = self.write_docker_config(image="")
        image = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "a" * 64
        )
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        registry.register(
            "ada",
            str(instance),
            runtime="docker",
            service="silicon-ada",
            compose_file=cfg["compose_file"],
            image="",
            container_name="silicon-ada",
        )
        inspected = [
            SimpleNamespace(returncode=1, stdout="", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps([image]),
                stderr="",
            ),
        ]
        with (
            mock.patch.object(docker_runtime, "_cmd", side_effect=inspected),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            bound = docker_runtime.bind_release_runtime(
                image,
                installs=[registry.find("ada")],
            )

        self.assertEqual(bound["image"], image)
        self.assertEqual(
            json.loads(docker_runtime.CONFIG_FILE.read_text())["image"],
            "",
        )
        self.assertEqual(registry.find("ada").image, image)
        self.assertIn(image, Path(cfg["compose_file"]).read_text())
        self.assertEqual(run.call_args.args[0], ["docker", "pull", image])

    def test_target_runtime_binding_does_not_switch_other_silicons(self):
        os.environ.pop("SILICON_DOCKER_ALLOW_UNPINNED_IMAGE", None)
        prior = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "1" * 64
        )
        candidate = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "2" * 64
        )
        cfg = self.write_docker_config(image=prior)
        for name in ("ada", "grace"):
            path = self.root / "silicons" / name
            path.mkdir(parents=True)
            registry.register(
                name,
                str(path),
                runtime="docker",
                service=f"silicon-{name}",
                compose_file=cfg["compose_file"],
                image=prior,
                container_name=f"silicon-{name}",
            )

        with mock.patch.object(
            docker_runtime,
            "prepare_release_image",
            return_value={**cfg, "image": candidate},
        ):
            docker_runtime.bind_release_runtime(
                candidate,
                installs=[registry.find("ada")],
            )

        self.assertEqual(registry.find("ada").image, candidate)
        self.assertEqual(registry.find("grace").image, prior)
        self.assertEqual(
            json.loads(docker_runtime.CONFIG_FILE.read_text())["image"],
            prior,
        )
        compose = Path(cfg["compose_file"]).read_text()
        self.assertIn(f'image: "{candidate}"', compose)
        self.assertIn(f'image: "{prior}"', compose)

    def test_target_runtime_binding_preserves_unrelated_legacy_tag(self):
        os.environ.pop("SILICON_DOCKER_ALLOW_UNPINNED_IMAGE", None)
        prior = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "1" * 64
        )
        candidate = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "2" * 64
        )
        legacy = "ghcr.io/teamofsilicons/silicon-runtime:latest"
        cfg = self.write_docker_config(image=prior)
        for name, image in (("ada", prior), ("grace", legacy)):
            path = self.root / "silicons" / name
            path.mkdir(parents=True)
            registry.register(
                name,
                str(path),
                runtime="docker",
                service=f"silicon-{name}",
                compose_file=cfg["compose_file"],
                image=image,
                container_name=f"silicon-{name}",
            )

        with mock.patch.object(
            docker_runtime,
            "prepare_release_image",
            return_value={**cfg, "image": candidate},
        ):
            docker_runtime.bind_release_runtime(
                candidate,
                installs=[registry.find("ada")],
            )

        self.assertEqual(registry.find("ada").image, candidate)
        self.assertEqual(registry.find("grace").image, legacy)
        compose = Path(cfg["compose_file"]).read_text()
        self.assertIn(f'image: "{candidate}"', compose)
        self.assertIn(f'image: "{legacy}"', compose)

    def test_digest_pull_fails_if_daemon_cannot_verify_repo_digest(self):
        os.environ.pop("SILICON_DOCKER_ALLOW_UNPINNED_IMAGE", None)
        cfg = self.write_docker_config(
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "b" * 64
        )
        with (
            mock.patch.object(
                docker_runtime,
                "_cmd",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(["ghcr.io/other/image@sha256:" + "b" * 64]),
                    stderr="",
                ),
            ),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ),
            self.assertRaisesRegex(RuntimeError, "pull and verify"),
        ):
            docker_runtime._ensure_image(cfg)

    def test_legacy_registry_rows_load_as_local(self):
        registry.REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        registry.REGISTRY_FILE.write_text(json.dumps({
            "installations": [
                {"name": "ada", "path": "/tmp/ada", "pid_file": "/tmp/ada/.silicon.pid"}
            ]
        }))

        [inst] = registry.installs()

        self.assertEqual(inst.name, "ada")
        self.assertEqual(inst.runtime, "local")
        self.assertFalse(inst.is_docker)

    def test_register_instance_writes_compose_and_metadata(self):
        cfg = self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)

        inst = docker_runtime.register_instance("ada", instance)

        self.assertTrue(inst.is_docker)
        self.assertEqual(inst.service, "silicon-ada")
        self.assertEqual(inst.container_name, "silicon-ada")
        compose = Path(cfg["compose_file"]).read_text()
        self.assertIn("services:", compose)
        self.assertIn("silicon-ada:", compose)
        self.assertIn(f'{instance.resolve()}:/silicon', compose)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', compose)
        self.assertIn("SILICON_SHARED_HOME", compose)
        self.assertIn("example/silicon:latest", compose)
        self.assertIn("healthcheck:", compose)
        self.assertIn("silicon-runtime-healthcheck.py", compose)
        self.assertIn("start_period: 120s", compose)

    def test_enabled_is_false_inside_container(self):
        self.write_docker_config()
        os.environ["SILICON_CONTAINER_MODE"] = "1"

        self.assertFalse(docker_runtime.enabled())

    def test_maintenance_run_uses_runtime_entrypoint(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            docker_runtime.maintenance_silicon(inst, ["status", "ada"])
        finally:
            docker_runtime._run = old_run

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("--entrypoint", cmd)
        self.assertIn("/usr/local/bin/silicon-runtime-entrypoint", cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertIn(f'{Path(self.root / "silicons" / ".shared-home").resolve()}:/silicon-shared-home', cmd)
        self.assertEqual(cmd[-4:], ["run", "silicon", "status", "ada"])

    def test_maintenance_run_uses_sudo_docker_when_configured(self):
        self.write_docker_config()
        data = json.loads(docker_runtime.CONFIG_FILE.read_text())
        data["docker_sudo"] = True
        docker_runtime.CONFIG_FILE.write_text(json.dumps(data))
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            docker_runtime.maintenance_silicon(inst, ["status", "ada"])
        finally:
            docker_runtime._run = old_run

        self.assertEqual(captured["cmd"][:2], ["sudo", "docker"])

    def test_exec_args_use_container_runtime_home(self):
        self.write_docker_config()
        inst = registry.Install(
            0,
            "ada",
            str(self.root / "silicons" / "ada"),
            str(self.root / "silicons" / "ada" / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )

        cmd = docker_runtime._exec_args(inst, ["silicon", "start", "ada"])

        self.assertEqual(cmd[:2], ["docker", "exec"])
        self.assertIn("-w", cmd)
        self.assertIn("/silicon", cmd)
        self.assertIn("HOME=/silicon/.home", cmd)
        self.assertIn("SILICON_HOME=/silicon/.home/.silicon", cmd)
        self.assertIn("SILICON_BROWSER_HOME=/silicon/.silicon-browser", cmd)
        self.assertIn("SILICON_CONTAINER_MODE=1", cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertEqual(cmd[-4:], ["silicon-ada", "silicon", "start", "ada"])

    def test_local_dockerfile_installs_and_checks_wheel_dependencies(self):
        dockerfile = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "Dockerfile.local"
        ).read_text(encoding="utf-8")

        self.assertNotIn("--no-deps", dockerfile)
        self.assertIn(
            "COPY --from=silicon_extend_wheel",
            dockerfile,
        )
        self.assertIn("/tmp/silicon-extend-wheel/*.whl", dockerfile)
        self.assertIn("/opt/silicon-runtime/bin/pip check", dockerfile)

    def test_production_dockerfile_installs_exact_extend_release(self):
        dockerfile = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ARG SILICON_EXTEND_SPEC=silicon-extend==0.1.3",
            dockerfile,
        )
        self.assertIn('"${SILICON_EXTEND_SPEC}"', dockerfile)
        self.assertIn("/opt/silicon-runtime/bin/pip check", dockerfile)

    def test_production_runtime_build_inputs_are_immutable(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "docker" / "runtime" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        for exact_input in (
            "# syntax=docker/dockerfile:1.24.0@sha256:"
            "87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89",
            "FROM node:22-bookworm-slim@sha256:"
            "6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3",
            "ARG SILICON_CLI_SPEC=silicon-cli==1.0.27",
            "ARG SILICON_BROWSER_SPEC=silicon-browser==1.1.1",
            "ARG SILICON_EXTEND_SPEC=silicon-extend==0.1.3",
            "ARG SILICON_INTERFACE_CLI_URL="
            "https://github.com/teamofsilicons/silicon-interface-web/releases/"
            "download/interface-cli-v2.0.4/"
            "teamofsilicons-silicon-interface-cli-2.0.4.tgz",
            "ARG SILICON_INTERFACE_CLI_SHA256="
            "75c6c5439ef7f5d62635408f00ad9314999d397b844175e3dfcecbf822391073",
            "ARG CLAUDE_CODE_SPEC=@anthropic-ai/claude-code@2.1.220",
            "ARG CODEX_SPEC=@openai/codex@0.146.0",
            "ARG PIP_SPEC=pip==26.2",
            "ARG SETUPTOOLS_SPEC=setuptools==83.0.0",
            "ARG WHEEL_SPEC=wheel==0.47.0",
        ):
            self.assertIn(exact_input, dockerfile)

        workflow = (
            root / ".github" / "workflows" / "publish-runtime.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("uses: actions/checkout@v4", workflow)
        self.assertNotIn("uses: docker/setup-qemu-action@v3", workflow)
        self.assertNotIn("uses: docker/setup-buildx-action@v3", workflow)
        self.assertNotIn("uses: docker/login-action@v3", workflow)
        self.assertNotIn("uses: docker/build-push-action@v6", workflow)
        self.assertIn(
            '"${IMAGE_NAME}@${{ steps.build.outputs.digest }}"',
            workflow,
        )

    def test_runtime_entrypoint_atomically_activates_persisted_interface_cli(self):
        root = Path(__file__).resolve().parents[1]
        entrypoint = (
            root
            / "docker"
            / "runtime"
            / "runtime-entrypoint.sh"
        ).read_text()
        dockerfile = (
            root / "docker" / "runtime" / "Dockerfile"
        ).read_text()

        self.assertIn(
            'global_interface="/usr/local/bin/silicon-interface"',
            entrypoint,
        )
        self.assertIn(
            "/usr/local/libexec/silicon-activate-interface-cli.py",
            entrypoint,
        )
        self.assertIn(
            '--executable "$global_interface"',
            entrypoint,
        )
        self.assertIn(
            'die "Silicon Interface shim does not match the runtime image"',
            entrypoint,
        )
        self.assertNotIn(" daemon start", entrypoint)
        self.assertIn(
            "SILICON_INTERFACE_RESET_DAEMON_PID=1 "
            'silicon start "$INSTANCE_NAME"',
            entrypoint,
        )
        self.assertIn(
            'state / "interface-daemon-reset-required"',
            entrypoint,
        )
        self.assertIn(
            "COPY docker/runtime/activate-interface-cli.py "
            "/usr/local/libexec/silicon-activate-interface-cli.py",
            dockerfile,
        )

    def test_silicon_health_requires_live_generation_child_metadata(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        now = time.time()
        status = {
            "supervisor_pid": 10,
            "child_pid": 11,
            "generation": "/silicon/.silicon/releases/generation-1",
            "started_at": now - 8,
            "uptime_seconds": 8.0,
            "application_ready": True,
            "ready_at": now - 7,
            "heartbeat_at": now,
        }

        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(status) + "\n",
                    stderr="",
                ),
            ) as run,
        ):
            self.assertTrue(docker_runtime.silicon_running(inst))
            self.assertTrue(
                docker_runtime.silicon_healthy(inst, min_uptime=5.0)
            )
            self.assertTrue(
                docker_runtime.silicon_ready(
                    inst, min_uptime=5.0, max_heartbeat_age=5.0
                )
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["docker", "exec"])
        self.assertIn(".silicon.pid.meta.json", command[-1])
        self.assertIn("child_pid", command[-1])
        self.assertIn("stale generation", command[-1])
        self.assertIn("runtime-health.json", command[-1])

    @unittest.skipIf(
        os.name == "nt",
        "the embedded Docker probe runs in a Linux PID namespace",
    )
    def test_container_main_liveness_rejects_reused_pid_birth_identity(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        process_id = os.getpid()
        identity = process._process_identity(process_id)
        self.assertTrue(identity)
        (instance / ".silicon.pid").write_text(f"{process_id}\n")
        metadata = {
            "schema": 1,
            "supervisor_pid": process_id,
            "supervisor_identity": identity,
            "child_pid": process_id,
            "child_identity": identity,
            "started_at": time.time() - 10,
            "generation": str(instance),
        }
        metadata_path = instance / ".silicon.pid.meta.json"
        metadata_path.write_text(json.dumps(metadata))

        def execute_script(command, *, check=False, capture=False):
            script = command[-1].replace(
                'Path("/silicon")',
                f"Path({str(instance)!r})",
                1,
            )
            return subprocess.run(
                [sys.executable, "-c", script],
                check=check,
                capture_output=True,
                text=True,
            )

        with (
            mock.patch.object(
                docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "_run", side_effect=execute_script
            ),
        ):
            self.assertIsNotNone(docker_runtime.silicon_child_status(inst))
            metadata["supervisor_identity"] = "linux:reused-pid:1"
            metadata_path.write_text(json.dumps(metadata))
            self.assertIsNone(docker_runtime.silicon_child_status(inst))
            metadata["supervisor_identity"] = identity
            metadata["child_identity"] = "linux:reused-pid:1"
            metadata_path.write_text(json.dumps(metadata))
            self.assertIsNone(docker_runtime.silicon_child_status(inst))

    @unittest.skipIf(
        os.name == "nt",
        "the embedded Docker probe runs in a Linux PID namespace",
    )
    def test_container_glass_liveness_rejects_reused_pid_birth_identity(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        process_id = os.getpid()
        identity = process._process_identity(process_id)
        self.assertTrue(identity)
        (instance / ".glass_agent.pid").write_text(f"{process_id}\n")
        metadata = {
            "schema": 1,
            "pid": process_id,
            "identity": identity,
        }
        metadata_path = instance / ".glass_agent.pid.meta.json"
        metadata_path.write_text(json.dumps(metadata))

        def execute_script(command, *, check=False, capture=False):
            script = command[-1].replace(
                'Path("/silicon")',
                f"Path({str(instance)!r})",
                1,
            )
            return subprocess.run(
                [sys.executable, "-c", script],
                check=check,
                capture_output=True,
                text=True,
            )

        with (
            mock.patch.object(
                docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "_run", side_effect=execute_script
            ),
        ):
            self.assertTrue(docker_runtime.glass_agent_running(inst))
            metadata["identity"] = "linux:reused-pid:1"
            metadata_path.write_text(json.dumps(metadata))
            self.assertFalse(docker_runtime.glass_agent_running(inst))

    def test_silicon_health_rejects_supervisor_only_or_unstable_runtime(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )

        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
            ),
        ):
            self.assertFalse(docker_runtime.silicon_running(inst))

        status = {
            "supervisor_pid": 10,
            "child_pid": 11,
            "generation": "/silicon",
            "started_at": 100.0,
            "uptime_seconds": 1.0,
        }
        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(status) + "\n",
                    stderr="",
                ),
            ),
        ):
            self.assertTrue(docker_runtime.silicon_running(inst))
            self.assertFalse(
                docker_runtime.silicon_healthy(inst, min_uptime=5.0)
            )

        status["uptime_seconds"] = 10.0
        status["application_ready"] = True
        status["ready_at"] = time.time() - 10
        status["heartbeat_at"] = time.time() - 30
        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(status) + "\n",
                    stderr="",
                ),
            ),
        ):
            self.assertFalse(
                docker_runtime.silicon_ready(
                    inst, min_uptime=5.0, max_heartbeat_age=5.0
                )
            )

    def test_legacy_supervisor_is_conservatively_running_for_safe_drain(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                side_effect=[
                    SimpleNamespace(returncode=1, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ],
            ),
        ):
            self.assertTrue(docker_runtime.silicon_running(inst))

        (instance / ".silicon.pid.meta.json").write_text("{}")
        with (
            mock.patch.object(docker_runtime, "container_running", return_value=True),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
            ),
        ):
            self.assertFalse(docker_runtime.silicon_running(inst))

    def test_active_python_uses_portable_generation_and_data_root(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        release = instance / ".silicon" / "releases" / "generation-1"
        release.mkdir(parents=True)
        (release / "main.py").write_text("print('ok')\n")
        (instance / ".venv" / "bin").mkdir(parents=True)
        (instance / ".venv" / "bin" / "python").write_text("")
        self.write_generation_pointer(instance)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        captured = {}
        old_run = docker_runtime._run
        old_container_running = docker_runtime.container_running

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            captured["capture"] = capture
            return SimpleNamespace(returncode=0, stdout="{}\n", stderr="")

        docker_runtime._run = fake_run
        docker_runtime.container_running = lambda _inst: True
        try:
            docker_runtime.run_active_python(
                inst,
                ["-m", "core.maintenance", "--root", "/silicon", "status"],
            )
        finally:
            docker_runtime._run = old_run
            docker_runtime.container_running = old_container_running

        cmd = captured["cmd"]
        self.assertIn("/silicon/.silicon/releases/generation-1", cmd)
        self.assertIn("SILICON_DATA_ROOT=/silicon", cmd)
        self.assertIn(
            "SILICON_RELEASE_ROOT=/silicon/.silicon/releases/generation-1",
            cmd,
        )
        self.assertEqual(
            cmd[-6:],
            [
                "/silicon/.venv/bin/python",
                "-m",
                "core.maintenance",
                "--root",
                "/silicon",
                "status",
            ],
        )
        self.assertTrue(captured["capture"])

    def test_active_python_uses_generation_environment_and_ephemeral_container(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        release = instance / ".silicon" / "releases" / "generation-1"
        environment = instance / ".silicon" / "environments" / "env-1"
        (environment / "bin").mkdir(parents=True)
        (environment / "bin" / "python").write_text("")
        release.mkdir(parents=True)
        (release / "main.py").write_text("print('ok')\n")
        lock = release / "requirements.lock"
        lock.write_text("certifi==1 --hash=sha256:" + "a" * 64 + "\n")
        (
            environment / ".silicon-environment.json"
        ).write_text(
            json.dumps(
                {
                    "requirements_sha256": hashlib.sha256(
                        lock.read_bytes()
                    ).hexdigest(),
                    "requirements_file": "requirements.lock",
                    "require_hashes": True,
                    "runtime": runtime_platform_identity(),
                }
            )
        )
        self.write_generation_pointer(
            instance,
            environment_path=".silicon/environments/env-1",
        )
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )

        with (
            mock.patch.object(docker_runtime, "container_running", return_value=False),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=0, stdout='{"phase":"available"}\n', stderr=""
                ),
            ) as run,
        ):
            docker_runtime.run_active_python(
                inst,
                ["-m", "core.maintenance", "--root", "/silicon", "status"],
            )

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        entrypoint = cmd.index("--entrypoint")
        self.assertEqual(
            cmd[entrypoint + 1],
            "/silicon/.silicon/environments/env-1/bin/python",
        )
        self.assertNotIn("silicon-runtime-entrypoint", " ".join(cmd))
        self.assertIn("SILICON_DATA_ROOT=/silicon", cmd)
        self.assertEqual(
            cmd[-5:],
            ["-m", "core.maintenance", "--root", "/silicon", "status"],
        )

    def test_verify_silicon_extend_checks_active_runtime_package_and_command(self):
        inst = registry.Install(
            0,
            "ada",
            str(self.root / "silicons" / "ada"),
            str(self.root / "silicons" / "ada" / ".silicon.pid"),
            "docker",
        )
        with mock.patch.object(
            docker_runtime,
            "run_active_python",
            return_value=SimpleNamespace(returncode=0),
        ) as active_python:
            docker_runtime.verify_silicon_extend(inst)

        active_python.assert_called_once()
        arguments = active_python.call_args.args[1]
        self.assertEqual(arguments[:2], ["-I", "-c"])
        self.assertEqual(
            arguments[-1],
            docker_runtime.SILICON_EXTEND_VERSION,
        )
        self.assertIn('metadata.version("silicon-extend")', arguments[2])
        self.assertIn('name="silicon-extend"', arguments[2])
        self.assertTrue(active_python.call_args.kwargs["capture"])

    def test_verify_silicon_extend_fails_closed_for_incomplete_runtime(self):
        inst = registry.Install(
            0,
            "ada",
            str(self.root / "silicons" / "ada"),
            str(self.root / "silicons" / "ada" / ".silicon.pid"),
            "docker",
        )
        with (
            mock.patch.object(
                docker_runtime,
                "run_active_python",
                return_value=SimpleNamespace(returncode=1),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                r"required Silicon Extend 0\.1\.3 runtime",
            ),
        ):
            docker_runtime.verify_silicon_extend(inst)

    def test_prepare_environment_returns_confined_host_path(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        release = instance / ".silicon" / "releases" / "generation-1"
        release.mkdir(parents=True)
        (release / "requirements.lock").write_text(
            "certifi==2026.1.4 --hash=sha256:" + "a" * 64 + "\n"
        )
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        expected = instance / ".silicon" / "environments" / "abc-py313-linux"

        with mock.patch.object(
            docker_runtime,
            "_ephemeral_command",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"environment_path":'
                    '"/silicon/.silicon/environments/abc-py313-linux"}\n'
                ),
                stderr="",
            ),
        ) as command:
            actual = docker_runtime.prepare_environment(inst, release)

        self.assertEqual(actual, expected.resolve())
        args = command.call_args.args
        self.assertEqual(args[0], inst)
        self.assertEqual(args[1][:2], ["python3", "-c"])
        self.assertEqual(
            args[1][-1], "/silicon/.silicon/releases/generation-1"
        )
        self.assertIn(
            '[sys.executable, "-m", "venv", "--copies", str(temporary)]',
            args[1][2],
        )
        self.assertTrue(command.call_args.kwargs["capture"])

    def test_prepare_environment_requires_hash_pinned_lockfile(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        release = instance / ".silicon" / "releases" / "generation-1"
        release.mkdir(parents=True)
        (release / "requirements.txt").write_text("certifi\n")
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )

        with self.assertRaisesRegex(RuntimeError, "requirements.lock"):
            docker_runtime.prepare_environment(inst, release)

    def test_prepare_environment_rejects_container_path_escape(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        release = instance / ".silicon" / "releases" / "generation-1"
        release.mkdir(parents=True)
        (release / "requirements.lock").write_text("")
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )

        with (
            mock.patch.object(
                docker_runtime,
                "_ephemeral_command",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout='{"environment_path":"/silicon/../etc"}\n',
                    stderr="",
                ),
            ),
            self.assertRaises(RuntimeError),
        ):
            docker_runtime.prepare_environment(inst, release)

    def test_runtime_entrypoint_term_handler_stops_and_exits(self):
        entrypoint = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "runtime-entrypoint.sh"
        )
        subprocess.run(["bash", "-n", str(entrypoint)], check=True)
        text = entrypoint.read_text()
        self.assertIn("terminate_runtime()", text)
        self.assertIn("trap terminate_runtime TERM INT", text)
        handler = text.split("terminate_runtime()", 1)[1].split("}", 1)[0]
        self.assertIn("stop_runtime", handler)
        self.assertIn("exit 0", handler)

    @unittest.skipIf(os.name == "nt", "Unix sockets are required by the runtime")
    def test_image_healthcheck_requires_fresh_main_and_daemon_rpc(self):
        root = self.root / "health-instance"
        state = root / ".silicon"
        release = state / "releases" / "generation-1"
        interface_state = root / ".silicon-interface"
        release.mkdir(parents=True)
        interface_state.mkdir(parents=True)
        (release / "main.py").write_text("", encoding="utf-8")
        (state / "current.json").write_text(
            json.dumps(
                {
                    "kind": "immutable-release",
                    "release_path": ".silicon/releases/generation-1",
                }
            ),
            encoding="utf-8",
        )
        process_id = os.getpid()
        (root / ".silicon.pid").write_text(f"{process_id}\n", encoding="utf-8")
        (root / ".silicon.pid.meta.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "supervisor_pid": process_id,
                    "child_pid": process_id,
                    "generation": str(release.resolve()),
                }
            ),
            encoding="utf-8",
        )
        now = time.time()
        health_file = state / "runtime-health.json"
        health_file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "pid": process_id,
                    "code_root": str(release.resolve()),
                    "ready": True,
                    "ready_at": now - 1,
                    "heartbeat_at": now,
                }
            ),
            encoding="utf-8",
        )
        (interface_state / "daemon.pid").write_text(
            f"{process_id}\n",
            encoding="utf-8",
        )
        rpc_path = Path("/tmp") / (
            "silicon-health-"
            + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
            + ".sock"
        )
        (interface_state / "daemon-rpc.json").write_text(
            json.dumps({"version": 1, "socket": str(rpc_path)}),
            encoding="utf-8",
        )
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(rpc_path))
        server.listen(2)

        def serve():
            for _ in range(2):
                connection, _address = server.accept()
                with connection:
                    request = b""
                    while b"\n" not in request:
                        request += connection.recv(4096)
                    value = json.loads(request.split(b"\n", 1)[0])
                    connection.sendall(
                        (
                            json.dumps(
                                {
                                    "version": 1,
                                    "id": value["id"],
                                    "ok": True,
                                    "result": {
                                        "running": True,
                                        "pid": process_id,
                                    },
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        script = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "runtime-healthcheck.py"
        )
        spec = importlib.util.spec_from_file_location(
            "runtime_healthcheck_test",
            script,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ROOT = root.resolve()

        self.assertEqual(module.main(), 0)
        stale = json.loads(health_file.read_text(encoding="utf-8"))
        stale["heartbeat_at"] = time.time() - 120
        health_file.write_text(json.dumps(stale), encoding="utf-8")
        self.assertEqual(module.main(), 1)
        thread.join(1)
        rpc_path.unlink(missing_ok=True)

    def test_runtime_entrypoint_validates_the_resolved_generation(self):
        entrypoint = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "runtime-entrypoint.sh"
        )
        text = entrypoint.read_text()
        self.assertNotIn('[ ! -f "$SILICON_ROOT/main.py" ]', text)
        resolved = text.index('release_root="${runtime_paths[0]}"')
        validated = text.index('[ ! -f "$release_root/main.py" ]')
        self.assertGreater(validated, resolved)
        self.assertIn(
            '"$runtime_python" - "$INSTANCE_NAME" "$SILICON_ROOT"',
            text,
        )

    def test_runtime_entrypoint_refuses_unowned_legacy_update_fence(self):
        entrypoint = (
            Path(__file__).resolve().parents[1]
            / "docker"
            / "runtime"
            / "runtime-entrypoint.sh"
        )
        instance = self.root / "silicons" / "ada"
        marker = (
            instance
            / ".silicon"
            / "maintenance"
            / "legacy-offline.json"
        )
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "update_id": "tx-1",
                    "created_at": 123.5,
                }
            )
        )
        environment = {
            **os.environ,
            "SILICON_ROOT": str(instance),
            "SILICON_INSTANCE_NAME": "ada",
            "SILICON_SHARED_HOME": str(self.root / "shared-home"),
        }

        refused = subprocess.run(
            ["bash", str(entrypoint)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(
            "legacy offline update is in progress",
            refused.stderr,
        )

        environment["SILICON_LEGACY_UPDATE_FENCE_OWNER"] = "tx-1"
        owned = subprocess.run(
            ["bash", str(entrypoint)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(owned.returncode, 0)
        self.assertNotIn("legacy offline update is in progress", owned.stderr)

    def test_legacy_offline_fence_requires_strict_finite_metadata(self):
        instance = self.root / "silicons" / "ada"
        marker = (
            instance
            / ".silicon"
            / "maintenance"
            / "legacy-offline.json"
        )
        marker.parent.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
        )
        marker.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "update_id": "tx-1",
                    "created_at": 123.5,
                }
            )
        )
        self.assertEqual(
            docker_runtime._legacy_offline_fence_owner(inst),
            "tx-1",
        )

        for invalid_created_at in (True, float("nan"), float("inf")):
            marker.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "update_id": "tx-1",
                        "created_at": invalid_created_at,
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "fence is invalid"):
                docker_runtime._legacy_offline_fence_owner(inst)

    def test_normal_start_reconciles_then_refuses_active_legacy_fence(self):
        from silicon_cli import update

        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        with (
            mock.patch.object(docker_runtime, "ensure_ready"),
            mock.patch.object(
                update, "reconcile_before_start"
            ) as reconcile,
            mock.patch.object(
                docker_runtime,
                "_legacy_offline_fence_owner",
                return_value="tx-1",
            ),
            mock.patch.object(docker_runtime, "_run") as run,
            mock.patch.object(ui, "error"),
        ):
            docker_runtime.start_one(inst)

        reconcile.assert_called_once_with(inst)
        run.assert_not_called()

    def test_updater_owned_start_uses_fence_owner_only_for_recreation(self):
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        cfg = self.write_docker_config()
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            cfg["compose_file"],
            cfg["image"],
            "silicon-ada",
        )
        with (
            mock.patch.object(docker_runtime, "ensure_ready"),
            mock.patch.object(
                docker_runtime,
                "_legacy_offline_fence_owner",
                return_value="tx-1",
            ),
            mock.patch.object(
                docker_runtime,
                "active_generation_runtime_image",
                return_value="",
            ),
            mock.patch.object(
                docker_runtime, "config_for_install", return_value=cfg
            ),
            mock.patch.object(docker_runtime, "_ensure_image"),
            mock.patch.object(
                docker_runtime, "render_compose"
            ) as render,
            mock.patch.object(
                docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ),
            mock.patch.object(
                docker_runtime, "_wait_for_container", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "silicon_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "glass_agent_running", return_value=True
            ),
            mock.patch.object(ui, "info"),
            mock.patch.object(ui, "success"),
        ):
            docker_runtime.start_one(
                inst,
                reconcile=False,
                allow_legacy_fence=True,
            )

        self.assertEqual(
            render.call_args_list[0],
            mock.call(
                cfg,
                update_fence_owners={"ada": "tx-1"},
                pinned_targets={"ada"},
            ),
        )
        self.assertEqual(
            render.call_args_list[-1],
            mock.call(cfg, pinned_targets={"ada"}),
        )

    def test_updater_owned_cold_start_bypasses_recursive_reconciliation(self):
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        cfg = self.write_docker_config()
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            cfg["compose_file"],
            cfg["image"],
            "silicon-ada",
        )
        suspend_marker = instance / ".silicon" / "docker-start-suspended"

        def acknowledge_start(_install):
            suspend_marker.unlink()
            return True

        with (
            mock.patch.object(docker_runtime, "ensure_ready"),
            mock.patch.object(
                docker_runtime,
                "_legacy_offline_fence_owner",
                return_value=None,
            ),
            mock.patch.object(
                docker_runtime,
                "active_generation_runtime_image",
                return_value="",
            ),
            mock.patch.object(
                docker_runtime, "config_for_install", return_value=cfg
            ),
            mock.patch.object(docker_runtime, "_ensure_image"),
            mock.patch.object(docker_runtime, "render_compose"),
            mock.patch.object(
                docker_runtime,
                "container_running",
                side_effect=[False, True],
            ),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            mock.patch.object(
                docker_runtime,
                "_wait_for_container",
                side_effect=acknowledge_start,
            ),
            mock.patch.object(
                docker_runtime, "silicon_running", return_value=False
            ),
            mock.patch.object(
                docker_runtime, "glass_agent_running", return_value=False
            ),
            mock.patch.object(docker_runtime, "_exec_silicon") as exec_silicon,
            mock.patch.object(ui, "info"),
            mock.patch.object(ui, "success"),
        ):
            docker_runtime.start_one(
                inst,
                start_agent=False,
                reconcile=False,
            )

        exec_silicon.assert_not_called()
        self.assertTrue(
            any(
                "process._start_one_unlocked" in " ".join(call.args[0])
                and "SILICON_INTERFACE_RESET_DAEMON_PID=1"
                in call.args[0]
                for call in run.call_args_list
            )
        )
        self.assertFalse(suspend_marker.exists())

    def test_cold_rollback_starts_interface_from_pre_activation_image(self):
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        cfg = self.write_docker_config()
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            cfg["compose_file"],
            cfg["image"],
            "silicon-ada",
        )
        suspend_marker = instance / ".silicon" / "docker-start-suspended"

        def acknowledge_start(_install):
            suspend_marker.unlink()
            return True

        with (
            mock.patch.object(docker_runtime, "ensure_ready"),
            mock.patch.object(
                docker_runtime,
                "_legacy_offline_fence_owner",
                return_value=None,
            ),
            mock.patch.object(
                docker_runtime,
                "active_generation_runtime_image",
                return_value="",
            ),
            mock.patch.object(
                docker_runtime,
                "config_for_install",
                return_value=cfg,
            ),
            mock.patch.object(docker_runtime, "_ensure_image"),
            mock.patch.object(docker_runtime, "render_compose"),
            mock.patch.object(
                docker_runtime,
                "container_running",
                side_effect=[False, True],
            ),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ),
            mock.patch.object(
                docker_runtime,
                "_wait_for_container",
                side_effect=acknowledge_start,
            ),
            mock.patch.object(
                docker_runtime,
                "silicon_running",
                return_value=False,
            ),
            mock.patch.object(
                docker_runtime,
                "_container_supports_interface_activation",
                return_value=False,
            ),
            mock.patch.object(
                docker_runtime,
                "interface_daemon_running",
                side_effect=[False, True],
            ),
            mock.patch.object(
                docker_runtime,
                "_start_legacy_container_interface",
            ) as start_interface,
            mock.patch.object(
                docker_runtime,
                "glass_agent_running",
                return_value=False,
            ),
            mock.patch.object(ui, "info"),
            mock.patch.object(ui, "success"),
        ):
            docker_runtime.start_one(
                inst,
                start_agent=False,
                start_interface=True,
                reconcile=False,
            )

        start_interface.assert_called_once_with(
            inst,
            reset_pid=True,
        )

    def test_pre_activation_image_starts_its_absolute_interface_command(self):
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        with (
            mock.patch.object(
                docker_runtime,
                "_reset_container_interface_pid",
            ) as reset,
            mock.patch.object(
                docker_runtime,
                "_exec_args",
                return_value=["docker", "exec", "interface"],
            ) as exec_args,
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ),
        ):
            docker_runtime._start_legacy_container_interface(
                inst,
                reset_pid=True,
            )

        reset.assert_called_once_with(inst)
        exec_args.assert_called_once_with(
            inst,
            [
                "/usr/local/bin/silicon-interface",
                "daemon",
                "start",
            ],
            extra_environment=(
                "SILICON_INTERFACE_ROOT=/silicon",
            ),
        )

    def test_active_container_python_uses_generation_environment(self):
        instance = self.root / "silicons" / "ada"
        environment = instance / ".silicon" / "environments" / "env-1"
        (environment / "bin").mkdir(parents=True)
        (environment / "bin" / "python").write_bytes(b"python")
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        store = mock.Mock()
        store.current.return_value = {"generation_id": "generation-1"}
        store.resolve_environment.return_value = environment

        with mock.patch(
            "silicon_cli.updater.generation.GenerationStore",
            return_value=store,
        ):
            selected = docker_runtime._active_container_python(inst)

        self.assertEqual(
            selected,
            "/silicon/.silicon/environments/env-1/bin/python",
        )

    def test_start_binds_only_the_active_generation_digest(self):
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        cfg = self.write_docker_config()
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            cfg["compose_file"],
            cfg["image"],
            "silicon-ada",
        )
        active = (
            "ghcr.io/teamofsilicons/silicon-runtime@sha256:" + "3" * 64
        )

        def bind(image, *, installs, pull=True):
            self.assertEqual(image, active)
            self.assertEqual(installs, [inst])
            inst.image = image
            return {**cfg, "image": image}

        with (
            mock.patch.object(docker_runtime, "ensure_ready"),
            mock.patch.object(
                docker_runtime,
                "_legacy_offline_fence_owner",
                return_value="",
            ),
            mock.patch.object(
                docker_runtime,
                "active_generation_runtime_image",
                return_value=active,
            ),
            mock.patch.object(
                docker_runtime,
                "bind_release_runtime",
                side_effect=bind,
            ) as bind_runtime,
            mock.patch.object(
                docker_runtime,
                "config_for_install",
                return_value={**cfg, "image": active},
            ),
            mock.patch.object(docker_runtime, "_ensure_image"),
            mock.patch.object(docker_runtime, "render_compose"),
            mock.patch.object(
                docker_runtime, "container_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime,
                "_run",
                return_value=SimpleNamespace(returncode=0),
            ),
            mock.patch.object(
                docker_runtime, "_wait_for_container", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "silicon_running", return_value=True
            ),
            mock.patch.object(
                docker_runtime, "glass_agent_running", return_value=True
            ),
            mock.patch.object(ui, "info"),
            mock.patch.object(ui, "success"),
        ):
            docker_runtime.start_one(inst, reconcile=False)

        bind_runtime.assert_called_once_with(active, installs=[inst])

    def test_ensure_ready_auto_initializes_and_pulls_image(self):
        calls = []
        old_binary = docker_runtime._ensure_docker_binary
        old_daemon = docker_runtime._ensure_daemon
        old_compose = docker_runtime._ensure_compose
        old_image = docker_runtime._ensure_image

        def fake_binary(install):
            calls.append(("binary", install))

        def fake_daemon(config):
            calls.append(("daemon", config["image"]))
            return {**config, "docker_sudo": True}

        def fake_compose(config):
            calls.append(("compose", config["docker_sudo"]))

        def fake_image(config, *, refresh=False):
            calls.append(("image", config["image"], refresh))

        docker_runtime._ensure_docker_binary = fake_binary
        docker_runtime._ensure_daemon = fake_daemon
        docker_runtime._ensure_compose = fake_compose
        docker_runtime._ensure_image = fake_image
        try:
            cfg = docker_runtime.ensure_ready(
                auto_init=True,
                root=str(self.root / "silicons"),
                image="example/silicon:latest",
            )
        finally:
            docker_runtime._ensure_docker_binary = old_binary
            docker_runtime._ensure_daemon = old_daemon
            docker_runtime._ensure_compose = old_compose
            docker_runtime._ensure_image = old_image

        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["docker_sudo"])
        self.assertEqual(cfg["image"], "example/silicon:latest")
        self.assertEqual(cfg["shared_home"], str((self.root / "silicons" / ".shared-home").resolve()))
        self.assertTrue(Path(cfg["compose_file"]).exists())
        self.assertIn(("image", "example/silicon:latest", False), calls)

    def test_ensure_ready_can_refresh_existing_runtime_image(self):
        calls = []
        old_binary = docker_runtime._ensure_docker_binary
        old_daemon = docker_runtime._ensure_daemon
        old_compose = docker_runtime._ensure_compose
        old_image = docker_runtime._ensure_image

        docker_runtime._ensure_docker_binary = lambda install: calls.append(("binary", install))
        docker_runtime._ensure_daemon = lambda config: config
        docker_runtime._ensure_compose = lambda config: calls.append(("compose", config["image"]))

        def fake_image(config, *, refresh=False):
            calls.append(("image", config["image"], refresh))

        docker_runtime._ensure_image = fake_image
        try:
            docker_runtime.ensure_ready(
                auto_init=True,
                root=str(self.root / "silicons"),
                image="example/silicon:latest",
                refresh_image=True,
            )
        finally:
            docker_runtime._ensure_docker_binary = old_binary
            docker_runtime._ensure_daemon = old_daemon
            docker_runtime._ensure_compose = old_compose
            docker_runtime._ensure_image = old_image

        self.assertIn(("image", "example/silicon:latest", True), calls)

    def test_refresh_runtime_image_pulls_even_when_cached(self):
        cfg = self.write_docker_config()
        calls = []
        old_cmd = docker_runtime._cmd
        old_run = docker_runtime._run

        def fake_cmd(cmd):
            calls.append(("cmd", cmd))
            return SimpleNamespace(returncode=0)

        def fake_run(cmd, *, check=False, capture=False):
            calls.append(("run", cmd))
            return SimpleNamespace(returncode=0)

        docker_runtime._cmd = fake_cmd
        docker_runtime._run = fake_run
        try:
            docker_runtime._ensure_image(cfg, refresh=True)
        finally:
            docker_runtime._cmd = old_cmd
            docker_runtime._run = old_run

        self.assertEqual(calls[0][0], "cmd")
        self.assertEqual(calls[1][0], "run")
        self.assertEqual(calls[1][1], ["docker", "pull", "example/silicon:latest"])
        self.assertEqual(calls[2][0], "cmd")

    def test_refresh_runtime_image_uses_cache_when_pull_fails(self):
        cfg = self.write_docker_config()
        calls = []
        old_cmd = docker_runtime._cmd
        old_run = docker_runtime._run

        def fake_cmd(cmd):
            calls.append(("cmd", cmd))
            return SimpleNamespace(returncode=0)

        def fake_run(cmd, *, check=False, capture=False):
            calls.append(("run", cmd))
            return SimpleNamespace(returncode=1)

        docker_runtime._cmd = fake_cmd
        docker_runtime._run = fake_run
        try:
            docker_runtime._ensure_image(cfg, refresh=True)
        finally:
            docker_runtime._cmd = old_cmd
            docker_runtime._run = old_run

        self.assertEqual(calls[0][0], "cmd")
        self.assertEqual(calls[1][0], "run")

    def test_pull_runtime_can_be_opted_out(self):
        os.environ["SILICON_RUNTIME"] = "local"
        old_ensure = docker_runtime.ensure_ready

        def fail_if_called(**_kwargs):
            raise AssertionError("ensure_ready should not be called")

        docker_runtime.ensure_ready = fail_if_called
        try:
            self.assertFalse(docker_runtime.ensure_pull_runtime())
        finally:
            docker_runtime.ensure_ready = old_ensure

    def test_docker_install_never_downloads_or_executes_root_script(self):
        old_manual = docker_runtime._manual_docker_steps
        old_run = docker_runtime._run
        calls = []

        docker_runtime._manual_docker_steps = lambda: calls.append("manual")
        docker_runtime._run = lambda *_args, **_kwargs: calls.append("run")
        os.environ["SILICON_DOCKER_AUTO_INSTALL"] = "1"
        try:
            self.assertFalse(docker_runtime._install_docker_engine())
        finally:
            docker_runtime._manual_docker_steps = old_manual
            docker_runtime._run = old_run

        self.assertEqual(calls, ["manual"])

    def test_auth_container_mounts_shared_home(self):
        cfg = self.write_docker_config()
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            self.assertEqual(docker_runtime._auth_container(cfg, "codex"), 0)
        finally:
            docker_runtime._run = old_run

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("-it", cmd)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertEqual(cmd[-2:], ["auth", "codex"])

    def test_shared_tool_container_forwards_args(self):
        cfg = self.write_docker_config()
        captured = {}
        old_run = docker_runtime._run
        old_interactive = ui.interactive

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        ui.interactive = lambda: True
        try:
            self.assertEqual(docker_runtime._shared_tool_container(cfg, "claude", ["--version"]), 0)
        finally:
            docker_runtime._run = old_run
            ui.interactive = old_interactive

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("-it", cmd)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertEqual(cmd[-3:], ["shared", "claude", "--version"])


if __name__ == "__main__":
    unittest.main()
