import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from tekmar_482 import CaptureGap, GatewayEndpoint, GatewayTraceGap

from tekmar_482_dashboard.app import create_app
from tekmar_482_dashboard.feed import Feed, Row
from tekmar_482_dashboard.gateway import Gateway
from tekmar_482_dashboard.history import History, TraceQuery

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


class HistoryTests(unittest.TestCase):
    def test_count_and_age_retention_are_independent_of_web_history(self) -> None:
        with (
            patch("time.time", return_value=1200) as wall,
            patch("time.monotonic", return_value=0) as clock,
        ):
            feed = Feed(capacity=1)
            feed.history = History(capacity=2, seconds=10)
            for _ in range(3):
                feed.publish("tn4", "rx", "State", port=0, hex="0000")
            feed.reset_sources()
            history = feed.history
            self.assertEqual(len(feed.events), 1)
            self.assertEqual(history.coverage()["oldest_seq"], 2)
            self.assertEqual(history.frame(2)["record"]["hex"], "0000")
            forward = history.snapshot().search(
                TraceQuery(), limit=1, before_seq=None, after_seq=0, include_hex=False
            )
            self.assertTrue(forward["history_truncated"])
            self.assertEqual(forward["next_after_seq"], 2)
            self.assertNotIn("hex", forward["records"][0])

            clock.return_value = 11
            wall.return_value = 1211
            self.assertEqual(history.coverage()["retained"], 0)
            self.assertEqual(history.coverage()["evicted_records"], 3)
            self.assertIsNone(history.frame(3)["record"])
            self.assertEqual(
                history.timeline(10, None, 12)["totals"]["ports"],
                [
                    {"port": 0, "rx": 3},
                ],
            )

    def test_timeline_keeps_loss_scoped_and_wraps_after_six_hours(self) -> None:
        with patch("time.time", return_value=1200) as wall:
            feed = Feed()
            feed.state("Connected", connection="online")
            feed.publish("tn4", "rx", "Reading", port=2)
            feed.publish("tn4", "tx", "Query", port=2, outcome="no_ack")
            feed.publish("tn4", "rx_invalid", "Invalid tN4 frame", port=3)
            feed.gap(GatewayTraceGap(9, 0b01100))
            feed.gap(GatewayTraceGap(3, 0b00010))
            feed.gap(CaptureGap(4))
            timeline = feed.history.timeline(30, 2, 4)
            self.assertTrue(timeline["history_truncated"])
            self.assertEqual(
                timeline["totals"],
                {
                    "ports": [{"port": 2, "rx": 1, "tx": 1, "tx_no_ack": 1}],
                    "gateway": {"entered_online": 1, "host_drops": 4},
                    "gateway_capture_drops": [{"ports": [2, 3], "count": "9"}],
                },
            )
            wall.return_value += 6 * 3600
            feed.publish("tn4", "rx", "New reading", port=4)
            timeline = feed.history.timeline(360, None, 12)
            self.assertLessEqual(len(timeline["buckets"]), 12)
            self.assertEqual(timeline["totals"]["ports"], [{"port": 4, "rx": 1}])
            self.assertEqual(timeline["totals"]["gateway_capture_drops"], [])

    def test_timeline_can_narrow_around_a_historical_minute(self) -> None:
        with patch("time.time", return_value=1200) as wall:
            feed = Feed()
            feed.publish("tn4", "rx", "First reading", port=0)
            wall.return_value = 1260
            feed.publish("tn4", "rx", "Second reading", port=1)
            wall.return_value = 1321
            historical = feed.history.timeline(1, None, 1, end_at=1265)
            self.assertEqual(historical["end_at"], 1260)
            self.assertEqual(historical["totals"]["ports"], [{"port": 0, "rx": 1}])
            unavailable = feed.history.timeline(1, None, 1, end_at=1200)
            self.assertTrue(unavailable["history_truncated"])
            self.assertEqual(unavailable["buckets"], [])


