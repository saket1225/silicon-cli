from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from silicon_cli.updater.maintenance import (
    GlassMaintenanceLease,
    MaintenanceError,
    MaintenanceProtocol,
)


class Response:
    def __init__(self, value):
        self.value = value

    def read(self, *_args):
        return json.dumps(self.value).encode()


class RawResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, *_args):
        return self.payload


class GlassLeaseTests(unittest.TestCase):
    def test_required_transition_retries_transient_glass_502(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            calls = 0

            def open_request(request, **_kwargs):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        502,
                        "Bad Gateway",
                        {},
                        io.BytesIO(b"temporary upstream failure"),
                    )
                payload = json.loads(request.data)
                return Response({"active": True, **payload})

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            with mock.patch(
                "silicon_cli.updater.maintenance.time.sleep"
            ) as sleep:
                lease.begin("update-retry", "2.0.0")
            lease.finish("cancelled")

            self.assertEqual(calls, 4)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [0.25, 0.5],
            )

    def test_rejects_oversized_and_non_object_glass_responses(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            oversized = GlassMaintenanceLease(
                instance,
                opener=lambda *_args, **_kwargs: RawResponse(
                    b"x" * (1024 * 1024 + 1)
                ),
            )
            with self.assertRaisesRegex(MaintenanceError, "size limit"):
                oversized.begin("update-big", "2.0.0")

            malformed = GlassMaintenanceLease(
                instance,
                opener=lambda *_args, **_kwargs: RawResponse(b"[]"),
            )
            with self.assertRaisesRegex(MaintenanceError, "invalid"):
                malformed.begin("update-list", "2.0.0")

    def test_validates_source_ack_when_public_revision_diverges_and_idle_clears(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            responses = []

            def open_request(request, **_kwargs):
                payload = json.loads(request.data)
                response = {
                    "active": payload["phase"] != "idle",
                    "delivery_deferred": False,
                    "phase": payload["phase"],
                    # Carbon queue changes own this public UI revision.
                    "revision": payload["revision"] + 100,
                    "source_revision": payload["revision"],
                    "accepted_update_id": payload["update_id"],
                    "accepted_revision": payload["revision"],
                }
                if payload["phase"] != "idle":
                    response["update_id"] = payload["update_id"]
                responses.append(response)
                return Response(response)

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("update-ack", "2.0.0")
            lease.report("draining")
            lease.finish("committed")

            self.assertEqual(responses[-1]["phase"], "idle")
            self.assertNotIn("update_id", responses[-1])
            self.assertGreater(
                responses[1]["revision"],
                responses[1]["accepted_revision"],
            )
            self.assertFalse(
                (
                    instance
                    / ".silicon"
                    / "maintenance"
                    / "glass-pending.json"
                ).exists()
            )

    def test_reports_monotonic_authenticated_phases_and_clears_idle(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            requests = []

            def open_request(request, **_kwargs):
                payload = json.loads(request.data)
                requests.append((request, payload))
                return Response({"active": payload["phase"] != "idle", **payload})

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("update-01", "2.0.0")
            lease.report("draining")
            lease.report("updating")
            lease.finish("committed")

            self.assertEqual(
                [payload["phase"] for _, payload in requests],
                ["preparing", "draining", "updating", "resuming", "idle"],
            )
            revisions = [payload["revision"] for _, payload in requests]
            self.assertEqual(revisions, sorted(set(revisions)))
            self.assertTrue(
                all(
                    request.get_header("X-silicon-key") == "scs_live_test"
                    for request, _ in requests
                )
            )

    def test_terminal_network_failure_is_durably_retryable(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            calls = 0

            def open_request(request, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    payload = json.loads(request.data)
                    return Response({"active": True, **payload})
                raise OSError("offline")

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("update-02", "2.0.0")
            lease.finish("rolled_back")
            pending = (
                instance / ".silicon" / "maintenance" / "glass-pending.json"
            )
            self.assertTrue(pending.is_file())
            self.assertEqual(json.loads(pending.read_text())["phase"], "rolled_back")

    def test_failed_resuming_notice_cannot_block_terminal_idle(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            phases = []

            def open_request(request, **_kwargs):
                payload = json.loads(request.data)
                phases.append(payload["phase"])
                if payload["phase"] == "resuming":
                    raise OSError("dropped resuming response")
                return Response(
                    {"active": payload["phase"] != "idle", **payload}
                )

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("update-03", "2.0.0")
            lease.finish("committed")

            self.assertEqual(phases[-1], "idle")
            self.assertFalse(
                (
                    instance
                    / ".silicon"
                    / "maintenance"
                    / "glass-pending.json"
                ).exists()
            )

    def test_pending_idle_reconciles_after_connectivity_recovers(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            online = False
            projection = {
                "active": False,
                "phase": "idle",
                "update_id": "",
                "revision": 0,
            }

            def open_request(request, **_kwargs):
                nonlocal projection
                if request.data is None:
                    if not online:
                        raise OSError("offline")
                    return Response(projection)
                payload = json.loads(request.data)
                if payload["phase"] == "idle" and not online:
                    raise OSError("offline")
                projection = {
                    "active": payload["phase"] != "idle",
                    **payload,
                }
                return Response(projection)

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("update-reconcile", "2.0.0")
            lease.finish("committed")
            pending = (
                instance / ".silicon" / "maintenance" / "glass-pending.json"
            )
            self.assertTrue(pending.is_file())
            pending_value = json.loads(pending.read_text(encoding="utf-8"))

            online = True
            self.assertTrue(lease.reconcile_pending_terminal())
            self.assertFalse(pending.exists())
            self.assertEqual(projection["phase"], "idle")
            self.assertEqual(projection["update_id"], "update-reconcile")
            self.assertEqual(
                projection["revision"], pending_value["revision"]
            )

    def test_refuses_credentials_over_an_unsafe_origin(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "http://glass.example",
                        "api_key": "secret",
                    }
                )
            )
            with self.assertRaises(MaintenanceError):
                GlassMaintenanceLease(instance)

    def test_refuses_a_cross_origin_redirect_response(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "secret",
                    }
                )
            )

            class RedirectedResponse(Response):
                def geturl(self):
                    return "https://attacker.example/maintenance"

            def open_request(request, **_kwargs):
                payload = json.loads(request.data)
                return RedirectedResponse({"phase": payload["phase"]})

            lease = GlassMaintenanceLease(instance, opener=open_request)
            with self.assertRaisesRegex(
                MaintenanceError, "cross-origin maintenance redirect"
            ):
                lease.begin("update-redirect", "2.0.0")

    def test_failed_pending_replay_cannot_leak_old_id_into_new_begin(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.example",
                        "api_key": "scs_live_test",
                    }
                )
            )
            pending = (
                instance / ".silicon" / "maintenance" / "glass-pending.json"
            )
            pending.parent.mkdir(parents=True)
            pending.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "update_id": "old-update",
                        "target_version": "old",
                        "phase": "failed",
                        "revision": 9,
                        "queued_count": 0,
                        "lease_seconds": 120,
                    }
                )
            )
            payloads = []

            def open_request(request, **_kwargs):
                payload = json.loads(request.data)
                payloads.append(payload)
                if payload["update_id"] == "old-update":
                    raise OSError("old terminal retry failed")
                return Response({"active": True, **payload})

            lease = GlassMaintenanceLease(
                instance, opener=open_request, heartbeat_seconds=3600
            )
            lease.begin("new-update", "new")
            lease.finish("cancelled")
            preparing = next(
                payload for payload in payloads if payload["phase"] == "preparing"
            )
            self.assertEqual(preparing["update_id"], "new-update")
            self.assertEqual(preparing["target_version"], "new")


