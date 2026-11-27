"""Byte-aligned display spans. Unknown layouts stay opaque."""

from __future__ import annotations

from tekmar_482 import (
    ErrorReason,
    ErrorSource,
    FailureReason,
    GatewayErrorCounter,
    NativeAddress,
    NativeFrame,
    NativeMessage,
    Packet,
    ProtocolError,
    TrpcMethod,
    TrpcPacket,
    TrpcService,
)
from tekmar_482.management import BridgeMethod
from tekmar_482.trpc import method_spec

from .native_layout import native_spans
from .wire_layout import Span, Values, group, span

_BLOCK_QUERY = [
    (1, "Bus"),
    (1, "Subnet"),
    (1, "Node"),
    (2, "Block"),
    (1, "Selector mask"),
    (1, "Selector"),
]


def transfer_fields(message: TrpcPacket) -> list[tuple[int, str]] | None:
    method, service, body = message.method_id, message.service, message.extra
    if service in {TrpcService.REQUEST, TrpcService.RESPONSE_REQUEST}:
        if method == BridgeMethod.NORMAL_BLOCK_READ and len(body) == 7:
            return _BLOCK_QUERY
        if method == BridgeMethod.NORMAL_STATUS_READ and len(body) >= 6:
            return [
                (1, "Bus"),
                (1, "Peer filter"),
                (1, "Subnet"),
                (1, "Node"),
                (2, "Native request ID"),
            ]
    if method == BridgeMethod.NORMAL_BLOCK_EDIT:
        if (
            service in {TrpcService.UPDATE, TrpcService.RESPONSE_UPDATE}
            and len(body) > 9
        ):
            n = body[8]
            if 0 < n < len(body) - 9:
                return [
                    *_BLOCK_QUERY,
                    (1, "Support mask"),
                    (1, "Baseline length"),
                    (n, "Baseline fields"),
                    (len(body) - 9 - n, "Proposed fields"),
                ]
        if service is TrpcService.REPORT and len(body) == 4:
            return [(1, "Bus"), (1, "Edit result"), (1, "Guard TX"), (1, "Update TX")]
    if service is TrpcService.REPORT and len(body) >= 3:
        prefix = [(1, "Bus"), (1, "Read result")]
        if method == BridgeMethod.NORMAL_BLOCK_READ:
            prefix.append((1, "Request TX"))
        if method in {BridgeMethod.NORMAL_BLOCK_READ, BridgeMethod.NORMAL_STATUS_READ}:
            if body[1] and len(body) == 3:
                return [*prefix[:2], (1, "Request TX")]
            size = len(body) - len(prefix) - 4
            if not body[1] and size > 0:
                return [*prefix, (4, "Gateway ms"), (size, "Native frame")]
    return None


