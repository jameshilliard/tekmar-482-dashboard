import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from tekmar_482 import (
    DeviceTraffic,
    DiscoveredDevice,
    FrameCounts,
    GatewayEndpoint,
    NativeFrame,
    NativePath,
    NormalTraceState,
    Packet,
    PortTraffic,
    Tekmar482Client,
    TrafficCounters,
    TrpcPacket,
)

from tekmar_482_dashboard.app import create_app
from tekmar_482_dashboard.devices import Inventory, controls
from tekmar_482_dashboard.feed import Feed
from tekmar_482_dashboard.gateway import Gateway
from tekmar_482_dashboard.main import operator_key
from tekmar_482_dashboard.native_layout import native_spans
from tekmar_482_dashboard.operations import arguments, display_result
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

    def test_cli_results_preserve_large_integers_and_plain_output(self) -> None:
        self.assertEqual(
            display_result(
                '{\n"presses": 18446744073709551615, "small": 9007199254740991\n}'
            ),
            {"presses": "18446744073709551615", "small": 9007199254740991},
        )
        self.assertEqual(
            display_result('{"count":9007199254740993}\n{"count":0}'),
            [{"count": "9007199254740993"}, {"count": 0}],
        )
        self.assertEqual(display_result("No response"), "No response")
        self.assertEqual(display_result(""), "")

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

    def test_hardware_traffic_is_read_only_and_json_keeps_u64_precision(self) -> None:
        client, gw = self.make_client()
        self.assertEqual(client.get("/api/traffic/ports").status_code, 409)
        reader = AsyncMock(spec=TrafficCounters)
        gw.traffic = reader
        count = FrameCounts(2**64 - 1, 2**56, 1, 0)
        reader.port.side_effect = [PortTraffic(p, count, 1, 0, 2) for p in range(5)]
        result = client.get("/api/traffic/ports").json()
        self.assertEqual(result[4]["rx_valid"], str(2**64 - 1))
        self.assertEqual(result[0]["tx_attempts"], str(2**56 + 1))
        path = NativePath(2, 0x42, 1)
        reader.device.return_value = DeviceTraffic(path, count)
        self.assertEqual(client.get("/api/traffic/2/66/1").json()["tx_ack"], str(2**56))
        reader.device.assert_awaited_once_with(path)
        self.assertEqual(client.get("/api/traffic/2/66/32").status_code, 422)

    def test_trace_drop_totals_are_exact_at_the_browser_boundary(self) -> None:
        client, gw = self.make_client()
        gw.feed.status["connection"] = "online"
        gw.client = AsyncMock(spec=Tekmar482Client)
        drops = (0, 1, 2**53 - 1, 2**53 + 1, 2**64 - 1)
        with patch("tekmar_482_dashboard.gateway.BridgeManagement", autospec=True) as m:
            m.return_value.trace_state.return_value = NormalTraceState(31, drops)
            resp = client.post(
                "/api/operations", json={"name": "trace-state", "confirmed": True}
            )
            m.return_value.trace_state.assert_awaited_once_with()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["result"],
            {"port_mask": 31, "gateway_drops": list(map(str, drops))},
        )

    def test_structured_tool_descriptors_include_subcommands_and_known_registers(
        self,
    ) -> None:
        client, _ = self.make_client()
        spec = client.get("/api/catalog").json()
        cmd = next(c for c in spec["commands"] if c["name"] == "read-status")
        variants = cmd["parameters"][0]["variants"]
        self.assertIn("selector", [p["name"] for p in variants["humidity"]])
        self.assertTrue(all(b["fields"] for b in spec["blocks"]))
        self.assertIn(
            {"name": "address", "width": 2},
            spec["method_fields"]["CURRENT_TEMPERATURE"],
        )

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
