import unittest

from tekmar_482 import (
    NativeAddress,
    NativeFrame,
    NativeMessage,
    NativeTargets,
    Packet,
    TrpcPacket,
)
from tekmar_482.management import BridgeMethod

from tekmar_482_dashboard.endpoints import native_endpoints, trpc_endpoints
from tekmar_482_dashboard.gateway import Gateway
from tekmar_482_dashboard.native_layout import native_spans
from tekmar_482_dashboard.trace_view import management_fields, native_quality
from tekmar_482_dashboard.wire_layout import diagram, field, group, opaque

from .test_dashboard import ENDPOINT


class AnnotationTests(unittest.TestCase):
    def test_addressed_requests_and_replies_have_filterable_endpoints(self) -> None:
        msg = NativeMessage.request_block(
            5, source=NativeAddress(0, 0), target=NativeTargets(0x42, 0b101)
        )
        frame = NativeFrame(4, 0, msg.to_bytes())
        self.assertEqual(
            native_endpoints(frame, frame.decoded, received=False),
            {"to": ["4:42:01", "4:42:03"]},
        )
        self.assertEqual(
            native_endpoints(frame, frame.decoded, received=True)["from"], ["4:00:00"]
        )
        opaque_msg = NativeMessage(0x123, NativeAddress(0x42, 35), b"")
        frame = NativeFrame(4, 0, opaque_msg.to_bytes())
        self.assertEqual(
            native_endpoints(frame, frame.decoded, received=True), {"from": ["4:42:03"]}
        )
        self.assertEqual(native_endpoints(frame, frame.decoded, received=False), {})
        for address in (42, 65535):
            packet = Packet(
                6,
                bytes.fromhex("02 37010000") + address.to_bytes(2, "little") + bytes(2),
            )
            self.assertEqual(
                trpc_endpoints(TrpcPacket.from_packet(packet)),
                {"device": ["tha:42"]} if address == 42 else {},
            )
        identity = Packet(
            6,
            b"\x01"
            + BridgeMethod.NORMAL_IDENTITY.to_bytes(4, "little")
            + bytes((4, 0x42, 3)),
        )
        self.assertEqual(
            trpc_endpoints(TrpcPacket.from_packet(identity)), {"device": ["4:42:03"]}
        )

    def test_valid_fault_and_link_failure_are_not_invalid_frames(self) -> None:
        gw = Gateway(ENDPOINT)
        native = NativeMessage(
            0x3D7, NativeAddress(0x42, 7), bytes.fromhex("80 20")
        ).to_bytes()
        for packet, notice in [
            (Packet(1, bytes(6) + native), "Outdoor Sensor Error"),
            (
                Packet(6, bytes.fromhex("02 07010000 8000")),
                "Device-reported network fault",
            ),
            (
                Packet(
                    6,
                    b"\x02"
                    + BridgeMethod.ERROR.to_bytes(4, "little")
                    + bytes.fromhex("01000000 01"),
                ),
                "Gateway busy",
            ),
            (Packet(1, bytes.fromhex("02 04 02 01")), "no ack"),
            (Packet(1, bytes.fromhex("03 04 01")), "send in progress"),
        ]:
            with self.subTest(packet=packet):
                gw._wire(packet, False, literal_header=packet.type == 1)
                event = gw.feed.events[-1]
                self.assertEqual(event["quality"], "good")
                self.assertIn(notice, event["notice"])
        gw._wire(
            Packet(1, bytes.fromhex("04 04 00000000 02 0800 aa")),
            False,
            literal_header=True,
        )
        event = gw.feed.events[-1]
        self.assertEqual(event["quality"], "error")
        self.assertIn("crc", event["reason"])

    def test_unmapped_bits_are_distinguished_from_mapped_neighbors(self) -> None:
        for ident, raw in [(0x123, "2f"), (0x36F, "80 01 25 1006 00 0000")]:
            frame = NativeFrame(
                0,
                0,
                NativeMessage(
                    ident, NativeAddress(1, 5), bytes.fromhex(raw)
                ).to_bytes(),
            )
            header, payload = native_spans(frame, frame.decoded)
            if ident == 0x123:
                bits = header["children"][0]["bits"]
                self.assertEqual([b.get("coverage") for b in bits], [None, "unknown"])
                self.assertEqual(payload["children"][0]["coverage"], "unknown")
            else:
                bits = payload["children"][0]["bits"]
                self.assertEqual(
                    [b.get("coverage") for b in bits], ["unknown", None, None, None]
                )
                self.assertNotIn("notes", payload)
            self.assertEqual(
                native_quality(frame, frame.decoded, payload)[0], "partial"
            )

    def test_octets_and_bit_groups_cover_the_original_frame(self) -> None:
        for message, payload in [
            (0x36F, "a0 43 24 11 06 30 00 40"),
            (0x2E7, "15 00 0c 34 12"),
            (0x2E7, "15 00 0c aa bb 34 12"),
            (0x17E, "09 01 1b 04 02"),
            (0x123, "ca 35 2f"),
        ]:
            with self.subTest(message=message, payload=payload):
                body = NativeMessage(
                    message, NativeAddress(0x42, 7), bytes.fromhex(payload)
                ).to_bytes()
                frame = NativeFrame(3, 123, body)
                header, contents = native_spans(frame, frame.decoded)
                spans = [*header["children"], *contents["children"]]
                self.assertEqual(sum(s["size"] for s in spans), len(body))
                for s in spans:
                    if bits := s.get("bits"):
                        self.assertEqual(
                            [i for b in bits for i in range(b["hi"], b["lo"] - 1, -1)],
                            list(reversed(range(8))),
                        )
                if message == 0x2E7:
                    self.assertIn("minimum room temp", spans[-1]["label"])
                if message == 0x123:
                    self.assertEqual(spans[-1]["kind"], "unknown")
                    self.assertEqual(
                        native_quality(frame, frame.decoded, contents)[0], "partial"
                    )

    def test_escapes_belong_to_one_nested_field_in_wire_order(self) -> None:
        frame = diagram(
            Packet(6, bytes.fromhex("ca 2f 35")),
            group(
                "Payload",
                [
                    field(1, "First"),
                    group("Inner", [field(2, "Next two")]),
                ],
            ),
        )
        self.assertEqual(frame["wire_hex"], "ca03062fca2f2f2f353735")
        self.assertEqual(frame["escapes"], [3, 5, 7])
        items = frame["layout"]
        self.assertEqual([p.get("end") for p in items], [1, 2, 3, None, 10, 11])
        self.assertEqual(items[3]["children"][0]["end"], 5)
        self.assertEqual(items[3]["children"][1]["children"][0]["end"], 9)
        self.assertNotIn("end", items[3])

    def test_unknown_empty_and_escaped_framing_fields(self) -> None:
        frame = opaque(Packet(0x35, b""))
        self.assertEqual(frame["escapes"], [2, 4])
        self.assertEqual(frame["layout"][3]["children"], [])
        self.assertEqual(
            frame["layout"][-1]["end"], len(bytes.fromhex(frame["wire_hex"]))
        )
        frame = opaque(Packet(0, bytes(0x2F)))
        self.assertEqual(frame["escapes"], [1, 51])

    def test_485_raw_send_keeps_reserved_length_literal_and_labels_timestamp(
        self,
    ) -> None:
        gateway = Gateway(ENDPOINT)
        body = bytes.fromhex("12300025 2fca35") + bytes(34)
        packet = Packet(1, bytes.fromhex("0004 00000000") + body)
        gateway._wire(packet, True, literal_header=True)
        row = gateway.feed.events[-1]
        self.assertEqual(row["direction"], "host_tx")
        self.assertEqual(row["port"], 4)
        self.assertEqual(row["hex"], body.hex())
        self.assertTrue(row["wire_hex"].startswith("ca2f01000400000000"))
        self.assertEqual(row["escapes"], [13, 15, 17])
        timestamp = row["layout"][3]["children"][0]["children"][2]
        self.assertEqual(timestamp["label"], "TX timestamp: 0")
        self.assertEqual(timestamp["end"], 9)

    def test_mode_selection_includes_raw_options(self) -> None:
        packet = Packet(6, bytes.fromhex("03 02f04b54 0100"))
        self.assertEqual(
            management_fields(TrpcPacket.from_packet(packet)),
            [(1, "Mode: Raw"), (1, "Raw extensions: Off")],
        )

    def test_truncated_uart_packet_is_visible_as_an_error(self) -> None:
        gateway = Gateway(ENDPOINT)
        packet = Packet(6, b"\x01")
        gateway._wire(packet, False)
        row = gateway.feed.events[-1]
        self.assertEqual(row["quality"], "error")
        self.assertIn("header", row["reason"])
        self.assertEqual(row["wire_hex"], "ca0106010835")