def management_fields(message: TrpcPacket) -> list[tuple[int, str]] | None:
    method, service, body = message.method_id, message.service, message.extra
    if method not in BridgeMethod or service is None:
        return None
    if method in {
        BridgeMethod.NORMAL_BLOCK_READ,
        BridgeMethod.NORMAL_BLOCK_EDIT,
        BridgeMethod.NORMAL_STATUS_READ,
    }:
        return transfer_fields(message)
    if method == BridgeMethod.TRAFFIC_COUNTS:
        path = [(1, "Bus"), (1, "Subnet"), (1, "Node")]
        totals = [(8, k) for k in ("RX valid", "TX ACK", "TX no ACK", "TX failed")]
        if service is TrpcService.REQUEST:
            return {0: [], 1: path[:1], 2: [(2, "Index")], 3: path}.get(len(body))
        if service is TrpcService.RESPONSE_REQUEST:
            return {
                4: [(2, "Capacity"), (2, "Used")],
                36: [*path, (1, "Present"), *totals],
                37: [(2, "Index"), *path, *totals],
                57: [
                    *path[:1],
                    *totals,
                    (8, "RX invalid"),
                    (8, "RX untracked"),
                    (8, "TX unattributed"),
                ],
            }.get(len(body))
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
        if method == BridgeMethod.CAPABILITIES and len(body) >= 4:
            return [
                (1, "Wire version"),
                (1, "Mode"),
                (1, "Port count"),
                (len(body) - 3, "Feature flags"),
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
            if len(message.extra) == 14:
                source, reason = message.extra[:2]
                if source not in ErrorSource:
                    parts[2]["coverage"] = "unknown"
                if reason not in ErrorReason or source in ErrorSource:
                    parts[3]["coverage"] = "unknown"
                return (
                    "partial",
                    "Unrecognized diagnostic source or reason",
                    "Gateway diagnostic report",
                )
            return "error", str(err), None
        notice = f"{v.source.name}: {v.reason.name.replace('_', ' ').lower()} (count {v.count})"
    if (
        message.method is TrpcMethod.NETWORK_ERROR
        and message.service is not TrpcService.REQUEST
        and (code := message.get("error"))
        and code & 0xFF
    ):
        notice = f"Device-reported network fault {code:#06x}"
    spec = method_spec(message.method_id)
    if message.service is None:
        return "partial", f"Unknown tRPC service 0x{message.service_id:02X}", notice
    if spec is not None and len(packet.data) == 5 + spec.size:
        return "good", "Valid tRPC frame; all fields decoded", notice
    if any(p.get("coverage") == "invalid" for p in parts):
        return "error", "Invalid embedded native frame", notice
    if message.method_id in BridgeMethod and all(
        p["kind"] != "unknown" and p.get("coverage") != "unknown" for p in parts
    ):
        return "good", "Recognized management fields", notice
    return "partial", "Valid tRPC frame; payload not fully interpreted", notice


def embedded_frame(port: int, part: bytes) -> Span:
    try:
        frame = NativeFrame(port, 0, part)
    except ValueError:
        return {**span(part, "Invalid native frame", "field"), "coverage": "invalid"}
    meaning = frame.decoded
    header, payload = native_spans(frame, meaning)
    item = group("Native frame", [header, payload])
    quality, _ = native_quality(frame, meaning, payload)
    if quality != "good":
        item["coverage"] = "invalid" if quality == "error" else "unknown"
    return item


def announcement_spans(message: TrpcPacket) -> list[Span] | None:
    data = message.extra
    if not data:
        return None
    port = span(data[:1], f"Bus: {data[0]}", "field")
    if message.service is TrpcService.REPORT and len(data) == 2:
        code = data[1]
        name = {
            0: "Link ACK",
            0xFE: "TX pin unavailable",
            0xFF: "Expired before send",
        }.get(code)
        if code in FailureReason:
            name = (
                FailureReason(code).name.replace("_", " ").title().replace("Ack", "ACK")
            )
        return [
            port,
            span(data[1:], f"TX: {name or hex(code)}", "field" if name else "unknown"),
        ]
    if (
        message.service not in {TrpcService.UPDATE, TrpcService.RESPONSE_UPDATE}
        or len(data) < 3
    ):
        return None
    native_id = int.from_bytes(data[1:3], "little")
    try:
        msg = NativeMessage(native_id, NativeAddress(0, 0), data[3:])
        frame = NativeFrame(data[0], 0, msg.to_bytes())
    except ValueError:
        return None
    result = [
        port,
        span(
            data[1:3],
            f"{msg.name or 'Native ID'}: 0x{native_id:03X}",
            "field" if msg.name else "unknown",
        ),
    ]
    # The UART carries the announcement data, not a native frame header.
    meaning = frame.decoded
    _, payload = native_spans(frame, meaning)
    quality, _ = native_quality(frame, meaning, payload)
    if quality != "good":
        annotated = payload if msg.payload else result[-1]
        annotated["coverage"] = "invalid" if quality == "error" else "unknown"
    if msg.payload:
        result.append(payload)
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
    if message.service is None:
        result[0]["coverage"] = "unknown"
    if message.method_name is None and message.method_id not in BridgeMethod:
        result[1]["coverage"] = "unknown"
    if message.method_id == BridgeMethod.NORMAL_ANNOUNCE and (
        parts := announcement_spans(message)
    ):
        return [*result, *parts]
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
            if label == "Native frame":
                result.append(embedded_frame(message.extra[0], part))
            elif label in {"Baseline fields", "Proposed fields"}:
                result.append(span(part, f"{label}: {part.hex(' ')}", "unknown"))
            else:
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


def trpc_view(
    packet: Packet, message: TrpcPacket
) -> tuple[str, Span, str, str, str | None]:
    """Decode tRPC once, independent of its serial or TCP envelope."""
    parts = trpc_spans(packet, message)
    quality, reason, notice = trpc_quality(packet, message, parts)
    children = parts[:2]
    children[0]["column"] = "service"
    children[1]["column"] = "method"
    if parts[2:]:
        children.append(group("Parameters", parts[2:], column="payload"))
    return parts[1]["label"], group("Payload · tRPC", children), quality, reason, notice
