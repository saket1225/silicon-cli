from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli.updater import snapshot_adapter
from silicon_cli.updater.generation import GenerationStore
from silicon_cli.updater.snapshot_adapter import (
    BootstrapSnapshotError,
    create_local_snapshot,
    restore_local_snapshot_in_place,
    verify_local_snapshot,
)


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def write_release_floor(
    root: Path,
    sequence: int,
    tree_sha256: str,
) -> None:
    write(
        root,
        ".silicon/release-sequence-floor.json",
        json.dumps(
            {
                "schema": 1,
                "sequence": sequence,
                "tree_sha256": tree_sha256,
                "recorded_at": float(sequence),
            }
        )
        + "\n",
    )


def signed_release(sequence: int, tree_sha256: str) -> dict:
    return {
        "version": f"v{sequence}",
        "revision": str(sequence) * 40,
        "sequence": sequence,
        "tree_sha256": tree_sha256,
        "artifact_sha256": "f" * 64,
        "source": "glass",
        "trust": "signed-ed25519",
    }


class BootstrapSnapshotAdapterTests(unittest.TestCase):
    def test_writes_canonical_schema_and_never_snapshots_plaintext_secrets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "remember this")
            write(
                root,
                "core/interface_state/maintenance.json",
                '{"epoch":7}',
            )
            write(root, ".silicon/overlays/manifests/test.json", "{}")
            write(root, ".glass.json", '{"api_key":"never-copy"}')
            result = create_local_snapshot(root, release_id="legacy:pre-update")
            manifest = verify_local_snapshot(
                Path(result["manifest_path"]), store=Path(result["store"])
            )
            self.assertEqual(
                set(manifest),
                {"schema", "release_id", "files", "tombstones", "root_hash"},
            )
            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("prompts/MEMORY.md", paths)
            self.assertIn("core/interface_state/maintenance.json", paths)
            self.assertIn(".silicon/overlays/manifests/test.json", paths)
            self.assertNotIn(".glass.json", paths)
            self.assertNotIn("never-copy", json.dumps(manifest))

    def test_additive_policy_cannot_force_a_secret_into_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, ".glass.json", '{"api_key":"secret"}')
            write(root, ".backupsilicon", ".glass.json\n")
            with self.assertRaises(BootstrapSnapshotError):
                create_local_snapshot(root, release_id="legacy")

    def test_snapshot_rejects_invalid_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "remember this")
            write_release_floor(root, 0, "a" * 64)

            with self.assertRaisesRegex(
                BootstrapSnapshotError,
                "release sequence floor.*invalid",
            ):
                create_local_snapshot(root, release_id="legacy")

    def test_in_place_restore_recovers_memory_but_not_live_maintenance_epoch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            write(
                root,
                "core/interface_state/maintenance.json",
                '{"epoch":1}',
            )
            result = create_local_snapshot(root, release_id="legacy")
            write(root, "prompts/MEMORY.md", "candidate-corruption")
            write(root, "prompts/memory/new.md", "candidate-created")
            write(
                root,
                "core/interface_state/maintenance.json",
                '{"epoch":2}',
            )

            restored = restore_local_snapshot_in_place(
                root,
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )
            # Repeating the restore after an ambiguous crash is safe.
            restore_local_snapshot_in_place(
                root,
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )

            self.assertEqual(restored["root_hash"], result["root_hash"])
            self.assertEqual((root / "prompts/MEMORY.md").read_text(), "before")
            self.assertFalse((root / "prompts/memory/new.md").exists())
            self.assertEqual(
                json.loads(
                    (
                        root
                        / "core"
                        / "interface_state"
                        / "maintenance.json"
                    ).read_text()
                ),
                {"epoch": 2},
            )

    def test_in_place_restore_never_lowers_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            write_release_floor(root, 10, "a" * 64)
            result = create_local_snapshot(root, release_id="legacy")
            manifest = verify_local_snapshot(
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )
            floor_entry = next(
                entry
                for entry in manifest["files"]
                if entry["path"] == ".silicon/release-sequence-floor.json"
            )
            self.assertEqual(floor_entry["classes"], ["security_state"])

            write(root, "prompts/MEMORY.md", "candidate")
            write_release_floor(root, 11, "b" * 64)
            restore_local_snapshot_in_place(
                root,
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )

            self.assertEqual((root / "prompts/MEMORY.md").read_text(), "before")
            floor = json.loads(
                (root / ".silicon/release-sequence-floor.json").read_text()
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)

    def test_in_place_restore_raises_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            write_release_floor(root, 11, "b" * 64)
            result = create_local_snapshot(root, release_id="legacy")
            write_release_floor(root, 10, "a" * 64)

            restore_local_snapshot_in_place(
                root,
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )

            floor = json.loads(
                (root / ".silicon/release-sequence-floor.json").read_text()
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)

    def test_restore_and_generation_writer_share_release_floor_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            write_release_floor(root, 11, "b" * 64)
            result = create_local_snapshot(root, release_id="legacy")
            write_release_floor(root, 10, "a" * 64)

            restore_entered = threading.Event()
            finish_restore = threading.Event()
            writer_started = threading.Event()
            writer_done = threading.Event()
            errors: list[BaseException] = []
            original_restore = (
                snapshot_adapter._restore_local_snapshot_in_place_locked
            )

            def blocked_restore(*args, **kwargs):
                restore_entered.set()
                if not finish_restore.wait(2):
                    raise AssertionError("test did not release snapshot restore")
                return original_restore(*args, **kwargs)

            def run_restore() -> None:
                try:
                    restore_local_snapshot_in_place(
                        root,
                        Path(result["manifest_path"]),
                        store=Path(result["store"]),
                    )
                except BaseException as exc:
                    errors.append(exc)

            def run_writer() -> None:
                writer_started.set()
                try:
                    GenerationStore(root).record_release_floor(
                        signed_release(12, "c" * 64)
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_done.set()

            with mock.patch.object(
                snapshot_adapter,
                "_restore_local_snapshot_in_place_locked",
                side_effect=blocked_restore,
            ):
                restore_thread = threading.Thread(target=run_restore)
                restore_thread.start()
                self.assertTrue(restore_entered.wait(2))
                writer_thread = threading.Thread(target=run_writer)
                writer_thread.start()
                self.assertTrue(writer_started.wait(2))
                self.assertFalse(writer_done.wait(0.1))
                finish_restore.set()
                restore_thread.join(2)
                writer_thread.join(2)

            self.assertFalse(restore_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual(errors, [])
            floor = json.loads(
                (root / ".silicon/release-sequence-floor.json").read_text()
            )
            self.assertEqual(floor["sequence"], 12)
            self.assertEqual(floor["tree_sha256"], "c" * 64)

    def test_equal_release_sequence_conflict_fails_before_data_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            write_release_floor(root, 11, "a" * 64)
            result = create_local_snapshot(root, release_id="legacy")
            write(root, "prompts/MEMORY.md", "must remain untouched")
            write_release_floor(root, 11, "b" * 64)

            with self.assertRaisesRegex(
                BootstrapSnapshotError,
                "reused for different immutable content",
            ):
                restore_local_snapshot_in_place(
                    root,
                    Path(result["manifest_path"]),
                    store=Path(result["store"]),
                )

            self.assertEqual(
                (root / "prompts/MEMORY.md").read_text(),
                "must remain untouched",
            )

    def test_snapshot_without_floor_never_deletes_new_local_floor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write(root, "prompts/MEMORY.md", "before")
            result = create_local_snapshot(root, release_id="legacy")
            write(root, "prompts/MEMORY.md", "candidate")
            write_release_floor(root, 11, "b" * 64)

            restore_local_snapshot_in_place(
                root,
                Path(result["manifest_path"]),
                store=Path(result["store"]),
            )

            floor = json.loads(
                (root / ".silicon/release-sequence-floor.json").read_text()
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)


if __name__ == "__main__":
    unittest.main()
