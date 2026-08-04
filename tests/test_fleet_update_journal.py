from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from silicon_cli import registry, update
from silicon_cli.updater.fleet import FleetJournal
from silicon_cli.updater.release import ReleaseIdentity


class SimulatedHostCrash(BaseException):
    pass


class FleetUpdateJournalTests(unittest.TestCase):
    @staticmethod
    def _release():
        identity = ReleaseIdentity(
            version="2.0.0",
            revision="a" * 40,
            sequence=2,
            tree_sha256="b" * 64,
            artifact_sha256="c" * 64,
            source="test",
            trust="test-fixture",
        )
        return SimpleNamespace(
            manifest=SimpleNamespace(
                identity=identity,
                runtime_image="",
            )
        )

    def test_crash_after_first_commit_is_durably_compensated(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            host_state = workspace / "host"
            installs = []
            for index, name in enumerate(("one", "two")):
                path = workspace / name
                (path / ".silicon").mkdir(parents=True)
                installs.append(
                    registry.Install(
                        index=index,
                        name=name,
                        path=str(path),
                        pid_file=str(path / ".silicon.pid"),
                    )
                )

            first = mock.Mock()
            second = mock.Mock()
            for updater in (first, second):
                updater.preflight.return_value = {
                    "safe_to_apply": True,
                    "plan": {"conflicts": []},
                }
            first.run.return_value = {"transaction_id": "tx-one"}
            second.run.side_effect = SimulatedHostCrash("power loss")
            release = self._release()

            with (
                mock.patch.object(update, "REGISTRY_DIR", host_state),
                mock.patch.object(update, "_targets", return_value=installs),
                mock.patch.object(update, "_cache", return_value=object()),
                mock.patch.object(
                    update, "_fetch_latest", return_value=release
                ),
                mock.patch.object(
                    update.registry, "installs", return_value=installs
                ),
                mock.patch.object(update, "_hooks"),
                mock.patch.object(
                    update,
                    "TransactionalUpdater",
                    side_effect=[first, second],
                ),
                mock.patch.object(update.ui, "info"),
                mock.patch.object(update.ui, "success"),
                self.assertRaises(SimulatedHostCrash),
            ):
                update.update_instance("group")

            interrupted = FleetJournal.active(host_state)
            self.assertIsNotNone(interrupted)
            self.assertEqual(
                [member["state"] for member in interrupted.value["members"]],
                ["committed", "activating"],
            )

            recovered_first = mock.Mock()
            recovered_second = mock.Mock()
            recovered_first.status.return_value = {
                "active_transaction": None
            }
            recovered_first.history.return_value = []
            recovered_first.rollback.return_value = {
                "transaction_id": "rollback-one",
                "state": "COMMITTED",
            }
            recovered_second.status.return_value = {
                "active_transaction": {
                    "transaction_id": "tx-two",
                    "metadata": {
                        "release": {
                            "tree_sha256": release.manifest.identity.tree_sha256,
                        }
                    },
                }
            }
            recovered_second.resume.return_value = {
                "transaction_id": "tx-two",
                "state": "ROLLED_BACK",
            }
            recovered_second.history.return_value = []
            with (
                mock.patch.object(update, "REGISTRY_DIR", host_state),
                mock.patch.object(
                    update.registry, "installs", return_value=installs
                ),
                mock.patch.object(update, "_cache", return_value=object()),
                mock.patch.object(update, "_hooks"),
                mock.patch.object(
                    update,
                    "TransactionalUpdater",
                    side_effect=[recovered_first, recovered_second],
                ),
            ):
                result = update._reconcile_incomplete_fleet(interrupted)

            self.assertEqual(result["state"], "COMPENSATED")
            self.assertEqual(
                [member["state"] for member in result["members"]],
                ["compensated", "compensated"],
            )
            recovered_first.rollback.assert_called_once_with(
                deadline=None,
                transaction_id="tx-one",
                lock_held=True,
            )
            recovered_second.resume.assert_called_once_with(
                "tx-two", lock_held=True
            )


if __name__ == "__main__":
    unittest.main()