class McpHttpTests(unittest.TestCase):
    def server(
        self, *, token: str | None = None, enabled: bool = True
    ) -> tuple[TestClient, Gateway]:
        gateway = Gateway(GatewayEndpoint.tcp("192.0.2.1", 3001))
        self.enterContext(patch.object(gateway, "start"))
        self.enterContext(patch.object(gateway, "stop", new_callable=AsyncMock))
        app = create_app(gateway, mcp_enabled=enabled, mcp_token=token)
        return self.enterContext(
            TestClient(app, base_url="http://127.0.0.1:8482")
        ), gateway

    @staticmethod
    def call(
        client: TestClient,
        method: str,
        params: Row | None = None,
        **headers: str,
    ) -> Row:
        response = client.post(
            "/mcp",
            headers={**HEADERS, **headers},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )
        response.raise_for_status()
        return response.json()

    def tool(self, client: TestClient, name: str, **arguments: object) -> Row:
        result = self.call(
            client, "tools/call", {"name": name, "arguments": arguments}
        )["result"]
        self.assertFalse(result.get("isError", False), result)
        return result["structuredContent"]

    def test_protocol_and_read_only_tools_share_the_dashboard(self) -> None:
        client, gateway = self.server()
        result = self.call(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "diagnostics test", "version": "1"},
            },
        )
        self.assertEqual(result["result"]["serverInfo"]["name"], "tekmarshark")
        tools = self.call(client, "tools/list")["result"]["tools"]
        self.assertEqual(
            {t["name"] for t in tools},
            {
                "network_status",
                "bus_timeline",
                "trace_search",
                "trace_frame",
                "list_devices",
                "device_state",
                "gateway_errors",
            },
        )
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        gateway.feed.state("Connected", connection="online", mode="normal", trace=True)
        self.assertEqual(
            self.tool(client, "network_status")["gateway"]["mode"], "normal"
        )
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/state").json()["status"]["mode"], "normal")
        with client.websocket_connect("/ws") as socket:
            self.assertEqual(socket.receive_json()["status"]["mode"], "normal")

    def test_search_cursors_and_data_expansion(self) -> None:
        client, gateway = self.server()
        for _ in range(3):
            gateway.feed.publish(
                "tn4",
                "tx",
                "BlockRequest",
                port=2,
                message_id=727,
                peer=None,
                endpoints={"to": ["2:4A:01"]},
                hex="6d700000",
                wire_hex="ca000135",
                fields={"block": 1},
                layout=[{"label": "Header"}],
            )
        gateway.feed.publish("tn4", "rx", "Reading", port=0)
        first = self.tool(client, "trace_search", limit=1, port=2, device="2:4a:01")
        self.assertEqual(first["next_before_seq"], 3)
        self.assertEqual(first["records"][0]["message_id"], 727)
        self.assertNotIn("wire_hex", first["records"][0])
        older = self.tool(client, "trace_search", before_seq=3, port=2, limit=2)
        self.assertEqual([r["seq"] for r in older["records"]], [2, 1])
        self.assertIsNone(older["next_before_seq"])
        newer = self.tool(client, "trace_search", after_seq=2, port=2, include_hex=True)
        self.assertEqual(newer["next_after_seq"], 4)
        self.assertEqual(newer["records"][0]["wire_hex"], "ca000135")
        frame = self.tool(client, "trace_frame", seq=3, include_layout=True)
        self.assertEqual(frame["record"]["fields"], {"block": 1})
        self.assertEqual(frame["layout"], [{"label": "Header"}])
        self.assertIsNone(self.tool(client, "trace_frame", seq=999)["record"])
        error = self.call(
            client,
            "tools/call",
            {
                "name": "trace_search",
                "arguments": {"limit": 65},
            },
        )
        self.assertTrue(error["result"]["isError"])

    def test_device_pages_and_precise_error_counts_are_cached(self) -> None:
        client, gateway = self.server()
        device = {"key": "1:01:02", "port": 1, "values": {"0x001": {}, "0x002": {}}}
        gateway.feed.peers[device["key"]] = device
        gateway.feed.inventory[23] = {"key": "tha:23", "port": 2, "address": 23}
        gateway.feed.counters["example"] = {
            "source": "port_1",
            "reason": "crc",
            "last_seen_ms": 4,
            "count": str(2**64 - 1),
        }
        devices = self.tool(client, "list_devices", limit=1)
        self.assertEqual(devices["next_after_key"], "1:01:02")
        self.assertEqual(
            self.tool(client, "list_devices", port=2)["devices"][0]["address"], 23
        )
        state = self.tool(
            client, "device_state", key="1:01:02", section="readings", limit=1
        )
        self.assertEqual(state["next_after_report"], "0x001")
        self.assertEqual(state["data"]["values"], {"0x001": {}})
        self.assertEqual(
            self.tool(client, "gateway_errors", port=1)["counters"][0]["count"],
            str(2**64 - 1),
        )

    def test_origin_host_token_and_request_size(self) -> None:
        client, _ = self.server(token="test-diagnostics-key")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        auth = {**HEADERS, "Authorization": "Bearer test-diagnostics-key"}
        for extra, status in (
            ({"Origin": "https://untrusted.example"}, 403),
            ({"Origin": "http://127.0.0.1:9000"}, 403),
            ({"Origin": "null"}, 403),
            ({"Host": "untrusted.example"}, 421),
            ({"Authorization": "Bearer other"}, 401),
            ({"Origin": "http://127.0.0.1:8482"}, 200),
        ):
            response = client.post("/mcp", json=payload, headers={**auth, **extra})
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(
            client.post("/mcp", json=payload, headers=HEADERS).status_code, 401
        )
        too_large = client.post(
            "/mcp",
            content=b" " * (17 * 1024),
            headers={**auth, "Content-Type": "application/json"},
        )
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(client.get("/api/state").status_code, 200)

    def test_mcp_can_be_disabled(self) -> None:
        client, _ = self.server(enabled=False)
        self.assertEqual(client.get("/mcp").status_code, 404)
        self.assertEqual(client.get("/").status_code, 200)
