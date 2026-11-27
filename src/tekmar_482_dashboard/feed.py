"""Bounded, single-event-loop history shared by every browser."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from tekmar_482 import (
    CaptureGap,
    GatewayTraceGap,
    NativeFrame,
    NativePath,
    NativeState,
    TrpcMethod,
    TrpcPacket,
)
from tekmar_482._native_payloads.layouts import BLOCK_SELECTORS
from tekmar_482.units import degh_to_fahrenheit

from .wire_layout import Values

Row = dict[str, Any]
HISTORY = 1200
WINDOW = 60


def data(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Enum):
        return value.name.lower()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: data(getattr(value, f.name))
            for f in fields(value)
            if f.name not in {"frame", "message", "air_frame"}
        }
    if isinstance(value, Mapping):
        return {str(k): data(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [data(v) for v in value]
    return value


class Feed:
    def __init__(self, capacity: int = HISTORY) -> None:
        self.events: deque[Row] = deque(maxlen=capacity)
        self.seq = 0
        self.changed = asyncio.Event()
        self.status: Row = {
            "connection": "connecting",
            "mode": None,
            "trace": False,
            "errors": False,
            "features": [],
            "revision": None,
        }
        self.ports: list[Row] = [
            {"port": p, "rx": 0, "tx": 0, "invalid": 0, "last": None} for p in range(5)
        ]
        self.bins: list[deque[tuple[int, int]]] = [
            deque(maxlen=WINDOW) for _ in self.ports
        ]
        self.counters: dict[str, Row] = {}
        self.reset_sources()

    def reset_sources(self) -> None:
        self.native = NativeState()
        self.inventory: dict[int, Row] = {}
        self.peers: dict[str, Row] = {}
        self.counters.clear()

    def publish(self, layer: str, direction: str, name: str, **detail: Any) -> Row:
        self.seq += 1
        now = time.time()
        event = {
            "seq": self.seq,
            "at": now,
            "layer": layer,
            "direction": direction,
            "name": name,
            **detail,
        }
        self.events.append(event)
        port = detail.get("port")
        if layer == "tn4" and isinstance(port, int) and 0 <= port < len(self.ports):
            entry = self.ports[port]
            entry["last"] = now
            field = {"rx": "rx", "tx": "tx", "rx_invalid": "invalid"}.get(direction)
            if field:
                entry[field] += 1
            buckets = self.bins[port]
            tick = int(now)
            count = buckets.pop()[1] if buckets and buckets[-1][0] == tick else 0
            buckets.append((tick, count + 1))
        self.wake()
        return event

    def wake(self) -> None:
        """Notify dashboards of a state change without inventing a trace event."""
        self.changed.set()
        self.changed = asyncio.Event()

    def state(self, message: str, **values: Any) -> None:
        self.status.update(values)
        self.publish("system", "info", message, fields=values)

    def gap(self, gap: CaptureGap, **detail: Any) -> None:
        self.peers = {
            key: peer
            for key, peer in self.peers.items()
            if "node" not in peer or not gap.affects(peer["port"])
        }
        self.native.observe(gap)
        lost = gap.dropped
        fields: Row = {"dropped": str(lost)}
        if isinstance(gap, GatewayTraceGap):
            fields["ports"] = list(gap.ports)
            ports = ", ".join(map(str, gap.ports))
            reason = f"Gateway dropped {lost} trace records across ports {ports}"
        else:
            reason = f"Host capture buffer dropped {lost} records"
        self.publish(
            "system",
            "gap",
            "Capture gap",
            fields=fields,
            quality="info",
            notice=f"Capture gap: {lost} records lost",
            reason=reason,
            **detail,
        )

    def observe(self, frame: NativeFrame, decoded: Values) -> str:
        path = NativePath.from_frame(frame)
        key = f"{path.port}:{path.subnet:02X}:{path.node:02X}"
        changes = self.native.observe(frame)
        peer = self.peers.setdefault(
            key,
            {
                "key": key,
                "port": path.port,
                "subnet": path.subnet,
                "node": path.node,
                "count": 0,
                "values": {},
            },
        )
        peer.update(
            last=time.time(),
            count=peer["count"] + 1,
            message=f"0x{frame.message_id:03X}",
        )
        peer["values"][f"0x{frame.message_id:03X}"] = decoded
        if frame.message.name == "BlockReport" and len(frame.message.payload) >= 3:
            block = int.from_bytes(frame.message.payload[:2], "little")
            layout = BLOCK_SELECTORS.get(block)
            selector = decoded.get(layout[0]) if layout is not None else None
            peer.setdefault("blocks", {})[
                f"{block:#04x}" + (f" / {selector}" if selector is not None else "")
            ] = decoded
        for changed in set((*changes, path)):
            row = self.peers.get(
                f"{changed.port}:{changed.subnet:02X}:{changed.node:02X}"
            )
            if row is None:
                continue
            identity = self.native.identity(changed)
            air = self.native.air_status(changed)
            readings = [r for r in (air.heat, air.cool) if r is not None] if air else []
            temperature = next(
                (r.temperature_f for r in readings if r.temperature_f is not None), None
            )
            climate = self.native.climate(changed)
            product = identity.product if identity else None
            snow = self.native.snowmelt(changed)
            plant = self.native.plant_registers(changed)
            row.update(
                updated_seq=self.seq + 1,
                model=product.model if product else None,
                family=product.family
                if product
                else "snowmelt"
                if snow
                else "plant"
                if plant
                else None,
                address=changed.address.tha_address(changed.port),
                temperature_f=temperature,
                calling=any(r.calling for r in readings) if readings else None,
                climate=data(climate),
                snowmelt=data(snow),
                identity=data(identity),
                fault=data(self.native.fault_report(changed)),
                line=data(self.native.line_reports_for(changed)),
                water=data(self.native.water_source(changed)),
                setpoint=data(self.native.setpoint_control(changed)),
                io=data(self.native.io_for(changed)),
                plant=data(plant),
                schedule=data(self.native.schedule(changed)),
            )
        return key

    def stock_report(self, packet: TrpcPacket) -> str | None:
        addr = packet.get("address")
        if addr is None or addr == 0xFFFF:
            return None
        key = f"tha:{addr}"
        peer = self.peers.setdefault(
            key, {"key": key, "address": addr, "count": 0, "values": {}}
        )
        peer["count"] += 1
        peer["updated_seq"] = self.seq + 1
        peer["last"] = time.time()
        peer["message"] = packet.method_name
        peer["values"][packet.method_name or str(packet.method_id)] = dict(packet.body)
        if (
            packet.method is TrpcMethod.CURRENT_TEMPERATURE
            and (temp := packet.get("temp")) is not None
        ):
            peer["temperature_f"] = degh_to_fahrenheit(temp)
        return key

    def snapshot(self, since: int | None = None) -> Row:
        head = self.events[0]["seq"] if self.events else self.seq + 1
        reset = since is None or since < head - 1 or since > self.seq
        events = (
            list(self.events)[-150:]
            if reset
            else [e for e in self.events if e["seq"] > since]
        )
        now = int(time.time())
        ports = []
        for port, bins in zip(self.ports, self.bins, strict=True):
            counts = dict(bins)
            ports.append(
                {
                    **port,
                    "activity": [
                        counts.get(t, 0) for t in range(now - WINDOW + 1, now + 1)
                    ],
                }
            )
        return {
            "type": "snapshot" if reset else "delta",
            "seq": self.seq,
            "events": events,
            "skipped": max(0, events[0]["seq"] - 1 - since)
            if reset and events and since is not None
            else 0,
            "status": self.status,
            "ports": ports,
            "device_details": {
                r["key"]: {
                    k: r.get(k)
                    for k in (
                        "key",
                        "address",
                        "controls",
                        "settings",
                        "fan_step",
                        "setback_events",
                    )
                }
                for r in self.inventory.values()
                if reset or r.get("updated_seq", 0) > (since or 0)
            },
            "devices": [
                {
                    k: d.get(k)
                    for k in (
                        "key",
                        "address",
                        "model",
                        "family",
                        "port",
                        "temperature_f",
                        "controls",
                    )
                }
                for d in self.inventory.values()
            ],
            "peers": [
                {
                    k: p.get(k)
                    for k in (
                        "key",
                        "port",
                        "subnet",
                        "node",
                        "count",
                        "last",
                        "model",
                        "family",
                        "address",
                        "message",
                        "temperature_f",
                        "calling",
                    )
                }
                for p in self.peers.values()
            ],
            "counters": list(self.counters.values()),
        }
