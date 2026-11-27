"""Bounded per-connection history; reply hints are not transaction IDs."""

from collections import deque
from dataclasses import dataclass
from time import monotonic, time
from typing import Any

from tekmar_482 import (
    Packet,
    ProtocolError,
    ResponseMatch,
    TrpcCommand,
    TrpcMethod,
    TrpcPacket,
    TrpcService,
)

from .trace_view import trpc_view
from .wire_layout import span, tcp_record

_HISTORY = 256
_MATCH_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Candidate:
    seq: int
    at: float
    match: ResponseMatch
    inventory_walk: bool


@dataclass(frozen=True, slots=True)
class Hint:
    requests: tuple[int, ...] = ()
    elapsed_ms: int | None = None
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class Record:
    seq: int
    at: float
    direction: str
    packet: Packet | None
    hint: Hint

    def render(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "seq": self.seq,
            "at": self.at,
            "direction": self.direction,
            "requests": self.hint.requests,
            "elapsed_ms": self.hint.elapsed_ms,
            "ambiguous": self.hint.ambiguous,
            "wire_format": "TCP hex record",
        }
        p = self.packet
        if p is None:
            return {
                **row,
                "name": "Invalid hex line",
                "quality": "error",
                "reason": "Not a complete hexadecimal packet",
            }
        try:
            msg = TrpcPacket.from_packet(p)
        except ProtocolError as err:
            return {
                **row,
                "name": "Filtered packet",
                "quality": "partial",
                "reason": str(err),
                **tcp_record(p, span(p.data, "Opaque payload", "unknown")),
            }
        name, body, quality, reason, notice = trpc_view(p, msg)
        return {
            **row,
            "name": name,
            "service": msg.service_name,
            "quality": quality,
            "reason": reason,
            "notice": notice,
            **tcp_record(p, body),
        }


class ClientTrace:
    def __init__(self, session_id: int) -> None:
        self.id = session_id
        self.seq = 0
        self.records: deque[Record] = deque(maxlen=_HISTORY)
        self.pending: deque[Candidate] = deque(maxlen=_HISTORY)

    def record(
        self, direction: str, packet: Packet | None, hint: Hint = Hint()
    ) -> None:
        self.seq += 1
        self.records.append(Record(self.seq, time(), direction, packet, hint))

    def request(self, packet: Packet) -> None:
        self.record("to_gateway", packet)
        msg = TrpcPacket.from_packet(packet)
        try:
            cmd = TrpcCommand(msg.service_id, msg.method_id, msg.body)
            match = ResponseMatch.for_command(cmd)
        except (ValueError, ProtocolError):
            return  # Observation must not change wire behavior.
        self.pending.append(
            Candidate(
                self.seq,
                monotonic(),
                match,
                msg.method is TrpcMethod.DEVICE_INVENTORY
                and msg.service is TrpcService.REQUEST
                and msg.get("address") == 0,
            )
        )

    def candidates(self, msg: TrpcPacket, now: float) -> list[Candidate]:
        while self.pending and now - self.pending[0].at > _MATCH_SECONDS:
            self.pending.popleft()
        if msg.service is TrpcService.REPORT:
            return []
        return [
            p
            for p in self.pending
            if (
                (
                    p.inventory_walk
                    and msg.method is TrpcMethod.DEVICE_INVENTORY
                    and msg.service is TrpcService.RESPONSE_REQUEST
                )
                or p.match.matches(msg)
            )
        ]

    def correlate(
        self, msg: TrpcPacket, matches: list[Candidate], now: float, count: int
    ) -> Hint:
        if not matches:
            return Hint()
        for p in matches:
            if not p.inventory_walk or msg.get("address") in (0, 0xFFFF):
                self.pending.remove(p)
        return Hint(
            tuple(p.seq for p in matches),
            round((now - matches[0].at) * 1000) if count == 1 else None,
            count > 1,
        )

    def read(self, after: int) -> dict[str, Any]:
        oldest = self.records[0].seq if self.records else 1
        return {
            "client_id": self.id,
            "seq": self.seq,
            "skipped": max(0, oldest - after - 1),
            "events": [r.render() for r in self.records if r.seq > after],
        }
