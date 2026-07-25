from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from silicon_cli import backup_runtime
from silicon_cli.updater.cache import ReleaseCache
from silicon_cli.updater.planner import seed_legacy_snapshot
from silicon_cli.updater.release import create_artifact


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class ActiveCustomizationCaptureTests(unittest.TestCase):
    def test_legacy_capture_binds_dna_to_cli_seed_and_excludes_living_memory(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            upstream = workspace / "upstream"
            instance = workspace / "instance"
            upstream.mkdir()
            instance.mkdir()
            write(upstream, "main.py", "print('base')\n")
            write(upstream, "prompts/DNA.py", "IDENTITY = 'base'\n")
            write(instance, "main.py", "print('base')\n")
            write(instance, "prompts/DNA.py", "IDENTITY = 'self-authored'\n")
            write(instance, "prompts/MEMORY.md", "private living memory\n")
            seed_legacy_snapshot(upstream, instance)

            reference = backup_runtime.capture_active_customizations(instance)
            manifest = backup_runtime.OverlayStore(instance).verify(
                reference["overlay_root_hash"]
            )

            self.assertEqual(reference["generation_id"], "legacy-flat")
            self.assertEqual(reference["base_source"], "legacy-cli-seed")
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]],
                ["prompts/DNA.py"],
            )
            self.assertNotIn("MEMORY", json.dumps(manifest))
            self.assertEqual(
                backup_runtime.load_latest_customization_reference(instance),
                reference,
            )

    def test_immutable_capture_requires_exact_verified_cache_release(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            upstream = workspace / "upstream"
            instance = workspace / "instance"
            cache = ReleaseCache(workspace / "cache")
            upstream.mkdir()
            instance.mkdir()
            write(upstream, "main.py", "print('release')\n")
            write(upstream, "prompts/DNA.py", "IDENTITY = 'base'\n")
            fetched = create_artifact(
                upstream,
                workspace / "release.tar",
                revision="a" * 40,
                source_label="test",
                trust="test",
            )
            cached = cache.store(fetched)
            release = instance / ".silicon" / "releases" / "generation-1"
            cache.materialize(cached, release)
            write(release, "prompts/DNA.py", "IDENTITY = 'evolved'\n")
            pointer = {
                "schema": 1,
                "kind": "immutable-release",
                "generation_id": "generation-1",
                "release_path": ".silicon/releases/generation-1",
                "environment_path": "",
                "upstream_tree_sha256": cached.manifest.identity.tree_sha256,
                "materialized_tree_sha256": (
                    cached.manifest.identity.tree_sha256
                ),
                "release": cached.manifest.identity.to_dict(),
                "overlay_root_hash": "0" * 64,
                "runtime_image": cached.manifest.runtime_image,
                "activated_at": 1,
            }
            (instance / ".silicon" / "current.json").write_text(
                json.dumps(pointer),
                encoding="utf-8",
            )

            reference = backup_runtime.capture_active_customizations(
                instance,
                cache_root=cache.root,
            )

            self.assertEqual(
                reference["base_tree_sha256"],
                cached.manifest.identity.tree_sha256,
            )
            self.assertEqual(
                reference["base_source"],
                "verified-release-cache",
            )
            manifest = backup_runtime.OverlayStore(instance).verify(
                reference["overlay_root_hash"]
            )
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]],
                ["prompts/DNA.py"],
            )

            (cache.root / "releases" / cached.manifest.identity.tree_sha256 / "release.tar").write_bytes(
                b"corrupt"
            )
            with self.assertRaises(backup_runtime.BackupSafetyError):
                backup_runtime.capture_active_customizations(
                    instance,
                    cache_root=cache.root,
                )

    def test_latest_reference_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            upstream = workspace / "upstream"
            instance = workspace / "instance"
            upstream.mkdir()
            instance.mkdir()
            write(upstream, "main.py", "print('base')\n")
            write(instance, "main.py", "print('local')\n")
            seed_legacy_snapshot(upstream, instance)
            backup_runtime.capture_active_customizations(instance)
            latest = instance / backup_runtime.LATEST_OVERLAY_REFERENCE
            value = json.loads(latest.read_text(encoding="utf-8"))
            value["overlay_root_hash"] = "0" * 64
            os.chmod(latest, 0o600)
            latest.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(backup_runtime.BackupSafetyError):
                backup_runtime.load_latest_customization_reference(instance)


if __name__ == "__main__":
    unittest.main()
