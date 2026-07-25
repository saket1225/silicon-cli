from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from silicon_cli import config, docker_runtime, stemcell
from silicon_cli.config import active_environment_python, active_release_root
from silicon_cli.updater.cache import (
    ReleaseCache,
    runtime_platform_identity,
)
from silicon_cli.updater.release import create_artifact


class SignedHydrationTests(unittest.TestCase):
    def test_marker_only_recovery_cannot_activate_an_older_signed_release(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "target"
            target.mkdir()
            cache = ReleaseCache(root / "cache")

            def prepared(sequence: int, label: str):
                source = root / f"source-{label}"
                source.mkdir()
                (source / "main.py").write_text(
                    f"print('{label}')\n", encoding="utf-8"
                )
                (source / ".silicon-data-root-v1").write_text(
                    "1\n", encoding="utf-8"
                )
                (source / "silicon-release.json").write_text(
                    json.dumps(
                        {
                            "identity": {
                                "version": label,
                                "sequence": sequence,
                                "tree_sha256": "",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                release = create_artifact(
                    source,
                    root / f"{label}.tar",
                    revision=(f"{sequence:x}" * 64)[:64],
                    source_label="glass",
                    trust="signed-ed25519",
                )
                (source / "silicon-release.json").unlink()
                release = replace(
                    release,
                    manifest=replace(
                        release.manifest,
                        runtime_image=(
                            "registry.example/silicon@sha256:" + "a" * 64
                        ),
                    ),
                )
                return stemcell.PreparedStemcell(
                    cache=cache,
                    release=release,
                    source=source,
                    environment=None,
                )

            release_a = prepared(20, "v20")
            release_b = prepared(19, "v19")
            stemcell._install_initial_generation(
                target, release_a, install_deps=False
            )
            pointer = target / ".silicon" / "current.json"
            marker = (
                target
                / ".silicon"
                / "generation-managed-v1.json"
            )
            pointer.unlink()
            marker.unlink()

            with self.assertRaisesRegex(
                Exception, "signed sequence floor|recorded before the crash"
            ):
                stemcell._install_initial_generation(
                    target, release_b, install_deps=False
                )
            self.assertFalse(pointer.exists())

            stemcell._install_initial_generation(
                target, release_a, install_deps=False
            )
            pointer.unlink()
            with self.assertRaisesRegex(
                RuntimeError, "does not match.*recorded before the crash"
            ):
                stemcell._install_initial_generation(
                    target, release_b, install_deps=False
                )
            self.assertFalse(pointer.exists())
            stemcell._install_initial_generation(
                target, release_a, install_deps=False
            )
            current = json.loads(pointer.read_text(encoding="utf-8"))
            self.assertEqual(current["release"]["sequence"], 20)
            self.assertEqual(
                current["upstream_tree_sha256"],
                release_a.release.manifest.identity.tree_sha256,
            )

    def test_team_hydration_reuses_one_verified_release_and_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            source = root / "publisher"
            source.mkdir()
            (source / "main.py").write_text("print('signed')\n")
            (source / ".silicon-data-root-v1").write_text("1\n")
            (source / "requirements.txt").write_text("certifi\n")
            lock = source / "requirements.lock"
            lock.write_text(
                "certifi==2026.7.22 --hash=sha256:" + "a" * 64 + "\n"
            )
            (source / "env.py").write_text('GLASS_API_KEY = ""\n')
            (source / "silicon.json").write_text(
                '{"name":"Silicon","address":""}\n'
            )
            templates = source / "templates" / "prompts"
            templates.mkdir(parents=True)
            (templates / "MEMORY.md").write_text("# Memory\n")
            release = create_artifact(
                source,
                root / "release.tar",
                revision="a" * 64,
                source_label="glass",
                trust="signed-ed25519",
            )
            cache = ReleaseCache(home / "cache")
            cached = cache.store(release)

            environment = home / "cache" / "environments" / "shared"
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            python.parent.mkdir(parents=True)
            python.write_text("")
            (environment / ".silicon-environment.json").write_text(
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

            first = root / "ada"
            second = root / "grace"
            with (
                mock.patch.object(stemcell, "REGISTRY_DIR", home),
                mock.patch.object(config, "REGISTRY_DIR", home),
                mock.patch.object(
                    stemcell,
                    "fetch_latest_release",
                    return_value=cached,
                ) as fetch,
                mock.patch.object(
                    ReleaseCache,
                    "prepare_environment",
                    return_value=environment,
                ) as prepare_environment,
                mock.patch.object(
                    docker_runtime,
                    "enabled",
                    return_value=True,
                ),
                mock.patch.object(
                    docker_runtime,
                    "bind_release_runtime",
                ) as bind_runtime,
                mock.patch.object(stemcell.ui, "interactive", return_value=False),
            ):
                with stemcell.prepare_hydration() as prepared:
                    stemcell.hydrate(
                        str(first),
                        prepared=prepared,
                        setup_interface=False,
                        register_install=False,
                    )
                    stemcell.hydrate(
                        str(second),
                        prepared=prepared,
                        setup_interface=False,
                        register_install=False,
                    )
                    # Simulate a crash after the fail-closed managed marker
                    # became durable but before current.json was published.
                    (first / ".silicon" / "current.json").unlink()
                    stemcell.hydrate(
                        str(first),
                        prepared=prepared,
                        setup_interface=False,
                        register_install=False,
                    )

                fetch.assert_called_once()
                self.assertFalse(
                    fetch.call_args.kwargs["allow_unsigned_git"]
                )
                prepare_environment.assert_called_once()
                bind_runtime.assert_not_called()
                for target in (first, second):
                    pointer = json.loads(
                        (
                            target / ".silicon" / "current.json"
                        ).read_text()
                    )
                    self.assertEqual(
                        pointer["upstream_tree_sha256"],
                        cached.manifest.identity.tree_sha256,
                    )
                    self.assertEqual(
                        Path(pointer["environment_path"]).resolve(),
                        environment.resolve(),
                    )
                    self.assertEqual(
                        Path(active_environment_python(target) or "").resolve(),
                        python.resolve(),
                    )
                    self.assertEqual(
                        (active_release_root(target) / "main.py").read_text(),
                        "print('signed')\n",
                    )
                    self.assertFalse((target / "main.py").exists())
                    self.assertEqual(
                        (target / "prompts" / "MEMORY.md").read_text(),
                        "# Memory\n",
                    )
                    self.assertTrue(
                        (
                            target
                            / ".silicon"
                            / "generation-managed-v1.json"
                        ).is_file()
                    )
                    self.assertFalse(
                        (target / ".silicon-upstream" / "base").exists()
                    )

            self.assertEqual(
                json.loads((first / "silicon.json").read_text())["address"],
                "ada",
            )
            self.assertEqual(
                json.loads((second / "silicon.json").read_text())["address"],
                "grace",
            )

    def test_legacy_flat_seed_preserves_existing_code_during_migration(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "main.py").write_text("print('upstream')\n")
            (source / "manager.py").write_text("print('manager')\n")
            (target / "main.py").write_text("print('self-authored')\n")

            stemcell._seed_legacy_flat_install(source, target)

            self.assertEqual(
                (target / "main.py").read_text(),
                "print('self-authored')\n",
            )
            self.assertEqual(
                (target / "manager.py").read_text(),
                "print('manager')\n",
            )


if __name__ == "__main__":
    unittest.main()
