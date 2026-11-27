"""Byte-aligned display spans. Unknown layouts stay opaque."""

from __future__ import annotations

from tekmar_482 import (
    GatewayErrorCounter,
    NativeFrame,
    Packet,
    ProtocolError,
    TrpcMethod,
    TrpcPacket,
    TrpcService,
)
from tekmar_482.management import BridgeMethod
from tekmar_482.trpc import method_spec

from .wire_layout import Span, Values, span


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
    if method == BridgeMethod.NORMAL_IDENTITY and service in {
        TrpcService.REQUEST,
        TrpcService.RESPONSE_REQUEST,
    }:
        return [(1, "Bus"), (1, "Subnet"), (1, "Node")]
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


def native_quality(
    frame: NativeFrame, meaning: Values, payload: Span
) -> tuple[str, str]:
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
    if payload.get("unknown_notes") or any(
        p.get("coverage") == "unknown"
        or any(b.get("coverage") == "unknown" for b in p.get("bits", []))
        for p in payload.get("children", [])
    ):
        return (
            "partial",
            "Frame valid; some payload bytes do not have a verified field mapping",
        )
    return "good", "Valid frame and recognized payload"


def trpc_quality(
    packet: Packet, message: TrpcPacket, parts: list[Span]
) -> tuple[str, str, str | None]:
    notice = None
    if message.method_id == BridgeMethod.ERROR:
        if len(message.extra) != 5:
            return "error", "Invalid gateway rejection length", None
        reason = message.extra[4]
        notice = {
            1: "Gateway busy",
            2: "Gateway rejected service or payload",
            3: "Wrong gateway mode",
        }.get(reason)
        if notice is None:
            parts[-1]["coverage"] = "unknown"
            return (
                "partial",
                "Unrecognized gateway rejection reason",
                f"Gateway rejection: {reason:#04x}",
            )
    if (
        message.method_id == BridgeMethod.ERROR_REPORTING
        and message.service_name == "Report"
    ):
        try:
            v = GatewayErrorCounter.decode(message.extra)
        except ProtocolError as err:
            return "error", str(err), None
        notice = f"{v.source.name}: {v.reason.name.replace('_', ' ').lower()} (count {v.count})"
    if (
        message.method is TrpcMethod.NETWORK_ERROR
        and message.service is not TrpcService.REQUEST
        and (code := message.get("error"))
    ):
        notice = f"Device-reported network fault {code:#06x}"
    spec = method_spec(message.method_id)
    if message.service is None:
        return "partial", f"Unknown tRPC service 0x{message.service_id:02X}", notice
    if spec is not None and len(packet.data) == 5 + spec.size:
        return "good", "Valid tRPC frame; all fields decoded", notice
    if message.method_id in BridgeMethod and all(p["kind"] != "unknown" for p in parts):
        return "good", "Recognized management fields", notice
    return "partial", "Valid tRPC frame; payload not fully interpreted", notice


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
    if message.service is None:
        result[0]["coverage"] = "unknown"
    if message.method_name is None and message.method_id not in BridgeMethod:
        result[1]["coverage"] = "unknown"
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
                "unknown",
            )
        )
    return result
