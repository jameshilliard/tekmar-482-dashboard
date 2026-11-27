import asyncio
import unittest
from contextlib import suppress

from tekmar_482 import NullTransport, Packet, Tekmar482Client

from tekmar_482_dashboard.client_trace import Hint
from tekmar_482_dashboard.compat import Compatibility, supported


def packet(line: str) -> Packet:
    raw = bytes.fromhex(line)
    return Packet(raw[0], raw[1:])


class Wire(NullTransport):
    def __init__(self) -> None:
        super().__init__()
        self.tx: asyncio.Queue[Packet] = asyncio.Queue()

    async def write_packet(self, p: Packet) -> None:
        await super().write_packet(p)
        self.tx.put_nowait(p)


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.wire = Wire()
        self.client = await self.enterAsyncContext(Tekmar482Client(self.wire))
        self.server = Compatibility("127.0.0.1", 0, lambda: None)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.attached = self.server.attach(self.client)
        self.attached.__enter__()
        self.addCleanup(self.attached.__exit__, None, None, None)
        await self.enterAsyncContext(asyncio.timeout(4))

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        r, w = await asyncio.open_connection(
            "127.0.0.1", self.server.snapshot()["port"]
        )
        self.addCleanup(w.close)
        return r, w

    async def test_upstream_startup_is_byte_exact_and_results_are_broadcast(
        self,
    ) -> None:
        r, w = await self.connect()
        observer, _ = await self.connect()
        for query, replies in (
            ("06000F01000000", ["06030F01000000"]),
            ("0601870100000000", ["0604870100000600"]),
            ("06018F0100000000", ["06048F0100000300"]),
            ("06006F01000001", ["06036F01000001"]),
            ("0601670100000000", ["0604670100000121", "0604670100000000"]),
            ("06000F01000001", ["06030F01000001"]),
            ("06003F0100000121015C", ["06033F0100000121015C"]),
        ):
            encoded = query.lower().encode()
            w.write(encoded[:5])
            await w.drain()
            w.write(encoded[5:] + b"\r\n")
            self.assertEqual(await self.wire.tx.get(), packet(query))
            for reply in replies:
                self.wire.queue_packet(packet(reply))
                for stream in (r, observer):
                    self.assertEqual(await stream.readline(), reply.encode() + b"\n")
        rows = self.server.snapshot()["active"]
        self.assertEqual([row["received"] for row in rows], [7, 0])
        self.assertEqual([row["sent"] for row in rows], [8, 8])
        first, second = (self.server.traces[p["id"]].read(0)["events"] for p in rows)
        self.assertEqual(sum(e["direction"] == "to_gateway" for e in first), 7)
        self.assertTrue(
            all(e["direction"] == "to_client" and not e["requests"] for e in second)
        )
        self.assertEqual(first[-1]["requests"], (first[-2]["seq"],))
        self.assertEqual(first[-1]["wire_hex"], "06033f0100000121015c")
        self.assertEqual(first[-1]["layout"][0]["label"], "Type: 0x06")
        self.assertNotIn("Checksum", str(first[-1]["layout"]))
        inventory = [e for e in first if e["name"] == "DeviceInventory"]
        self.assertEqual(
            [r["requests"] for r in inventory[1:]], [(inventory[0]["seq"],)] * 2
        )

    async def test_simultaneous_identical_reads_have_only_local_ambiguous_hints(
        self,
    ) -> None:
        sockets = [await self.connect(), await self.connect()]
        for _, w in sockets:
            w.write(b"06018F0100000000\n")
            self.assertEqual(await self.wire.tx.get(), packet("06018f0100000000"))
        self.wire.queue_packet(packet("06048f0100000300"))
        self.wire.queue_packet(packet("06023701000001213406"))
        for r, _ in sockets:
            await r.readline()
            await r.readline()
        for trace in self.server.traces.values():
            reply, report = trace.read(1)["events"]
            self.assertEqual(reply["requests"], (1,))
            self.assertTrue(reply["ambiguous"])
            self.assertIsNone(reply["elapsed_ms"])
            self.assertFalse(report["requests"])

    async def test_devices_receive_only_methods_the_upstream_class_handles(
        self,
    ) -> None:
        def identity(address: int, code: int) -> Packet:
            return packet(
                "060497010000"
                + (address.to_bytes(2, "little") + code.to_bytes(4, "little")).hex()
            )

        # The dashboard can identify devices before a client connects.
        self.server.publish(identity(1, 105801))
        self.server.publish(identity(2, 107201))
        self.server.publish(identity(3, 101101))
        reader, writer = await self.connect()
        captured: list[Packet] = []
        with self.client.session.observe_received_packets(captured.append):
            writer.write(b"06017F010000010000\n06017F010000020000\n")
            self.assertEqual(await self.wire.tx.get(), packet("06017f010000020000"))
            hidden = [
                "06047F0100000100FF",
                "06027F010000010002",
                "060277010000010004",
                "06027F0100000300FF",
                "06023E0100000200049205",
            ]
            visible = [
                "06047F010000020001",
                "0602370100000200AE05",
                "06022F010000010001",
                "06023E0100000300049205",
            ]
            for line in [*hidden, *visible]:
                self.wire.queue_packet(packet(line))
            for line in visible:
                self.assertEqual(await reader.readline(), line.encode() + b"\n")
            self.assertEqual(captured, list(map(packet, [*hidden, *visible])))
        self.wire.queue_packet(packet("06025F01000001000400"))
        self.wire.queue_packet(identity(1, 107201))
        self.wire.queue_packet(packet("06027F010000010002"))
        for expected in [
            packet("06025F01000001000400"),
            identity(1, 107201),
            packet("06027F010000010002"),
        ]:
            self.assertEqual(packet((await reader.readline()).decode()), expected)

    async def test_filters_both_ways_without_disabling_diagnostics_or_rewriting(
        self,
    ) -> None:
        r, w = await self.connect()
        tracked: list[Packet] = []
        with self.client.session.observe_received_packets(tracked.append):
            w.write(
                b"garbage\n010203\n060101F04B54\n06028F0100000300\n06003F010000\n06018F010000\n"
            )
            # The last request is the compact version, not rewritten with placeholders.
            self.assertEqual(await self.wire.tx.get(), packet("06018F010000"))
            hidden = [
                "010000",
                "07000123",
                "060201F04B54",
                "060406F04B54",
                "0604FE010000",
            ]
            for wire in hidden:
                self.wire.queue_packet(packet(wire))
            reply = "06048F0100000300"
            self.wire.queue_packet(packet(reply))
            self.assertEqual(await r.readline(), reply.encode() + b"\n")
            self.assertEqual(tracked, [packet(p) for p in [*hidden, reply]])
        stats = self.server.snapshot()
        self.assertEqual(stats["active"][0]["filtered"], 5)
        self.assertTrue(self.wire.tx.empty())

    async def test_detach_disconnects_and_does_not_replay_in_new_session(self) -> None:
        r, w = await self.connect()
        w.write(b"06018F010")
        self.attached.__exit__(None, None, None)
        # Closing a socket with unread partial input can produce TCP RST or FIN.
        with suppress(ConnectionResetError):
            self.assertEqual(await r.read(), b"")
        r, _ = await self.connect()
        self.assertEqual(await r.read(), b"")
        self.assertFalse(self.server.snapshot()["available"])
        with self.server.attach(self.client):
            _, writer = await self.connect()
            writer.write(b"06018F0100000000\n")
            self.assertEqual(await self.wire.tx.get(), packet("06018F0100000000"))
        self.assertEqual(len(self.wire.written), 1)

    async def test_backpressure_disconnects_only_the_slow_consumer(self) -> None:
        slow, _ = await self.connect()
        fast, _ = await self.connect()
        task, peer = next(iter(self.server.peers.items()))
        while not peer.outbox.full():
            peer.outbox.put_nowait((b"test\n", "test", packet("060200000000"), Hint()))
        report = "06023701000001213406"
        self.server.publish(packet(report))
        self.assertEqual(await fast.readline(), report.encode() + b"\n")
        await slow.read()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.server.recent[0].reason, "Client not reading")
        self.assertEqual(len(self.server.peers), 1)

    def test_reported_extensions_preserved_and_truncated_data_never_forwarded(
        self,
    ) -> None:
        self.assertIsNotNone(
            supported(packet("06023701000001213406CA35"), from_client=False)
        )
        self.assertIsNone(supported(packet("060237010000012134"), from_client=False))
        self.assertIsNone(supported(packet("060004F04B5400"), from_client=True))