class LocalMaintenanceProtocolTests(unittest.TestCase):
    def test_expired_owned_fence_accepts_rolled_back_completion(self):
        with tempfile.TemporaryDirectory() as raw:
            calls = []

            def command(arguments):
                calls.append(arguments)
                if arguments[:2] == ["phase", "rolling_back"]:
                    raise MaintenanceError(
                        "invalid maintenance transition: available -> rolling_back"
                    )
                if arguments == ["status"]:
                    return {
                        "maintenance_id": "update-1",
                        "phase": "available",
                        "active_count": 0,
                        "last_outcome": "deadline_expired",
                    }
                return {}

            protocol = MaintenanceProtocol(Path(raw), command=command)
            protocol.set_phase("update-1", "rolled_back", "")

            self.assertEqual(
                calls,
                [
                    ["phase", "rolling_back", "--id", "update-1"],
                    ["status"],
                ],
            )

    def test_expired_foreign_fence_rejects_rolled_back_completion(self):
        with tempfile.TemporaryDirectory() as raw:
            def command(arguments):
                if arguments[:2] == ["phase", "rolling_back"]:
                    raise MaintenanceError("foreign fence")
                return {
                    "maintenance_id": "other-update",
                    "phase": "available",
                    "active_count": 0,
                }

            protocol = MaintenanceProtocol(Path(raw), command=command)
            with self.assertRaisesRegex(MaintenanceError, "foreign fence"):
                protocol.set_phase("update-1", "rolled_back", "")

    def test_wait_requires_same_id_epoch_and_a_revalidated_safe_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            calls = []
            statuses = [
                {
                    "maintenance_id": "update-1",
                    "epoch": 8,
                    "phase": "draining",
                    "safe_to_stop": False,
                    "active_count": 1,
                },
                {
                    "maintenance_id": "update-1",
                    "epoch": 8,
                    "phase": "draining",
                    "safe_to_stop": True,
                    "active_count": 0,
                },
            ]

            def command(arguments):
                calls.append(arguments)
                if arguments[0] == "request":
                    return {
                        "maintenance_id": "update-1",
                        "epoch": 8,
                        "queued_message_count": 2,
                    }
                if arguments[0] == "status":
                    return statuses.pop(0)
                return {"phase": arguments[1] if len(arguments) > 1 else "available"}

            class NoGlass:
                def begin(self, *_args):
                    pass

                def set_queued_count(self, *_args):
                    pass

                def report(self, *_args, **_kwargs):
                    pass

                def finish(self, *_args):
                    pass

            protocol = MaintenanceProtocol(
                Path(raw),
                command=command,
                glass=NoGlass(),
                poll_interval_seconds=2.0,
            )
            protocol.begin("update-1", "2.0.0")
            protocol.request_drain("update-1", time.time() + 10)
            with mock.patch(
                "silicon_cli.updater.maintenance.time.sleep"
            ) as sleep:
                protocol.await_quiescent(
                    "update-1",
                    time.time() + 10,
                    lambda: False,
                    services_running=True,
                )
            sleep.assert_called_once_with(2.0)
            protocol.set_phase("update-1", "updating", "")
            self.assertGreaterEqual(
                sum(1 for call in calls if call[0] == "status"), 2
            )
            self.assertIn(
                ["phase", "updating", "--id", "update-1"], calls
            )

    def test_legacy_offline_mode_requires_every_service_already_stopped(self):
        with tempfile.TemporaryDirectory() as raw:
            instance = Path(raw)
            (instance / "core").mkdir()
            running = True

            class NoGlass:
                update_id = ""

                def begin(self, *_args):
                    pass

                def set_queued_count(self, *_args):
                    pass

                def report(self, *_args, **_kwargs):
                    pass

                def finish(self, *_args):
                    pass

                def reattach(self, *_args):
                    pass

            protocol = MaintenanceProtocol(
                instance,
                glass=NoGlass(),
                legacy_offline_safe=lambda: not running,
            )
            protocol.begin("legacy-update", "2.0.0")
            with self.assertRaisesRegex(
                MaintenanceError, "stop both Silicon"
            ):
                protocol.request_drain("legacy-update", None)
            self.assertFalse(protocol.legacy_offline)

            running = False
            protocol.request_drain("legacy-update", None)
            fence = (
                instance
                / ".silicon"
                / "maintenance"
                / "legacy-offline.json"
            )
            self.assertTrue(protocol.legacy_offline)
            self.assertTrue(fence.is_file())
            protocol.await_quiescent(
                "legacy-update",
                None,
                lambda: False,
                services_running=False,
            )

            running = True
            with self.assertRaisesRegex(
                MaintenanceError, "became active"
            ):
                protocol.await_quiescent(
                    "legacy-update",
                    None,
                    lambda: False,
                    services_running=False,
                )
            running = False
            protocol.finish("legacy-update", "committed")
            self.assertFalse(fence.exists())


if __name__ == "__main__":
    unittest.main()
