"""Read-only MCP tools over the dashboard's existing gateway observations."""

from __future__ import annotations

import asyncio
import heapq
import secrets
from collections.abc import Iterator
from typing import Annotated, Literal
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .feed import Feed, Row
from .history import TIMELINE_MINUTES, TraceLayer, TraceQuery

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)
_DEVICE_SUMMARY = (
    "key",
    "address",
    "port",
    "subnet",
    "node",
    "model",
    "family",
    "temperature_f",
    "calling",
    "last",
    "count",
    "message",
)
_DEVICE_SECTIONS = {
    "identity": ("identity",),
    "readings": ("values", "climate", "water", "snowmelt", "setpoint", "io"),
    "configuration": ("settings", "blocks", "plant", "schedule", "setback_events"),
    "diagnostics": ("fault", "line"),
}
_MAX_REQUESTS = 8
_REQUEST_SECONDS = 15
MCP_BODY_LIMIT = 16 * 1024
MCP_PATH = "/mcp"

type Port = Annotated[
    int, Field(ge=0, le=4, description="Physical gateway input 0 through 4")
]
type Seq = Annotated[int, Field(ge=0)]
type PageSize = Annotated[int, Field(ge=1, le=64)]
type DeviceKey = Annotated[
    str,
    Field(
        pattern=r"^(?:[0-4]:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}|[tT][hH][aA]:[0-9]{1,5})$",
        description="Native port:SS:NN (hex subnet/node) or tha:decimal address",
    ),
]
type UnixTime = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Direction = Literal[
    "rx", "tx", "rx_invalid", "host_tx", "host_rx", "report", "gap", "info", "result"
]


