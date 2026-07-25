from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from silicon_cli.updater.journal import ORDERED_STATES, TransactionJournal
from silicon_cli.updater.overlay import OverlayError, OverlayStore
from silicon_cli.updater.retention import RetentionError, RetentionManager
from silicon_cli import pull_transaction


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


class OverlayTests(unittest.TestCase):
    def test_modified_file_and_tombstone_survive_release_gc(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw) / "instance"
            base = Path(raw) / "base"
            local = Path(raw) / "local"
            for directory in (instance, base, local):
                directory.mkdir()
            write(base, "main.py", "old\n")
            write(base, "core/deleted.py", "delete me\n")
            write(local, "main.py", "self modified\n")
            write(local, "prompts/MEMORY.md", "private memory\n")
            write(local, ".glass.json", '{"api_key":"secret"}\n')

            overlay = OverlayStore(instance)
            captured = overlay.capture(
                base, local, base_tree_sha256="a" * 64
            )
            manifest = overlay.verify(captured["root_hash"])
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]], ["main.py"]
            )
            self.assertEqual(manifest["tombstones"], ["core/deleted.py"])
            serialized = json.dumps(manifest)
            self.assertNotIn("private memory", serialized)
            self.assertNotIn("api_key", serialized)

            restored = Path(raw) / "restored"
            shutil.copytree(base, restored)
            overlay.apply(captured["root_hash"], restored)
            self.assertEqual((restored / "main.py").read_text(), "self modified\n")
            self.assertFalse((restored / "core/deleted.py").exists())

            # Generation retention is confined to immutable release/cache
            # directories and must never remove backup-visible overlays.
            releases = instance / ".silicon" / "releases"
            releases.mkdir(parents=True)
            for index in range(5):
                write(releases, f"gen-{index}/main.py", str(index))
            RetentionManager(
                instance,
                Path(raw) / "cache",
                keep_generations=2,
            ).prune()
            self.assertEqual(
                overlay.verify(captured["root_hash"])["root_hash"],
                captured["root_hash"],
            )


