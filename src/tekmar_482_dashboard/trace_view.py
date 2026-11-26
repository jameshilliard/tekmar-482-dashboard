"""Byte-aligned display spans. Unknown layouts stay opaque."""

from __future__ import annotations

from tekmar_482 import (
    GatewayErrorCounter,
    NativeFrame,
    Packet,
    ProtocolError,
    TrpcPacket,
    TrpcService,
)
from tekmar_482.management import BridgeMethod
from tekmar_482.trpc import method_spec

Span = dict[str, str]


def management_fields(message: TrpcPacket) -> list[tuple[int, str]] | None:
    method, service, body = message.method_id, message.service, message.extra
    if method not in BridgeMethod or service is None:
        return None
    if method == BridgeMethod.ERROR_REPORTING:
        if service is TrpcService.REQUEST and len(body) == 2:
            return [(1, "Source"), (1, "Reason")]
        if (
            service in {TrpcService.REPORT, TrpcService.RESPONSE_REQUEST}
            and len(body) == 14
        ):
            return [(1, "Source"), (1, "Reason"), (4, "Last ms"), (8, "Count")]
        if service in {
            TrpcService.RESPONSE_REQUEST,
            TrpcService.UPDATE,
            TrpcService.RESPONSE_UPDATE,
        }:
            return [(1, "Source mask")]
    if method == BridgeMethod.STATS:
        if service == TrpcService.REQUEST:
            return [(1, "Bus")]
        if service == TrpcService.RESPONSE_REQUEST:
            return [(1, "Bus"), (8, "Dropped valid"), (8, "Dropped invalid")]
    if service == TrpcService.RESPONSE_REQUEST:
        if method == BridgeMethod.CAPABILITIES:
            return [
                (1, "Wire version"),
                (1, "Mode"),
                (1, "Port count"),
                (1, "Feature flags"),
            ]
        if method == BridgeMethod.NORMAL_TRACE:
            return [(1, "Trace mask"), *((8, f"Bus {p} drops") for p in range(5))]
        if method == BridgeMethod.PANEL:
            return [
                (4, "Boot inputs"),
                (4, "Inputs"),
                (8, "Test presses"),
                (8, "DIP changes"),
                (4, "LED mask"),
                (4, "Pin mask"),
                (4, "Testing"),
            ]
        if method == BridgeMethod.BUILD and body and body.isascii():
            return [(len(body), "Build")]
    if method in {
        BridgeMethod.MODE,
        BridgeMethod.NORMAL_TRACE,
        BridgeMethod.ACK_MASK,
    } and service in {TrpcService.UPDATE, TrpcService.RESPONSE_UPDATE}:
        return [(1, BridgeMethod(method).name.replace("_", " ").title())]
    if method == BridgeMethod.ERROR:
        return [(4, "Rejected method"), (1, "Reason")]
    return [] if not body else None


def native_quality(frame: NativeFrame) -> tuple[str, str]:
    meaning = frame.decoded
    if "truncated" in meaning:
        return (
            "error",
            f"Truncated {frame.message.name or 'native'} payload: {meaning['truncated']}",
        )
    if frame.message.name is None:
        return "partial", f"Unrecognized native message ID 0x{frame.message_id:03X}"
    if any(
        ("reserved" in k and v not in (0, "0", "0x00"))
        or any(tag in k for tag in ("extension_hex", "unknown", "opaque", "unresolved"))
        or (isinstance(v, str) and v.lower() in {"unknown", "ambiguous", "unsupported"})
        for k, v in meaning.items()
    ):
        return "partial", "Frame valid; contains extension, reserved or unresolved data"
    if frame.message.name in {"BlockReport", "BlockUpdate"} and frame.message.payload:
        return (
            "partial",
            "Block data shown as decoded; not all register meanings are verified",
        )
    if (
        frame.message.name == "NetworkError"
        and frame.message.payload
        and frame.message.payload[0]
    ):
        return (
            "error",
            f"Device-reported network fault 0x{frame.message.payload[0]:02X} (valid frame)",
        )
    return "good", "Valid frame and recognized payload"


