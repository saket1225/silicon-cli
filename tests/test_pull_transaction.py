from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import pull_transaction, registry, sync


class AbruptCrash(BaseException):
    pass


def _item(journal, parent: Path, *, silicon_id: str = "sil-1", name: str = "ada"):
    return pull_transaction.planned_item(
        silicon_id=silicon_id,
        silicon_name="Ada",
        name=name,
        parent=parent,
        transaction_id=journal.transaction_id,
        setup_config={"brain": "claude"},
    )


def _initialize(journal, parent: Path, *, items=None):
    journal.initialize(
        team_name="Acme",
        runtime="local",
        runtime_image="",
        release_tree_sha256="a" * 64,
        environment_path="",
        backups=False,
        provider_key_env={},
        items=items or [_item(journal, parent)],
    )


class PullJournalTests(unittest.TestCase):
    def test_same_credential_resumes_without_storing_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "home"
            first = pull_transaction.PullJournal.open_or_create(
                root,
                kind="team",
                server="https://glass.example",
                credential="sct_live_never-write-this",
            )
            second = pull_transaction.PullJournal.open_or_create(
                root,
                kind="team",
                server="https://glass.example",
                credential="sct_live_never-write-this",
            )

            self.assertEqual(first.transaction_id, second.transaction_id)
            self.assertNotIn(
                "sct_live_never-write-this",
                first.path.read_text(encoding="utf-8"),
            )
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(first.path.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(first.path.parent.stat().st_mode), 0o700
                )

    def test_hidden_stage_rename_recovers_crash_before_journal_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            parent = root / "instances"
            parent.mkdir()
            journal = pull_transaction.PullJournal.open_or_create(
                home,
                kind="single",
                server="https://glass.example",
                credential="scs_live_secret",
            )
            _initialize(journal, parent)
            item = journal.items[0]
            stage = journal.prepare_stage(item)
            (stage / "ready").write_text("yes\n")
            journal.update_item(0, staged=True)
            journal.set_state("STAGED")

            # Power loss after the atomic filesystem operation but before the
            # journal bit is persisted.
            final = Path(item["final_path"])
            os.rename(stage, final)
            recovered = pull_transaction.PullJournal.load(journal.path)
            recovered.reconcile_and_commit()

            self.assertEqual(recovered.state, "RENAMED")
            self.assertTrue(recovered.items[0]["renamed"])
            self.assertTrue(final.is_dir())
            self.assertFalse(stage.exists())

    def test_precommit_cleanup_only_removes_marked_hidden_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "instances"
            parent.mkdir()
            journal = pull_transaction.PullJournal.open_or_create(
                root / "home",
                kind="single",
                server="https://glass.example",
                credential="scs_live_secret",
            )
            _initialize(journal, parent)
            stage = journal.prepare_stage(journal.items[0])
            (stage / "partial").write_text("partial\n")

            journal.cleanup_precommit()

            self.assertFalse(stage.exists())
            self.assertEqual(journal.state, "ABORTED")

    def test_cleanup_refuses_foreign_stage_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "instances"
            parent.mkdir()
            journal = pull_transaction.PullJournal.open_or_create(
                root / "home",
                kind="single",
                server="https://glass.example",
                credential="scs_live_secret",
            )
            _initialize(journal, parent)
            stage = Path(journal.items[0]["stage_path"])
            stage.mkdir(mode=0o700)
            (stage / "foreign").write_text("keep\n")

            with self.assertRaises(pull_transaction.PullJournalError):
                journal.cleanup_precommit()

            self.assertTrue((stage / "foreign").exists())

    def test_staged_recovery_revalidates_a_crash_after_raw_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "instances"
            parent.mkdir()
            journal = pull_transaction.PullJournal.open_or_create(
                root / "home",
                kind="single",
                server="https://glass.example",
                credential="scs_live_secret_credential",
            )
            _initialize(journal, parent)
            item = journal.items[0]
            stage = journal.prepare_stage(item)
            journal.update_item(0, staged=True)
            journal.set_state("STAGED")
            final = Path(item["final_path"])
            os.rename(stage, final)

            with (
                mock.patch.object(sync, "_verify_staged_pull") as verify,
                mock.patch.object(
                    sync.runtime_contract,
                    "verify_local_interface_install",
                ) as verify_interface,
            ):
                sync._stage_pull_items(
                    journal,
                    {item["silicon_id"]: {"silicon_id": item["silicon_id"]}},
                    {item["silicon_id"]: "scs_live_secret_credential"},
                    SimpleNamespace(),
                )

            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[0], final)
            verify_interface.assert_called_once_with(final)


class RegistryDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_dir = registry.REGISTRY_DIR
        self.old_file = registry.REGISTRY_FILE
        registry.REGISTRY_DIR = self.root / ".silicon"
        registry.REGISTRY_FILE = registry.REGISTRY_DIR / "registry.json"

    def tearDown(self):
        registry.REGISTRY_DIR = self.old_dir
        registry.REGISTRY_FILE = self.old_file
        self.temporary.cleanup()

    def test_concurrent_registration_loses_no_rows(self):
        errors: list[Exception] = []

        def register_one(index: int):
            try:
                registry.register(
                    f"silicon-{index}", str(self.root / f"silicon-{index}")
                )
            except Exception as exc:  # pragma: no cover - assertion reports it.
                errors.append(exc)

        threads = [
            threading.Thread(target=register_one, args=(index,))
            for index in range(24)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(registry.installs()), 24)
        json.loads(registry.REGISTRY_FILE.read_text(encoding="utf-8"))

    def test_registration_is_idempotent_but_identity_collision_fails(self):
        path = self.root / "ada"
        self.assertEqual(registry.register("ada", str(path)), "added")
        self.assertEqual(registry.register("ada", str(path)), "exists")
        with self.assertRaises(registry.RegistryConflict):
            registry.register("ada", str(self.root / "other"))
        self.assertEqual(len(registry.installs()), 1)


class PullOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_dir = registry.REGISTRY_DIR
        self.old_file = registry.REGISTRY_FILE
        registry.REGISTRY_DIR = self.root / ".silicon"
        registry.REGISTRY_FILE = registry.REGISTRY_DIR / "registry.json"

    def tearDown(self):
        registry.REGISTRY_DIR = self.old_dir
        registry.REGISTRY_FILE = self.old_file
        self.temporary.cleanup()

    def test_docker_pull_verifies_extend_before_marking_instance_started(self):
        final = self.root / "instances" / "ada"
        final.mkdir(parents=True)
        item = {
            "name": "ada",
            "final_path": str(final),
            "registered": False,
            "interface_attempted": False,
            "started": False,
            "backup_attempted": False,
        }

        class Journal:
            def __init__(self):
                self.state = "CLAIM_COMMITTED"
                self.value = {"runtime": "docker", "backups": False}
                self.items = [item]

            def set_state(self, state):
                self.state = state

            def verify_marker(self, _root, _item):
                return None

            def update_item(self, index, **values):
                self.items[index].update(values)

        journal = Journal()
        install = registry.Install(
            0,
            "ada",
            str(final),
            str(final / ".silicon.pid"),
            "docker",
        )
        events = []
        with (
            mock.patch.object(
                sync,
                "_register_pulled_item",
                return_value=install,
            ),
            mock.patch.object(
                sync.process,
                "start_one",
                side_effect=lambda name: events.append(("start", name)),
            ),
            mock.patch.object(
                sync.registry,
                "find",
                return_value=install,
            ),
            mock.patch.object(
                sync.process,
                "install_is_running",
                return_value=True,
            ),
            mock.patch.object(
                sync.docker_runtime,
                "verify_silicon_extend",
                side_effect=lambda row: events.append(("extend", row.name)),
            ) as verify_extend,
            mock.patch.object(sync.ui, "info"),
        ):
            sync._finish_pull(journal)

        self.assertEqual(
            events,
            [("start", "ada"), ("extend", "ada")],
        )
        verify_extend.assert_called_once_with(install)
        self.assertTrue(item["started"])
        self.assertEqual(journal.state, "COMPLETE")

    def test_docker_pull_stays_incomplete_when_extend_verification_fails(self):
        final = self.root / "instances" / "ada"
        final.mkdir(parents=True)
        item = {
            "name": "ada",
            "final_path": str(final),
            "registered": False,
            "interface_attempted": False,
            "started": False,
            "backup_attempted": False,
        }

        class Journal:
            def __init__(self):
                self.state = "CLAIM_COMMITTED"
                self.value = {"runtime": "docker", "backups": False}
                self.items = [item]

            def set_state(self, state):
                self.state = state

            def verify_marker(self, _root, _item):
                return None

            def update_item(self, index, **values):
                self.items[index].update(values)

        journal = Journal()
        install = registry.Install(
            0,
            "ada",
            str(final),
            str(final / ".silicon.pid"),
            "docker",
        )
        with (
            mock.patch.object(sync, "_register_pulled_item", return_value=install),
            mock.patch.object(sync.process, "start_one"),
            mock.patch.object(sync.registry, "find", return_value=install),
            mock.patch.object(
                sync.process,
                "install_is_running",
                return_value=True,
            ),
            mock.patch.object(
                sync.docker_runtime,
                "verify_silicon_extend",
                side_effect=RuntimeError("Extend is unavailable"),
            ),
            mock.patch.object(sync.ui, "info"),
            self.assertRaisesRegex(RuntimeError, "Extend is unavailable"),
        ):
            sync._finish_pull(journal)

        self.assertFalse(item["started"])
        self.assertEqual(journal.state, "POSTCOMMIT")

    def test_team_plan_reserves_all_deterministic_names_before_mutation(self):
        parent = self.root / "instances"
        parent.mkdir()
        journal = pull_transaction.PullJournal.open_or_create(
            registry.REGISTRY_DIR,
            kind="team",
            server="https://glass.example",
            credential="sct_live_secret",
        )
        silicons = [
            {"silicon_id": "ada-team-000001", "name": "Engineer"},
            {"silicon_id": "grace-team-000002", "name": "Engineer"},
        ]

        items = sync._plan_pull_items(
            journal,
            silicons,
            {
                "ada-team-000001": {"brain": "claude"},
                "grace-team-000002": {"brain": "codex"},
            },
            parent,
        )

        self.assertEqual(len({item["name"] for item in items}), 2)
        self.assertFalse(any(Path(item["stage_path"]).exists() for item in items))
        self.assertFalse(any(Path(item["final_path"]).exists() for item in items))

    def test_staged_team_commit_has_no_visible_partial_or_duplicate_registry(self):
        parent = self.root / "instances"
        parent.mkdir()
        journal = pull_transaction.PullJournal.open_or_create(
            registry.REGISTRY_DIR,
            kind="team",
            server="https://glass.example",
            credential="sct_live_secret",
        )
        silicons = [
            {
                "silicon_id": "ada-1",
                "name": "Ada",
                "api_key": "scs_live_ada-secret",
            },
            {
                "silicon_id": "grace-2",
                "name": "Grace",
                "api_key": "scs_live_grace-secret",
            },
        ]
        items = sync._plan_pull_items(
            journal,
            silicons,
            {"ada-1": {}, "grace-2": {}},
            parent,
        )
        journal.initialize(
            team_name="Acme",
            runtime="local",
            runtime_image="",
            release_tree_sha256="a" * 64,
            environment_path="",
            backups=False,
            provider_key_env={},
            items=items,
        )
        prepared = SimpleNamespace(
            release=SimpleNamespace(
                manifest=SimpleNamespace(
                    identity=SimpleNamespace(tree_sha256="a" * 64)
                )
            )
        )

        def fake_hydrate(target, **_kwargs):
            target = Path(target)
            (target / "silicon.json").touch(exist_ok=True)

        with (
            mock.patch.object(sync.stemcell, "hydrate", side_effect=fake_hydrate),
            mock.patch.object(sync, "_verify_staged_pull"),
            mock.patch.object(
                sync.interface_cli,
                "setup",
                return_value=True,
            ) as setup_interface,
            mock.patch.object(sync.process, "start_one"),
            mock.patch.object(
                sync.process, "install_is_running", return_value=True
            ),
            mock.patch.object(
                sync, "_close_team_pull_claim", return_value=(True, "")
            ) as close_claim,
            mock.patch.object(
                sync, "_refresh_team_pull_claim_credentials"
            ),
        ):
            sync._execute_planned_pull(
                journal,
                prepared=prepared,
                silicons=silicons,
                team_key="sct_live_secret",
            )

        self.assertEqual(journal.state, "COMPLETE")
        self.assertEqual(
            setup_interface.call_args_list[:2],
            [
                mock.call(
                    Path(items[0]["stage_path"]),
                    required=True,
                    start_daemon=False,
                ),
                mock.call(
                    Path(items[1]["stage_path"]),
                    required=True,
                    start_daemon=False,
                ),
            ],
        )
        self.assertEqual(len(registry.installs()), 2)
        self.assertFalse(
            any(Path(item["stage_path"]).exists() for item in items)
        )
        self.assertTrue(
            all(Path(item["final_path"]).is_dir() for item in items)
        )
        close_claim.assert_called_once()
        journal_text = journal.path.read_text(encoding="utf-8")
        self.assertNotIn("sct_live_secret", journal_text)
        self.assertNotIn("scs_live_ada-secret", journal_text)
        self.assertNotIn("scs_live_grace-secret", journal_text)

        # Retrying the post-commit registration phase converges on the exact
        # same two identities.
        journal.value["state"] = "CLAIM_COMMITTED"
        for item in journal.items:
            item["registered"] = False
            item["interface_attempted"] = False
            item["started"] = False
        journal.save()
        with (
            mock.patch.object(sync.interface_cli, "setup", return_value=True),
            mock.patch.object(sync.process, "start_one"),
            mock.patch.object(
                sync.process, "install_is_running", return_value=True
            ),
        ):
            sync._finish_pull(journal)
        self.assertEqual(len(registry.installs()), 2)

    def test_ambiguous_claim_commit_resumes_after_atomic_rename(self):
        parent = self.root / "instances"
        parent.mkdir()
        journal = pull_transaction.PullJournal.open_or_create(
            registry.REGISTRY_DIR,
            kind="team",
            server="https://glass.example",
            credential="sct_live_secret",
        )
        silicon = {
            "silicon_id": "ada-1",
            "name": "Ada",
            "api_key": "scs_live_ada-secret",
        }
        items = sync._plan_pull_items(
            journal, [silicon], {"ada-1": {}}, parent
        )
        journal.initialize(
            team_name="Acme",
            runtime="local",
            runtime_image="",
            release_tree_sha256="a" * 64,
            environment_path="",
            backups=False,
            provider_key_env={},
            items=items,
        )
        prepared = SimpleNamespace(
            release=SimpleNamespace(
                manifest=SimpleNamespace(
                    identity=SimpleNamespace(tree_sha256="a" * 64)
                )
            )
        )

        with (
            mock.patch.object(sync.stemcell, "hydrate"),
            mock.patch.object(sync, "_verify_staged_pull"),
            mock.patch.object(
                sync,
                "_close_team_pull_claim",
                return_value=(False, "response lost"),
            ),
            mock.patch.object(
                sync, "_refresh_team_pull_claim_credentials"
            ),
            self.assertRaisesRegex(RuntimeError, "rerun the same pull"),
        ):
            sync._execute_planned_pull(
                journal,
                prepared=prepared,
                silicons=[silicon],
                team_key="sct_live_secret",
            )

        self.assertEqual(journal.state, "RENAMED")
        self.assertTrue(Path(items[0]["final_path"]).is_dir())
        self.assertFalse(Path(items[0]["stage_path"]).exists())
        self.assertEqual(registry.installs(), [])

        with (
            mock.patch.object(
                sync,
                "_close_team_pull_claim",
                return_value=(True, ""),
            ),
            mock.patch.object(
                sync, "_refresh_team_pull_claim_credentials"
            ),
            mock.patch.object(sync.interface_cli, "setup", return_value=True),
            mock.patch.object(sync.process, "start_one"),
            mock.patch.object(
                sync.process, "install_is_running", return_value=True
            ),
        ):
            # A committed-claim replay intentionally contains no plaintext
            # credentials. Post-rename recovery must not need them.
            sync._execute_planned_pull(
                journal,
                prepared=prepared,
                silicons=[],
                team_key="sct_live_secret",
            )

        self.assertEqual(journal.state, "COMPLETE")
        self.assertEqual(len(registry.installs()), 1)

    def test_expired_renamed_claim_refreshes_every_final_key_and_converges(self):
        parent = self.root / "instances"
        parent.mkdir()
        journal = pull_transaction.PullJournal.open_or_create(
            registry.REGISTRY_DIR,
            kind="team",
            server="https://glass.example",
            credential="sct_live_secret",
        )
        silicons = [
            {
                "silicon_id": "ada-1",
                "name": "Ada",
                "api_key": "scs_live_old_ada_credential",
            },
            {
                "silicon_id": "grace-2",
                "name": "Grace",
                "api_key": "scs_live_old_grace_credential",
            },
        ]
        items = sync._plan_pull_items(
            journal,
            silicons,
            {"ada-1": {}, "grace-2": {}},
            parent,
        )
        journal.initialize(
            team_name="Acme",
            runtime="local",
            runtime_image="",
            release_tree_sha256="a" * 64,
            environment_path="",
            backups=False,
            provider_key_env={},
            items=items,
        )
        for index, (item, silicon) in enumerate(zip(items, silicons)):
            stage = journal.prepare_stage(item)
            sync._seed_glass_files(
                stage,
                server="https://glass.example",
                api_key=silicon["api_key"],
                silicon=silicon,
                instance_name=item["name"],
            )
            journal.update_item(index, staged=True)
        journal.set_state("STAGED")
        journal.reconcile_and_commit()

        replacement_keys = {
            "ada-1": "scs_live_replacement_ada_credential",
            "grace-2": "scs_live_replacement_grace_credential",
        }
        replay = {
            "pull_transaction_id": journal.transaction_id,
            "claim_state": "pending",
            "silicons": [
                {"silicon_id": silicon_id, "api_key": api_key}
                for silicon_id, api_key in replacement_keys.items()
            ],
        }
        original_write = sync._write_private_json
        writes = 0

        def crash_during_second_refresh(path, payload):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise AbruptCrash()
            original_write(path, payload)

        with (
            mock.patch.object(
                sync,
                "_post_json_with_team_key",
                return_value=(200, replay),
            ),
            mock.patch.object(
                sync,
                "_write_private_json",
                side_effect=crash_during_second_refresh,
            ),
            self.assertRaises(AbruptCrash),
        ):
            sync._refresh_team_pull_claim_credentials(
                journal, "sct_live_secret"
            )

        # Replaying the same claim returns the same replacement keys, so a
        # crash after any individual atomic secret write safely converges.
        with mock.patch.object(
            sync,
            "_post_json_with_team_key",
            return_value=(200, replay),
        ):
            sync._refresh_team_pull_claim_credentials(
                journal, "sct_live_secret"
            )

        for item, old in zip(items, silicons):
            final = Path(item["final_path"])
            glass_path = final / ".glass.json"
            glass = json.loads(glass_path.read_text(encoding="utf-8"))
            self.assertEqual(
                glass["api_key"], replacement_keys[item["silicon_id"]]
            )
            self.assertNotIn(
                old["api_key"], final.joinpath(".env").read_text(encoding="utf-8")
            )
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(glass_path.stat().st_mode), 0o600
                )
        journal_text = journal.path.read_text(encoding="utf-8")
        for key in [
            *(silicon["api_key"] for silicon in silicons),
            *replacement_keys.values(),
        ]:
            self.assertNotIn(key, journal_text)

    def test_hydration_failure_cleans_stages_and_aborts_claim(self):
        parent = self.root / "instances"
        parent.mkdir()
        journal = pull_transaction.PullJournal.open_or_create(
            registry.REGISTRY_DIR,
            kind="team",
            server="https://glass.example",
            credential="sct_live_secret",
        )
        silicon = {
            "silicon_id": "ada-1",
            "name": "Ada",
            "api_key": "scs_live_ada-secret",
        }
        items = sync._plan_pull_items(
            journal, [silicon], {"ada-1": {}}, parent
        )
        journal.initialize(
            team_name="Acme",
            runtime="local",
            runtime_image="",
            release_tree_sha256="a" * 64,
            environment_path="",
            backups=False,
            provider_key_env={},
            items=items,
        )
        prepared = SimpleNamespace(
            release=SimpleNamespace(
                manifest=SimpleNamespace(
                    identity=SimpleNamespace(tree_sha256="a" * 64)
                )
            )
        )
        with (
            mock.patch.object(
                sync.stemcell,
                "hydrate",
                side_effect=RuntimeError("dependency failure"),
            ),
            mock.patch.object(
                sync,
                "_close_team_pull_claim",
                return_value=(True, ""),
            ) as close_claim,
            self.assertRaisesRegex(RuntimeError, "dependency failure"),
        ):
            sync._execute_planned_pull(
                journal,
                prepared=prepared,
                silicons=[silicon],
                team_key="sct_live_secret",
            )

        self.assertEqual(journal.state, "ABORTED")
        self.assertFalse(Path(items[0]["stage_path"]).exists())
        self.assertFalse(Path(items[0]["final_path"]).exists())
        self.assertEqual(
            close_claim.call_args.args[2]
            if len(close_claim.call_args.args) > 2
            else close_claim.call_args.args[-1],
            "abort",
        )


if __name__ == "__main__":
    unittest.main()