class RetentionTests(unittest.TestCase):
    def _overlays(
        self,
        root: Path,
        count: int,
        *,
        base_tree: str = "a" * 64,
    ) -> list[dict]:
        instance = root / "instance"
        base = root / "base"
        local = root / "local"
        for directory in (instance, base, local):
            directory.mkdir(parents=True, exist_ok=True)
        write(base, "main.py", "base\n")
        captured = []
        store = OverlayStore(instance)
        for index in range(count):
            write(local, "main.py", f"custom-{index}\n")
            item = store.capture(
                base,
                local,
                base_tree_sha256=base_tree,
            )
            stamp = 1_700_000_000 + index
            os.utime(item["manifest_path"], (stamp, stamp))
            captured.append(item)
        return captured

    def test_overlay_gc_preserves_active_all_journals_and_backup_latest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            captured = self._overlays(root, 8)
            instance = root / "instance"
            cache_root = root / "cache"
            cache_release = cache_root / "releases" / ("a" * 64)
            cache_release.mkdir(parents=True)

            current = {
                "schema": 1,
                "release_path": str(instance),
                "upstream_tree_sha256": "b" * 64,
                "environment_path": "",
                "overlay_root_hash": captured[7]["root_hash"],
            }
            (instance / ".silicon" / "current.json").write_text(
                json.dumps(current),
                encoding="utf-8",
            )
            committed = TransactionJournal.create(
                instance,
                {
                    "new_generation": {
                        "overlay_root_hash": captured[6]["root_hash"],
                    },
                    "prior_generation": {
                        "overlay_root_hash": captured[5]["root_hash"],
                    },
                    "customization_overlay": captured[4],
                },
                transaction_id="committed-overlays",
            )
            for state in ORDERED_STATES[1:]:
                committed.transition(state, state.lower())
            failed_rollback = TransactionJournal.create(
                instance,
                {
                    "operation": "rollback",
                    "rollback_customization_delta": captured[3],
                    "rollback_customization_overlay": captured[2],
                },
                transaction_id="failed-rollback",
            )
            failed_rollback.transition("FAILED", "preserved rollback delta")
            nonterminal = TransactionJournal.create(
                instance,
                {
                    "new_generation": {
                        "overlay_root_hash": captured[1]["root_hash"],
                    },
                },
                transaction_id="nonterminal-overlays",
            )
            nonterminal.transition("RESOLVED", "resolved")
            latest = {
                "schema": 1,
                "generation_id": "legacy-flat",
                "generation_kind": "legacy-flat",
                "active_release_path": ".",
                "base_source": "legacy-cli-seed",
                "base_tree_sha256": "a" * 64,
                "overlay_root_hash": captured[0]["root_hash"],
                "overlay_manifest_path": (
                    ".silicon/overlays/manifests/"
                    f"{captured[0]['root_hash']}.json"
                ),
                "captured_at": time.time(),
            }
            (instance / ".silicon" / "overlays" / "latest.json").write_text(
                json.dumps(latest),
                encoding="utf-8",
            )

            removed = RetentionManager(
                instance,
                cache_root,
                keep_generations=2,
            ).prune()

            self.assertEqual(removed["overlay_manifests"], [])
            self.assertEqual(removed["overlay_objects"], [])
            self.assertTrue(cache_release.is_dir())
            for item in captured:
                self.assertEqual(
                    OverlayStore(instance).verify(item["root_hash"])["root_hash"],
                    item["root_hash"],
                )

    def test_overlay_gc_removes_only_unreferenced_old_manifests_and_objects(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            captured = self._overlays(root, 4)
            instance = root / "instance"
            (instance / ".silicon" / "current.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "release_path": str(instance),
                        "upstream_tree_sha256": "a" * 64,
                        "environment_path": "",
                        "overlay_root_hash": captured[0]["root_hash"],
                    }
                ),
                encoding="utf-8",
            )
            removed_manifest = OverlayStore(instance).verify(
                captured[1]["root_hash"]
            )
            removed_digest = removed_manifest["files"][0]["sha256"]

            removed = RetentionManager(
                instance,
                root / "cache",
                keep_generations=2,
            ).prune()

            self.assertEqual(
                removed["overlay_manifests"],
                [captured[1]["root_hash"]],
            )
            self.assertIn(removed_digest, removed["overlay_objects"])
            with self.assertRaises(OverlayError):
                OverlayStore(instance).verify(captured[1]["root_hash"])
            for index in (0, 2, 3):
                self.assertEqual(
                    OverlayStore(instance).verify(
                        captured[index]["root_hash"]
                    )["root_hash"],
                    captured[index]["root_hash"],
                )

    def test_corrupt_unreferenced_overlay_fails_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            captured = self._overlays(root, 4)
            instance = root / "instance"
            corrupt = Path(captured[0]["manifest_path"])
            os.chmod(corrupt, 0o600)
            corrupt.write_text("{not-json", encoding="utf-8")
            manifests = instance / ".silicon" / "overlays" / "manifests"
            before = {path.name for path in manifests.iterdir()}

            with self.assertRaises(RetentionError):
                RetentionManager(
                    instance,
                    root / "cache",
                    keep_generations=2,
                ).prune()

            self.assertEqual(
                {path.name for path in manifests.iterdir()},
                before,
            )

    def test_missing_referenced_overlay_blocks_all_retention_deletions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            captured = self._overlays(root, 3)
            instance = root / "instance"
            releases = instance / ".silicon" / "releases"
            for index in range(4):
                write(releases, f"generation-{index}/main.py", str(index))
            before = {path.name for path in releases.iterdir()}
            (instance / ".silicon" / "current.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "release_path": str(instance),
                        "upstream_tree_sha256": "a" * 64,
                        "environment_path": "",
                        "overlay_root_hash": "f" * 64,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RetentionError,
                "referenced customization overlay manifests are missing",
            ):
                RetentionManager(
                    instance,
                    root / "cache",
                    keep_generations=2,
                ).prune()

            self.assertEqual(
                {path.name for path in releases.iterdir()},
                before,
            )
            for item in captured:
                self.assertTrue(Path(item["manifest_path"]).is_file())

    def test_snapshot_gc_callback_is_post_commit_retention_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            instance = root / "instance"
            instance.mkdir()
            calls = []

            def snapshot_gc():
                calls.append("called")
                return {
                    "manifests": ["c" * 64],
                    "objects": ["d" * 64],
                }

            removed = RetentionManager(
                instance,
                root / "cache",
                snapshot_gc=snapshot_gc,
            ).prune()

            self.assertEqual(calls, ["called"])
            self.assertEqual(removed["snapshot_manifests"], ["c" * 64])
            self.assertEqual(removed["snapshot_objects"], ["d" * 64])

    def test_active_prior_and_nonterminal_references_are_never_pruned(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            instance = root / "instance"
            release_root = instance / ".silicon" / "releases"
            transaction_root = instance / ".silicon" / "transactions"
            cache_root = root / "cache"
            cache_releases = cache_root / "releases"
            cache_environments = cache_root / "environments"
            for directory in (
                release_root,
                transaction_root,
                cache_releases,
                cache_environments,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            generations = []
            environments = []
            trees = [f"{index:064x}" for index in range(6)]
            for index, tree in enumerate(trees):
                generation = release_root / f"generation-{index}"
                write(generation, "main.py", str(index))
                generations.append(generation.resolve())
                cache_release = cache_releases / tree
                cache_release.mkdir()
                environment = cache_environments / f"env-{index}"
                environment.mkdir()
                environments.append(environment.resolve())
                stamp = time.time() - (100 - index)
                os.utime(generation, (stamp, stamp))
                os.utime(cache_release, (stamp, stamp))
                os.utime(environment, (stamp, stamp))

            current = {
                "schema": 1,
                "kind": "immutable-release",
                "generation_id": "generation-5",
                "release_path": str(generations[5]),
                "upstream_tree_sha256": trees[5],
                "environment_path": str(environments[5]),
            }
            (instance / ".silicon" / "current.json").write_text(
                json.dumps(current)
            )
            committed = TransactionJournal.create(
                instance,
                {
                    "new_generation": current,
                    "prior_generation": {
                        "release_path": str(generations[4]),
                        "upstream_tree_sha256": trees[4],
                        "environment_path": str(environments[4]),
                    },
                },
                transaction_id="committed",
            )
            for state in ORDERED_STATES[1:]:
                committed.transition(state, state.lower())
            nonterminal = TransactionJournal.create(
                instance,
                {
                    "new_generation": {
                        "release_path": str(generations[0]),
                        "upstream_tree_sha256": trees[0],
                        "environment_path": str(environments[0]),
                    },
                    "release": {"tree_sha256": trees[0]},
                },
                transaction_id="in-progress",
            )
            nonterminal.transition("RESOLVED", "resolved")
            nonterminal.transition("STAGED", "staged")

            removed = RetentionManager(
                instance, cache_root, keep_generations=2
            ).prune()

            for index in (0, 4, 5):
                self.assertTrue(generations[index].is_dir())
                self.assertTrue((cache_releases / trees[index]).is_dir())
                self.assertTrue(environments[index].is_dir())
            self.assertTrue(removed["generations"])
            self.assertTrue(removed["release_cache"])
            self.assertTrue(removed["environments"])

    def test_nonterminal_host_pull_protects_exact_shared_cache_inputs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            instance = root / "instance"
            cache_root = root / "cache"
            cache_releases = cache_root / "releases"
            cache_environments = cache_root / "environments"
            target_parent = root / "targets"
            for directory in (
                instance,
                cache_releases,
                cache_environments,
                target_parent,
            ):
                directory.mkdir(parents=True, exist_ok=True)
            trees = [f"{index:064x}" for index in range(5)]
            environments = []
            for index, tree in enumerate(trees):
                (cache_releases / tree).mkdir()
                environment = cache_environments / f"env-{index}"
                environment.mkdir()
                environments.append(environment.resolve())
                stamp = time.time() - (100 - index)
                os.utime(cache_releases / tree, (stamp, stamp))
                os.utime(environment, (stamp, stamp))

            journal = pull_transaction.PullJournal.open_or_create(
                root,
                kind="team",
                server="https://glass.example",
                credential="sct_live_secret",
            )
            item = pull_transaction.planned_item(
                silicon_id="ada-1",
                silicon_name="Ada",
                name="ada",
                parent=target_parent,
                transaction_id=journal.transaction_id,
                setup_config={},
            )
            journal.initialize(
                team_name="Acme",
                runtime="local",
                runtime_image="",
                release_tree_sha256=trees[0],
                environment_path=str(environments[0]),
                backups=False,
                provider_key_env={},
                items=[item],
            )

            RetentionManager(
                instance, cache_root, keep_generations=2
            ).prune()

            self.assertTrue((cache_releases / trees[0]).is_dir())
            self.assertTrue(environments[0].is_dir())


if __name__ == "__main__":
    unittest.main()
