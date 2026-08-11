from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from silicon_cli import registry, update
from silicon_cli.updater.engine import TransactionalUpdater
from silicon_cli.updater.maintenance import MaintenanceTimeout
from tests.test_transactional_updater import Fixture


class WorkConservingFleetSchedulerTests(unittest.TestCase):
    @staticmethod
    def _prepared(names: tuple[str, ...]):
        prepared = []
        for index, name in enumerate(names):
            install = registry.Install(
                index=index,
                name=name,
                path=f"/tmp/silicon-fleet-test-{name}",
                pid_file=f"/tmp/silicon-fleet-test-{name}/.silicon.pid",
            )
            prepared.append((install, mock.Mock(), {"member": name}))
        return prepared

    def test_busy_drain_is_parked_while_pending_member_backfills_slot(self):
        prepared = self._prepared(("busy", "steady", "replacement", "last"))
        replacement_started = threading.Event()
        started: list[str] = []
        started_lock = threading.Lock()

        for install, updater, _preflight in prepared:
            name = install.name

            def run(_release, *, _name=name, **kwargs):
                with started_lock:
                    started.append(_name)
                kwargs["on_drain_requested"]()
                if _name == "busy":
                    if not replacement_started.wait(timeout=2):
                        raise AssertionError("busy drain was never backfilled")
                gate = kwargs["activation_gate"]
                if _name == "replacement":
                    replacement_started.set()
                if not gate.acquire(timeout=2):
                    raise AssertionError("activation gate was not released")
                try:
                    kwargs["on_activation_slot"]()
                    if _name == "steady" and not replacement_started.wait(
                        timeout=2
                    ):
                        raise AssertionError(
                            "steady activation blocked without backfill"
                        )
                    time.sleep(0.01)
                    return {"transaction_id": f"tx-{_name}"}
                finally:
                    kwargs["on_activation_slot_released"]()
                    gate.release()

            updater.run.side_effect = run

        fleet = mock.Mock()
        committed = []
        failures, remaining, stats = (
            update._activate_local_fleet_work_conserving(
                prepared,
                list(range(len(prepared))),
                object(),
                fleet,
                committed,
                concurrency=2,
                deadline_seconds=1.0,
                drain_probe_seconds=0.01,
            )
        )

        self.assertEqual(failures, 0)
        self.assertEqual(remaining, [])
        self.assertEqual(len(committed), 4)
        self.assertLess(started.index("replacement"), started.index("last"))
        self.assertLessEqual(stats["peak_productive"], 2)
        self.assertEqual(stats["peak_parked"], 1)
        self.assertGreaterEqual(stats["peak_in_flight"], 3)
        self.assertLessEqual(stats["peak_in_flight"], 4)
        self.assertIn(
            mock.call(0, state="draining", error=""),
            fleet.member.call_args_list,
        )

    def test_eight_productive_slots_backfill_four_busy_drains(self):
        names = tuple(
            [f"busy-{index}" for index in range(4)]
            + [f"steady-{index}" for index in range(4)]
            + [f"replacement-{index}" for index in range(4)]
        )
        prepared = self._prepared(names)
        replacements_started = threading.Event()
        replacement_count = 0
        replacement_lock = threading.Lock()

        for install, updater, _preflight in prepared:
            name = install.name

            def run(_release, *, _name=name, **kwargs):
                nonlocal replacement_count
                kwargs["on_drain_requested"]()
                if _name.startswith("busy-"):
                    if not replacements_started.wait(timeout=2):
                        raise AssertionError("four busy drains were not backfilled")
                gate = kwargs["activation_gate"]
                if not gate.acquire(timeout=2):
                    raise AssertionError("activation gate was not released")
                try:
                    kwargs["on_activation_slot"]()
                    if _name.startswith("replacement-"):
                        with replacement_lock:
                            replacement_count += 1
                            if replacement_count == 4:
                                replacements_started.set()
                    elif _name.startswith("steady-"):
                        if not replacements_started.wait(timeout=2):
                            raise AssertionError(
                                "steady slots completed before fleet backfill"
                            )
                    time.sleep(0.01)
                    return {"transaction_id": f"tx-{_name}"}
                finally:
                    kwargs["on_activation_slot_released"]()
                    gate.release()

            updater.run.side_effect = run

        failures, remaining, stats = (
            update._activate_local_fleet_work_conserving(
                prepared,
                list(range(len(prepared))),
                object(),
                mock.Mock(),
                [],
                concurrency=8,
                deadline_seconds=1.0,
                drain_probe_seconds=0.01,
            )
        )

        self.assertEqual(failures, 0)
        self.assertEqual(remaining, [])
        self.assertEqual(replacement_count, 4)
        self.assertLessEqual(stats["peak_productive"], 8)
        self.assertEqual(stats["peak_parked"], 4)
        self.assertGreaterEqual(stats["peak_in_flight"], 12)
        self.assertLessEqual(stats["peak_in_flight"], 16)

    def test_full_parked_cap_does_not_begin_draining_the_whole_fleet(self):
        prepared = self._prepared(("busy-one", "busy-two", "three", "four"))

        for _install, updater, _preflight in prepared[:2]:
            def remain_busy(_release, **kwargs):
                kwargs["on_drain_requested"]()
                time.sleep(0.08)
                raise MaintenanceTimeout("bounded drain expired")

            updater.run.side_effect = remain_busy

        fleet = mock.Mock()
        failures, remaining, stats = (
            update._activate_local_fleet_work_conserving(
                prepared,
                list(range(len(prepared))),
                object(),
                fleet,
                [],
                concurrency=2,
                deadline_seconds=0.05,
                drain_probe_seconds=0.01,
            )
        )

        self.assertGreaterEqual(failures, 1)
        self.assertEqual(stats["peak_parked"], 2)
        self.assertEqual(stats["peak_in_flight"], 2)
        prepared[2][1].run.assert_not_called()
        prepared[3][1].run.assert_not_called()
        self.assertCountEqual(remaining, [0, 1, 2, 3])


class ActivationGateEngineTests(unittest.TestCase):
    def test_quiescent_runtime_waits_for_gate_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=fixture.hooks(),
            )
            gate = threading.BoundedSemaphore(1)
            gate.acquire()
            drained = threading.Event()
            activated = threading.Event()

            with self.assertRaises(MaintenanceTimeout):
                updater.run(
                    fixture.release,
                    deadline=time.time() + 0.03,
                    activation_gate=gate,
                    on_drain_requested=drained.set,
                    on_activation_slot=activated.set,
                )

            self.assertTrue(drained.is_set())
            self.assertFalse(activated.is_set())
            self.assertNotIn("checkpoint", fixture.events)
            self.assertTrue(fixture.services["main"])
            self.assertIsNone(updater.status()["active_transaction"])
            gate.release()

    def test_activation_gate_is_released_after_success(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = Fixture(Path(raw))
            updater = TransactionalUpdater(
                fixture.instance,
                fixture.cache,
                hooks=fixture.hooks(),
            )
            gate = threading.BoundedSemaphore(1)
            activated = threading.Event()

            result = updater.run(
                fixture.release,
                activation_gate=gate,
                on_activation_slot=activated.set,
            )

            self.assertEqual(result["state"], "COMMITTED")
            self.assertTrue(activated.is_set())
            self.assertTrue(gate.acquire(blocking=False))
            gate.release()


if __name__ == "__main__":
    unittest.main()