def diagnostics_server(feed: Feed) -> MCPServer:
    mcp = MCPServer(
        "tekmarshark",
        log_level="WARNING",
        instructions=(
            "Passive diagnostics for this tekmar gateway. Start with network_status, "
            "locate the interval with bus_timeline, then use trace_search and trace_frame. "
            "Times marked at are Unix seconds; gateway_ms is wrapping firmware uptime. "
            "Reported traffic describes captured evidence, not everything on the bus. "
            "Check retention, capture gaps, and connection state before concluding "
            "that a port or device was silent. Tools never send frames or change settings."
        ),
    )

    @mcp.tool(annotations=_READ_ONLY)
    async def network_status() -> Row:
        """Get capture coverage, gateway mode, observed port activity and current availability."""
        keys = (
            "connection",
            "mode",
            "trace",
            "errors",
            "trace_ports",
            "error_sources",
            "ack_ports",
            "baudrate",
            "transport",
            "firmware",
            "revision",
            "features",
            "firmware_update",
            "panel",
        )
        return {
            "gateway": {k: feed.status[k] for k in keys if k in feed.status},
            "ports": [dict(port) for port in feed.ports],
            "inventory_devices": len(feed.inventory),
            "observed_sources": len(feed.peers),
            "trace_coverage": feed.history.coverage(),
            "timeline_minutes": TIMELINE_MINUTES,
        }

    @mcp.tool(annotations=_READ_ONLY)
    async def bus_timeline(
        minutes: Annotated[int, Field(ge=1, le=TIMELINE_MINUTES)] = 30,
        port: Port | None = None,
        max_buckets: Annotated[int, Field(ge=1, le=60)] = 12,
        end_at: UnixTime | None = None,
    ) -> Row:
        """Summarize recent RX, TX outcomes, invalid frames, error reports and capture loss.

        Uses minute summaries even after individual records expire. Bucket and total
        counts are observations, not firmware lifetime counters. Missing metrics
        mean none were captured; the last bucket may be partial. A multi-port
        gateway drop count is shared across its listed ports. To inspect an older
        interval, specify end_at (rounded down to a minute boundary, exclusive).
        """
        return feed.history.timeline(minutes, port, max_buckets, end_at=end_at)

    @mcp.tool(annotations=_READ_ONLY)
    async def trace_search(
        port: Port | None = None,
        layer: TraceLayer | None = None,
        direction: Direction | None = None,
        device: DeviceKey | None = None,
        message_id: Annotated[int, Field(ge=0, le=1023)] | None = None,
        name: Annotated[str, Field(max_length=100)] | None = None,
        quality: Literal["good", "partial", "error", "info"] | None = None,
        start_at: UnixTime | None = None,
        end_at: UnixTime | None = None,
        before_seq: Seq | None = None,
        after_seq: Seq | None = None,
        limit: PageSize = 20,
        include_hex: bool = False,
    ) -> Row:
        """Page through captured tN4, tRPC and gateway events with optional exact filters.

        Default is newest first; page backward using next_before_seq. after_seq
        reads forward; use next_after_seq to continue or poll with the same filters.
        Cursors are exclusive and reset on service restart. start_at is inclusive
        and end_at exclusive. device matches observed or addressed endpoints;
        message_id is a numeric tN4 ID and name also matches tRPC method names.
        The port filter matches records tagged with a physical port. When correlating
        across layers, omit port, layer and direction and use a time range. Use
        trace_frame for decoded fields. Payload hex is omitted unless requested.
        """
        if before_seq is not None and after_seq is not None:
            raise ValueError("Choose before_seq or after_seq, not both")
        if start_at is not None and end_at is not None and start_at >= end_at:
            raise ValueError("start_at must be earlier than end_at")
        query = TraceQuery(
            port=port,
            layer=layer,
            direction=direction,
            device=device.upper() if device else None,
            message_id=message_id,
            name=name,
            quality=quality,
            start_at=start_at,
            end_at=end_at,
        )
        return await asyncio.to_thread(
            feed.history.snapshot().search,
            query,
            limit=limit,
            before_seq=before_seq,
            after_seq=after_seq,
            include_hex=include_hex,
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def trace_frame(seq: Seq, include_layout: bool = False) -> Row:
        """Inspect one record, including decoded fields, endpoints and captured hex.

        Wire bytes are reconstructed from accepted TPCK packets. Set include_layout
        for the annotated byte/bit diagram if it is still in the smaller web history.
        A missing record is reported with the current coverage, not as an error.
        """
        result = feed.history.frame(seq)
        if include_layout:
            result["layout"] = next(
                (
                    r.get("layout")
                    for r in reversed(feed.events)
                    if result["record"] is not None and r["seq"] == seq
                ),
                None,
            )
        return result

    def records() -> Iterator[Row]:
        yield from feed.inventory.values()
        for key, peer in feed.peers.items():
            if not key.startswith("tha:") or peer["address"] not in feed.inventory:
                yield peer

    @mcp.tool(annotations=_READ_ONLY)
    async def list_devices(
        port: Port | None = None,
        after_key: DeviceKey | None = None,
        limit: PageSize = 20,
    ) -> Row:
        """List cached devices and native sources without running discovery.

        Inventory and native sources have separate keys; a source alone is not a
        confirmed device identity. Page with next_after_key using the same filter.
        """
        after = after_key.upper() if after_key else ""
        page = heapq.nsmallest(
            limit + 1,
            (
                row
                for row in records()
                if (port is None or port == row.get("port"))
                and row["key"].upper() > after
            ),
            key=lambda row: row["key"].upper(),
        )
        more = len(page) > limit
        return {
            "next_after_key": page[limit - 1]["key"] if more else None,
            "devices": [
                {k: row[k] for k in _DEVICE_SUMMARY if k in row} for row in page[:limit]
            ],
        }

    @mcp.tool(annotations=_READ_ONLY)
    async def device_state(
        key: DeviceKey,
        section: Literal[
            "summary", "identity", "readings", "configuration", "diagnostics"
        ] = "summary",
        after_report: Annotated[str, Field(max_length=80)] | None = None,
        limit: PageSize = 16,
    ) -> Row:
        """Inspect cached state for a device key; no gateway or device reads are issued.

        Readings and configuration page the report/block map with next_after_report.
        """
        if key.lower().startswith("tha:"):
            address = int(key[4:])
            row = feed.inventory.get(address) or feed.peers.get(f"tha:{address}")
        else:
            row = feed.peers.get(key.upper())
        if row is None:
            return {"key": key, "found": False}
        detail = {k: row[k] for k in _DEVICE_SECTIONS.get(section, ()) if k in row}
        cursor = None
        report_key = {"readings": "values", "configuration": "blocks"}.get(section)
        if report_key is not None and (reports := detail.get(report_key)):
            page = heapq.nsmallest(
                limit + 1, (k for k in reports if k > (after_report or ""))
            )
            if len(page) > limit:
                cursor = page[limit - 1]
            detail[report_key] = {k: reports[k] for k in page[:limit]}
        return {
            "found": True,
            "device": {k: row[k] for k in _DEVICE_SUMMARY if k in row},
            "section": section,
            "data": detail,
            "next_after_report": cursor,
        }

    @mcp.tool(annotations=_READ_ONLY)
    async def gateway_errors(
        port: Port | None = None, nonzero_only: bool = True, limit: PageSize = 32
    ) -> Row:
        """Get cached firmware lifetime error totals, highest counts first.

        Counts are decimal strings to preserve 64-bit precision. They are not
        counts within the trace window. Without port, include UART/gateway errors.
        """
        matching = [
            row
            for row in feed.counters.values()
            if (port is None or row.get("source") == f"port_{port}")
            and (not nonzero_only or int(row["count"]))
        ]
        page = sorted(matching, key=lambda row: -int(row["count"]))[:limit]
        return {
            "reporting_enabled": bool(feed.status["errors"]),
            "matching": len(matching),
            "counters": page,
        }

    return mcp


def _authority(value: str) -> tuple[str, int | None] | None:
    try:
        url = urlsplit("//" + value)
        if (
            url.hostname
            and url.username is None
            and not (url.path or url.query or url.fragment)
        ):
            return url.hostname, url.port
    except ValueError:
        pass
    return None


class McpAccess:
    """Limit MCP to the accepted socket address or local names and same-origin HTTP."""

    def __init__(self, app: ASGIApp, local_names: set[str], token: str | None) -> None:
        self.app = app
        self.local_names = {name.lower() for name in local_names}
        self.token = f"Bearer {token}".encode() if token is not None else None
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        host = _authority(hosts[0]) if len(hosts) == 1 else None
        server = scope.get("server")
        origins = headers.getlist("origin")
        scheme = scope.get("scheme", "http")
        if origins and (
            len(origins) != 1
            or host is None
            or not self._origin_matches(origins[0], scheme, host)
        ):
            await PlainTextResponse("Invalid MCP Origin", 403)(scope, receive, send)
            return
        if host is None or (
            host[0] not in self.local_names and (not server or host[0] != server[0])
        ):
            await PlainTextResponse("Unrecognized MCP Host", 421)(scope, receive, send)
            return
        authorization = headers.getlist("authorization")
        if self.token is not None and (
            len(authorization) != 1
            or not secrets.compare_digest(authorization[0].encode(), self.token)
        ):
            await PlainTextResponse(
                "MCP key required", 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
            return
        if self.active >= _MAX_REQUESTS:
            await PlainTextResponse("MCP is busy", 503, headers={"Retry-After": "1"})(
                scope, receive, send
            )
            return

        responded = False

        async def uncached(message: Message) -> None:
            nonlocal responded
            if message["type"] == "http.response.start":
                responded = True
                message.setdefault("headers", []).append(
                    (b"cache-control", b"no-store")
                )
            await send(message)

        self.active += 1
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                await self.app(scope, receive, uncached)
        except TimeoutError:
            if responded:
                raise
            await PlainTextResponse("MCP request timed out", 408)(scope, receive, send)
        finally:
            self.active -= 1

    @staticmethod
    def _origin_matches(origin: str, scheme: str, host: tuple[str, int | None]) -> bool:
        if scheme not in {"http", "https"} or not origin.startswith(scheme + "://"):
            return False
        origin_host = _authority(origin[len(scheme) + 3 :])
        default_port = 443 if scheme == "https" else 80
        return origin_host is not None and (
            origin_host[0],
            default_port if origin_host[1] is None else origin_host[1],
        ) == (host[0], default_port if host[1] is None else host[1])
