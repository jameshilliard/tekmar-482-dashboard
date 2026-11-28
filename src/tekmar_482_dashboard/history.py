"""Compact gateway capture history. Only the application loop writes to it."""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Literal

TRACE_SECONDS = 30 * 60
TRACE_CAPACITY = 50_000
TIMELINE_MINUTES = 6 * 60
_MINUTE = 60

type TraceLayer = Literal["tn4", "trpc", "host", "tpck", "error", "system", "operator"]
type Row = dict[str, Any]

_SUMMARY_KEYS = (
    "seq",
    "at",
    "layer",
    "direction",
    "name",
    "port",
    "subnet",
    "node",
    "message_id",
    "peer",
    "protocol",
    "service",
    "quality",
    "reason",
    "notice",
    "outcome",
    "ack",
    "early_start",
    "gateway_ms",
    "origin",
    "bit_count",
)
_WIRE_KEYS = ("hex", "wire_hex")
_COMPACT_KEYS = (*_SUMMARY_KEYS, *_WIRE_KEYS)
_SYSTEM_FIELDS = (
    "connection",
    "mode",
    "trace",
    "errors",
    "trace_ports",
    "ack_ports",
    "error_sources",
    "firmware_update",
    "dropped",
    "ports",
)


def _compact(event: Row) -> Row:
    row = {key: event[key] for key in _COMPACT_KEYS if key in event}
    fields = event.get("fields") or {}
    if event["layer"] == "system":
        row["fields"] = {key: fields[key] for key in _SYSTEM_FIELDS if key in fields}
    elif fields:
        row["fields"] = fields.copy()
    if endpoints := event.get("endpoints"):
        row["endpoints"] = {k: tuple(v) for k, v in endpoints.items()}
    return row


@dataclass(frozen=True, slots=True)
class TraceQuery:
    port: int | None = None
    layer: TraceLayer | None = None
    direction: str | None = None
    device: str | None = None
    message_id: int | None = None
    name: str | None = None
    quality: str | None = None
    start_at: float | None = None
    end_at: float | None = None

    def matches(self, row: Row) -> bool:
        if (
            (self.port is not None and row.get("port") != self.port)
            or (self.layer is not None and row["layer"] != self.layer)
            or (self.direction is not None and row["direction"] != self.direction)
            or (
                self.message_id is not None and row.get("message_id") != self.message_id
            )
            or (self.quality is not None and row.get("quality") != self.quality)
            or (self.start_at is not None and row["at"] < self.start_at)
            or (self.end_at is not None and row["at"] >= self.end_at)
            or (
                self.name is not None and self.name.casefold() != row["name"].casefold()
            )
        ):
            return False
        return (
            self.device is None
            or self.device == (row.get("peer") or "").upper()
            or any(
                self.device == endpoint.upper()
                for keys in row.get("endpoints", {}).values()
                for endpoint in keys
            )
        )


@dataclass(frozen=True, slots=True)
class TraceView:
    # Rows are never mutated after publication; a snapshot is safe to search in a worker.
    rows: tuple[tuple[float, Row], ...]
    coverage: Row

    def search(
        self,
        query: TraceQuery,
        *,
        limit: int,
        before_seq: int | None,
        after_seq: int | None,
        include_hex: bool,
    ) -> Row:
        source: Iterable[tuple[float, Row]] = (
            iter(self.rows) if after_seq is not None else reversed(self.rows)
        )
        matches = (
            row
            for _, row in source
            if (after_seq is None or row["seq"] > after_seq)
            and (before_seq is None or row["seq"] < before_seq)
            and query.matches(row)
        )
        page = list(islice(matches, limit + 1))
        more = len(page) > limit
        page = page[:limit]
        oldest = self.coverage["oldest_seq"]
        latest = self.coverage["latest_seq"]
        keys = _COMPACT_KEYS if include_hex else _SUMMARY_KEYS
        return {
            "coverage": self.coverage,
            "history_truncated": bool(
                (after_seq is not None and after_seq < oldest - 1)
                or (before_seq is not None and before_seq <= oldest and oldest > 1)
                or (
                    query.start_at is not None
                    and query.start_at < self.coverage["available_from"]
                )
                or (
                    query.end_at is not None
                    and query.end_at <= self.coverage["available_from"]
                )
            ),
            "cursor_ahead": bool(
                (after_seq is not None and after_seq > latest)
                or (before_seq is not None and before_seq > latest + 1)
            ),
            "has_more": more,
            "next_before_seq": page[-1]["seq"] if more and after_seq is None else None,
            "next_after_seq": (page[-1]["seq"] if more else latest)
            if after_seq is not None
            else None,
            "records": [{key: row[key] for key in keys if key in row} for row in page],
        }


