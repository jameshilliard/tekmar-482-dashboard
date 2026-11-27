import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from tekmar_482 import (
    DiscoveredDevice,
    GatewayEndpoint,
    NativeFrame,
    Packet,
    TrpcPacket,
)

from tekmar_482_dashboard.app import create_app
from tekmar_482_dashboard.devices import Inventory, controls
from tekmar_482_dashboard.feed import Feed
from tekmar_482_dashboard.gateway import Gateway
from tekmar_482_dashboard.main import operator_key
from tekmar_482_dashboard.native_layout import native_spans
from tekmar_482_dashboard.operations import arguments
from tekmar_482_dashboard.trace_view import native_quality, trpc_spans

ENDPOINT = GatewayEndpoint.tcp("192.0.2.1", 3001)


class FeedTests(unittest.TestCase):
    def test_every_byte_is_shown_once_and_unknown_is_not_green(self) -> None:
        for body, quality in [
            ("76f00608 00032b0a06000000", "good"),
            ("52300002 ca35", "partial"),
            ("76f00601 00", "error"),
        ]:
            frame = NativeFrame(1, 20, bytes.fromhex(body))
            fields = frame.decoded
            header, payload = native_spans(frame, fields)
            self.assertEqual(
                sum(s["size"] for g in (header, payload) for s in g["children"]),
                len(frame.body),
            )
            verdict, reason = native_quality(frame, fields, payload)
            self.assertEqual(verdict, quality)
            self.assertTrue(reason)
        packet = Packet(6, bytes.fromhex("023701000001003406"))
        spans = trpc_spans(packet, TrpcPacket.from_packet(packet))
        self.assertEqual(sum(s["size"] for s in spans), len(packet.data))
        self.assertEqual(spans[2]["label"], "address: 1")

    def test_loss_and_device_metadata_are_distinct(self) -> None:
        feed = Feed(3)
        for _ in range(7):
            feed.publish("tn4", "rx", "Unknown", port=4)
        state = feed.snapshot(1)
        self.assertEqual((state["type"], state["skipped"]), ("snapshot", 3))
        self.assertEqual(state["ports"][4]["rx"], 7)
        self.assertEqual(feed.snapshot(6)["events"][0]["seq"], 7)
        frame = NativeFrame(1, 10, bytes.fromhex("76f00608 00032b0a06000000"))
        key = feed.observe(frame, frame.decoded)
        self.assertIsNotNone(feed.peers[key]["temperature_f"])
        self.assertIsNone(feed.peers[key]["model"])
        feed.gap(2)
        self.assertFalse(feed.peers)
        self.assertIn("2 records", feed.events[-1]["reason"])

    def test_server_owns_transport_and_apply(self) -> None:
        for words in (
            ["--ser", "elsewhere"],
            ["--tcp=127.0.0.1:80"],
            ["--confirm"],
            ["--auto-update"],
            ["--help"],
        ):
            with self.assertRaises(ValueError):
                arguments(ENDPOINT, False, "panel", words, False)
        with self.assertRaises(ValueError):
            arguments(ENDPOINT, False, "update", [], False)
        good = arguments(
            ENDPOINT, False, "set-climate-mode", ["1", "0x14", "6", "heat"], False
        )
        self.assertFalse(good.confirm)
        self.assertEqual(good.tcp, "192.0.2.1:3001")

    def test_optional_key_is_private_and_reusable(self) -> None:
        with TemporaryDirectory() as dest:
            path = Path(dest) / "operator.key"
            key = operator_key(path)
            self.assertEqual(key, operator_key(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ServerTests(unittest.TestCase):
    def make_client(self, token: str | None = None) -> tuple[TestClient, Gateway]:
        gateway = Gateway(ENDPOINT)
        self.enterContext(patch.object(gateway, "start"))
        self.enterContext(patch.object(gateway, "stop", new_callable=AsyncMock))
        app = create_app(gateway, token)
        return self.enterContext(TestClient(app)), gateway

    def test_websocket_and_read_only_pages_cannot_send_commands(self) -> None:
        client, gw = self.make_client()
        with patch.object(gw, "command", new_callable=AsyncMock) as send:
            gw.feed.publish("tn4", "rx", "AirGroupMemberStatus", port=1, hex="00")
            for url in ("/", "/api/state", "/api/catalog", "/static/instruments.js"):
                self.assertEqual(client.get(url).status_code, 200)
            with client.websocket_connect("/ws") as ws:
                self.assertEqual(
                    ws.receive_json()["events"][-1]["name"], "AirGroupMemberStatus"
                )
            send.assert_not_awaited()

    def test_no_key_by_default_but_confirmation_and_origin_required(self) -> None:
        client, gw = self.make_client()
        op = {"name": "panel", "confirmed": True}
        with patch.object(
            gw, "command", new_callable=AsyncMock, return_value="ok"
        ) as send:
            self.assertEqual(
                client.post("/api/operations", json={"name": "panel"}).status_code, 422
            )
            self.assertEqual(
                client.post(
                    "/api/operations", headers={"Origin": "http://other.test"}, json=op
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/operations", headers={"Host": "other.test"}, json=op
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post("/api/operations", json=op).json(), {"result": "ok"}
            )
            send.assert_awaited_once_with("panel", [], False)

    def test_auth_is_opt_in_and_device_apply_is_separate(self) -> None:
        client, gw = self.make_client("test-key")
        op = {
            "name": "device-setting",
            "confirmed": True,
            "values": {"address": 1, "setting": "heat", "value": 20},
        }
        self.assertEqual(client.post("/api/operations", json=op).status_code, 401)
        gw.feed.status["connection"] = "online"
        with patch.object(gw.inventory, "set", new_callable=AsyncMock) as write:
            self.assertEqual(
                client.post(
                    "/api/operations",
                    json=op,
                    headers={"Authorization": "Bearer test-key"},
                ).status_code,
                422,
            )
            write.assert_not_awaited()


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_device_edit_checks_support_and_changes_only_one_setting(
        self,
    ) -> None:
        inv = Inventory(Feed())
        dev = DiscoveredDevice(address=1, type_code=101103)
        self.assertNotIn("cool", controls(dev))
        client = AsyncMock()
        inv.devices[1] = dev
        with self.assertRaises(ValueError):
            await inv.set(client, {"address": 1, "setting": "cool", "value": 123})
        client.set_latest_cool_setpoint_celsius.assert_not_awaited()

    async def test_inventory_always_uses_request_only_options(self) -> None:
        inv = Inventory(Feed())
        from tekmar_482 import DiscoveryResult, GatewayInfo

        client = AsyncMock()
        client.discover.return_value = DiscoveryResult(GatewayInfo(), ())
        client.poll_devices.return_value = ()
        await inv.refresh(client)
        client.discover.assert_awaited_once_with(
            timeout=60, manage_reporting=False, setback_enable=None
        )
        client.update_response.assert_not_awaited()
        self.assertEqual(inv.feed.status["inventory"], "ready")