def trpc_quality(packet: Packet, message: TrpcPacket) -> tuple[str, str]:
    if message.method_id == BridgeMethod.ERROR:
        reason = message.extra[4:] if len(message.extra) >= 5 else b""
        return "error", {
            b"\x01": "Gateway busy",
            b"\x02": "Invalid service or payload",
            b"\x03": "Wrong gateway mode",
        }.get(reason, "Unrecognized gateway rejection: " + reason.hex())
    if (
        message.method_id == BridgeMethod.ERROR_REPORTING
        and message.service_name == "Report"
    ):
        try:
            v = GatewayErrorCounter.decode(message.extra)
        except ProtocolError as err:
            return "error", str(err)
        return (
            "error",
            f"{v.source.name}: {v.reason.name.replace('_', ' ').lower()} (lifetime count {v.count})",
        )
    spec = method_spec(message.method_id)
    if message.service is None:
        return "partial", f"Unknown tRPC service 0x{message.service_id:02X}"
    if spec is not None and len(packet.data) == 5 + spec.size:
        return "good", "Valid tRPC frame; all fields decoded"
    layout = management_fields(message)
    if layout is not None and sum(n for n, _ in layout) == len(message.extra):
        return "good", "Recognized management fields"
    return "partial", "Valid tRPC frame; payload not fully interpreted"


def span(raw: bytes, label: str, kind: str) -> Span:
    return {"hex": raw.hex(" ").upper(), "label": label, "kind": kind}


def native_spans(frame: NativeFrame) -> list[Span]:
    msg, body, f = frame.message, frame.message.payload, frame.decoded
    result = [
        span(
            frame.body[:4],
            f"ID {frame.message_id:03X} · P{msg.priority} · {frame.subnet:02X}:{frame.node:02X} · {len(body)}B",
            "header",
        )
    ]
    parts: list[tuple[int, str]] = []
    if msg.name == "AirGroupMemberStatus" and len(body) >= 8:
        parts = [
            (1, f"Group {f.get('air_group_id', '?')}"),
            (1, str(f.get("schedule_name", "Air flags"))),
            (1, f"Target {f.get('target_f', '?')} °F"),
            (2, f"Air {f.get('temperature_f', '?')} °F"),
            (1, "Outputs"),
            (2, "Remote sensor"),
        ]
    elif msg.name == "BlockReport" and len(body) >= 3:
        parts = [
            (2, f"Block {int.from_bytes(body[:2], 'little'):#04x}"),
            (1, f"Support {body[2]:08b}"),
        ]
    elif msg.name == "BlockRequest" and len(body) >= 7:
        parts = [
            (2, f"Block {int.from_bytes(body[:2], 'little'):#04x}"),
            (1, f"Subnet {body[2]:02X}"),
            (4, "Node mask"),
        ]
    elif msg.name == "BlockUpdate" and len(body) >= 4:
        parts = [
            (1, f"Subnet {body[0]:02X}"),
            (1, f"Node {body[1] & 31}"),
            (2, f"Block {int.from_bytes(body[2:4], 'little'):#04x}"),
        ]
    elif msg.name == "NetworkError" and len(body) >= 2:
        parts = [
            (1, f"Error {body[0]:02X}"),
            (1, f"Priority {body[1] >> 6} · ID {body[1] & 63}"),
        ]
    offset = 0
    for width, label in parts:
        result.append(span(body[offset : offset + width], label, "field"))
        offset += width
    if body[offset:]:
        summary = (
            " · ".join(
                f"{k.replace('_', ' ')}: {v}"
                for k, v in f.items()
                if not k.endswith("_raw")
            )
            or "Native payload"
        )
        result.append(span(body[offset:], summary, "payload"))
    return result


def trpc_spans(packet: Packet, message: TrpcPacket) -> list[Span]:
    raw = packet.data
    name = message.method_name or (
        BridgeMethod(message.method_id).name
        if message.method_id in BridgeMethod
        else f"0x{message.method_id:08X}"
    )
    result = [
        span(raw[:1], message.service_name or "Service", "service"),
        span(raw[1:5], name, "header"),
    ]
    offset = 5
    spec = method_spec(message.method_id)
    if spec:
        for field in spec.fields:
            part = raw[offset : offset + field.size]
            if len(part) != field.size:
                break
            result.append(
                span(part, f"{field.name}: {int.from_bytes(part, 'little')}", "field")
            )
            offset += field.size
    elif layout := management_fields(message):
        for width, label in layout:
            part = raw[offset : offset + width]
            if len(part) != width:
                break
            value = (
                part.decode("ascii")
                if label == "Build"
                else int.from_bytes(part, "little")
            )
            result.append(span(part, f"{label}: {value}", "field"))
            offset += width
    if raw[offset:]:
        result.append(
            span(
                raw[offset:],
                "Extension payload" if not spec else "Extra or partial field",
                "payload",
            )
        )
    return result