@dataclass(slots=True)
class _Minute:
    counts: dict[int | None, Counter[str]] = field(default_factory=dict)
    gateway_drops: Counter[int] = field(default_factory=Counter)

    def add(self, row: Row) -> None:
        layer, direction = row["layer"], row["direction"]
        port = row.get("port")
        fields = row.get("fields", {})
        if layer == "tn4":
            metric = direction
        elif layer == "error":
            metric = "error_reports"
        elif layer == "trpc":
            metric = "trpc_sent" if direction == "host_tx" else "trpc_received"
        elif layer == "host":
            metric = "raw_requests" if direction == "host_tx" else "raw_results"
        elif layer == "system" and direction == "gap":
            mask = sum(1 << p for p in fields.get("ports", ()))
            if mask:
                self.gateway_drops[mask] += int(fields["dropped"])
            else:
                counters = self.counts.setdefault(None, Counter())
                counters["host_drops"] += int(fields["dropped"])
            return
        elif layer == "system" and fields.get("connection") in {
            "online",
            "offline",
            "busy",
            "connecting",
        }:
            metric = "entered_" + fields["connection"]
        else:
            return
        counters = self.counts.setdefault(port, Counter())
        counters[metric] += 1
        if layer == "tn4" and direction == "tx":
            counters["tx_" + str(row.get("outcome", "unknown"))] += 1
        if layer == "host" and direction == "host_rx" and "status" in fields:
            counters["raw_" + str(fields["status"])] += 1

    def extend(self, other: _Minute, port: int | None) -> None:
        for bus, counts in other.counts.items():
            if port is None or bus in {None, port}:
                self.counts.setdefault(bus, Counter()).update(counts)
        for mask, count in other.gateway_drops.items():
            if port is None or mask & (1 << port):
                self.gateway_drops[mask] += count

    def render(self) -> Row:
        return {
            "ports": [
                {"port": port, **counts}
                for port, counts in sorted(
                    (port, counts)
                    for port, counts in self.counts.items()
                    if port is not None
                )
            ],
            "gateway": dict(self.counts.get(None, {})),
            # A multi-port drop total is counted once, not attributed to each port.
            "gateway_capture_drops": [
                {
                    "ports": [p for p in range(5) if mask & (1 << p)],
                    "count": str(count),
                }
                for mask, count in sorted(self.gateway_drops.items())
            ],
        }


class History:
    def __init__(
        self,
        *,
        capacity: int = TRACE_CAPACITY,
        seconds: int = TRACE_SECONDS,
    ) -> None:
        if capacity < 1 or seconds < 1:
            raise ValueError("trace retention must be positive")
        self.capacity = capacity
        self.seconds = seconds
        self.started_at = time.time()
        self._trace: deque[tuple[float, Row]] = deque(maxlen=capacity)
        self._minutes: list[tuple[int, _Minute] | None] = [None] * TIMELINE_MINUTES
        self.latest_seq = 0

    def _expire(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._trace and self._trace[0][0] < cutoff:
            self._trace.popleft()

    def record(self, event: Row) -> None:
        now = time.monotonic()
        self._expire(now)
        self.latest_seq = event["seq"]
        row = _compact(event)
        self._trace.append((now, row))
        minute = int(row["at"]) // _MINUTE
        slot = minute % TIMELINE_MINUTES
        sample = self._minutes[slot]
        if sample is None or sample[0] != minute:
            sample = self._minutes[slot] = (minute, _Minute())
        sample[1].add(row)

    def coverage(self) -> Row:
        self._expire(time.monotonic())
        first = self._trace[0][1] if self._trace else {}
        oldest = first.get("seq", self.latest_seq + 1)
        return {
            "started_at": self.started_at,
            "available_from": self.started_at
            if oldest == 1
            else first.get("at", time.time()),
            "oldest_seq": oldest,
            "latest_seq": self.latest_seq,
            "retained": len(self._trace),
            "max_records": self.capacity,
            "max_seconds": self.seconds,
            "evicted_records": oldest - 1,
        }

    def snapshot(self) -> TraceView:
        coverage = self.coverage()
        return TraceView(tuple(self._trace), coverage)

    def frame(self, seq: int) -> Row:
        coverage = self.coverage()
        index = seq - coverage["oldest_seq"]
        return {
            "coverage": coverage,
            "record": self._trace[index][1] if 0 <= index < len(self._trace) else None,
        }

    def timeline(
        self, minutes: int, port: int | None, limit: int, *, end_at: float | None = None
    ) -> Row:
        now = time.time()
        current = int(now) // _MINUTE
        available_from = max(
            self.started_at, (current - TIMELINE_MINUTES + 1) * _MINUTE
        )
        end: float
        if end_at is not None and end_at < now:
            # Only whole minutes can be queried before the live partial minute.
            end = int(end_at) // _MINUTE * _MINUTE
            current = end // _MINUTE - 1
        else:
            end = now
        requested = (current - minutes + 1) * _MINUTE
        start = int(max(requested, available_from)) // _MINUTE
        width = max(1, (current - start + limit) // limit)
        total = _Minute()
        buckets = []
        for minute in range(start, current + 1, width):
            stop = min(minute + width, current + 1)
            bucket = _Minute()
            for key in range(minute, stop):
                sample = self._minutes[key % TIMELINE_MINUTES]
                if sample is not None and sample[0] == key:
                    bucket.extend(sample[1], port)
            total.extend(bucket, port)
            buckets.append(
                {
                    "start_at": max(minute * _MINUTE, available_from),
                    "end_at": min(stop * _MINUTE, end),
                    **bucket.render(),
                }
            )
        return {
            "available_from": available_from,
            "start_at": requested,
            "end_at": end,
            "history_truncated": requested < available_from,
            "bucket_seconds": width * _MINUTE,
            "totals": total.render(),
            "buckets": buckets,
        }
