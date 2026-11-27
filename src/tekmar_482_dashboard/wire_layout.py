"""Project nested logical fields onto the canonical, escaped TPCK wire frame."""

from typing import Literal, NotRequired, TypedDict

from tekmar_482 import Packet
from tekmar_482.tpck import ESC, serialize

type Values = dict[str, str | int | float]
type Coverage = Literal["unknown", "invalid", "unused"]


class Bit(TypedDict):
    hi: int
    lo: int
    label: str
    meaning: NotRequired[str]
    coverage: NotRequired[Coverage]


class Span(TypedDict):
    label: str
    kind: str
    size: NotRequired[int]  # Logical width, replaced by the exclusive wire end.
    end: NotRequired[int]
    children: NotRequired[list["Span"]]
    bits: NotRequired[list[Bit]]
    notes: NotRequired[Values]
    column: NotRequired[str]
    coverage: NotRequired[Coverage]
    unknown_notes: NotRequired[list[str]]


class Diagram(TypedDict):
    wire_hex: str
    layout: list[Span]
    escapes: list[int]


def field(size: int, label: str, kind: str = "field") -> Span:
    leaf: Span = {"size": size, "label": label, "kind": kind}
    if kind == "unknown":
        leaf["coverage"] = "unknown"
    return leaf


def span(raw: bytes, label: str, kind: str) -> Span:
    return field(len(raw), label, kind)


def group(
    label: str,
    children: list[Span],
    notes: Values | None = None,
    *,
    column: str | None = None,
) -> Span:
    node: Span = {"label": label, "kind": "group", "children": children}
    if notes:
        node["notes"] = notes
    if column:
        node["column"] = column
    return node


def diagram(packet: Packet, payload: Span) -> Diagram:
    wire = serialize(packet)
    escapes: list[int] = []
    pos = 1  # SOF is a delimiter, not a stuffed data byte.

    def project(node: Span) -> Span:
        nonlocal pos
        if "children" in node:
            return {**node, "children": [project(child) for child in node["children"]]}
        leaf = node.copy()
        for _ in range(leaf.pop("size")):
            if pos >= len(wire) - 1:
                raise ValueError("annotated field extends beyond the TPCK frame")
            if wire[pos] == ESC:
                escapes.append(pos)
                pos += 1
            pos += 1
        leaf["end"] = pos
        return leaf

    items: list[Span] = [
        {"label": "SOF", "kind": "boundary", "end": 1},
        project(field(1, f"Length: {len(packet.data)} B", "length")),
        project(field(1, f"Type: {packet.type:#04x}", "type")),
        project(payload),
        project(field(1, "Checksum", "checksum")),
        {"label": "EOF", "kind": "boundary", "end": len(wire)},
    ]
    if pos != len(wire) - 1:
        raise ValueError("annotated payload width differs from the TPCK length")
    for i in (0, 1, 2, 4, 5):
        items[i]["column"] = "framing"
    return {"wire_hex": wire.hex(), "layout": items, "escapes": escapes}


def opaque(packet: Packet, label: str = "Undecoded bytes") -> Diagram:
    leaves = [span(packet.data, label, "unknown")] if packet.data else []
    return diagram(packet, group("Payload", leaves))
