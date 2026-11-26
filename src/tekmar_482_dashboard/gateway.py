"""One gateway owner; sessions and capture buffers never belong to a browser."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import AsyncExitStack, suppress
from typing import Any

from tekmar_482 import (
    BridgeClient,
    BridgeFeature,
    BridgeManagement,
    BridgeMode,
    BridgeStream,
    CaptureGap,
    ErrorReason,
    ErrorReporting,
    ErrorSource,
    GatewayEndpoint,
    NativeFrame,
    NormalTrace,
    Packet,
    ProtocolError,
    RxFlag,
    Tekmar482Client,
    TraceRx,
    TraceTx,
    TrpcMethod,
    TrpcPacket,
    TrpcService,
    open_gateway,
)
from tekmar_482.cli_trace import trace_record
from tekmar_482.packet import TYPE_NORMAL_TRACE, TYPE_TN4, TYPE_TRPC
from tekmar_482.raw import InvalidCapture, decode_raw_packet
from tekmar_482.tpck import serialize
from tekmar_482.wire_trace import observe_bridge_packets

from . import operations
from .devices import Inventory
from .feed import Feed, Row, data
from .identity import identify_sources
from .trace_view import native_quality, native_spans, span, trpc_quality, trpc_spans


class Gateway:
    def __init__(
        self,
        endpoint: GatewayEndpoint,
        *,
        vendor: bool = False,
        mode: str = "normal",
        errors: bool = True,
    ) -> None:
        self.endpoint, self.vendor, self.mode, self.errors = (
            endpoint,
            vendor,
            mode,
            errors,
        )
        self.trace_ports = tuple(range(5))
        self.ack_ports: tuple[int, ...] = ()
        self.error_sources = tuple(ErrorSource)
        self.feed = Feed()
        self.inventory = Inventory(self.feed)
        self.scan: asyncio.Task[None] | None = None
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.client: Tekmar482Client | None = None
        self.raw: BridgeStream | None = None
        self.diagnostics: ErrorReporting | None = None
        self.feed.status.update(
            baudrate=endpoint.baudrate,
            transport="TCP" if endpoint.port else "Serial",
            firmware="Stock" if vendor else "Custom",
        )

    def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="gateway owner")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _run(self) -> None:
        delay = 1
        while True:
            self.feed.state(
                "Connecting to gateway",
                connection="connecting",
                trace=False,
                errors=False,
            )
            try:
                async with AsyncExitStack() as stack:
                    self.feed.reset_sources()
                    self.inventory = Inventory(self.feed)
                    if self.vendor:
                        stock = await stack.enter_async_context(
                            open_gateway(self.endpoint, vendor=True)
                        )
                        assert isinstance(stock, Tekmar482Client)
                        await self._normal(stack, stock, BridgeFeature(0))
                    else:
                        wire = await stack.enter_async_context(self.endpoint.connect())
                        if self.mode == "raw":
                            stack.enter_context(
                                observe_bridge_packets(
                                    lambda p, tx: self._wire(p, tx, captures=False)
                                )
                            )
                        bridge = BridgeClient(wire)
                        await bridge.identify()
                        revision = await bridge.revision()
                        self.feed.status["panel"] = data(await bridge.panel())
                        async with bridge.normal(select_mode=False) as control:
                            caps = await BridgeManagement(control).capabilities()
                        if caps is None:
                            raise ValueError(
                                "Expected custom gateway; use --vendor for stock"
                            )
                        self.feed.status.update(
                            revision=revision or "unversioned",
                            features=[
                                f.name.lower()
                                for f in BridgeFeature
                                if f.name and f in caps.features
                            ],
                        )
                        delay = 1
                        if self.mode == "raw":
                            self.raw = await stack.enter_async_context(
                                bridge.stream(
                                    diagnostics=False, ack_ports=self.ack_ports
                                )
                            )
                            self.diagnostics = (
                                self.raw.error_reporting
                                if caps.features & BridgeFeature.ERROR_REPORTING
                                else None
                            )
                            await self._capture(
                                stack, self._raw_frames(self.raw), traced=True
                            )
                        else:
                            normal = await stack.enter_async_context(
                                bridge.normal(
                                    select_mode=caps.mode != BridgeMode.NORMAL
                                )
                            )
                            await self._normal(stack, normal, caps.features)
                raise ConnectionError("Gateway stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.feed.state(
                    "Gateway unavailable",
                    connection="offline",
                    trace=False,
                    errors=False,
                    problem=str(err) or type(err).__name__,
                )
            finally:
                self.client = self.raw = self.diagnostics = None
            await asyncio.sleep(delay)
            delay = min(15, delay * 2)

    async def _normal(
        self, stack: AsyncExitStack, client: Tekmar482Client, flags: BridgeFeature
    ) -> None:
        manager = BridgeManagement(client)
        self.diagnostics = (
            manager.error_reporting if flags & BridgeFeature.ERROR_REPORTING else None
        )
        has_trace = bool(flags & BridgeFeature.NORMAL_TRACE and self.trace_ports)
        if has_trace:
            trace = await stack.enter_async_context(manager.trace(self.trace_ports))
        else:
            trace = NormalTrace(512)
            stack.enter_context(client.session.trace_packets(trace))
        self.client = client
        stack.enter_context(client.session.observe_messages(self.inventory.observe))
        async with asyncio.TaskGroup() as tasks:
            identify = (
                tasks.create_task(
                    identify_sources(self.feed, manager, self.trace_ports)
                )
                if has_trace and flags & BridgeFeature.NORMAL_IDENTITY
                else None
            )
            self.scan = tasks.create_task(self.inventory.watch(client))
            try:
                await self._capture(stack, self._normal_frames(trace), traced=has_trace)
            finally:
                self.scan.cancel()
                self.scan = None
                if identify is not None:
                    identify.cancel()

    async def _capture(
        self, stack: AsyncExitStack, source: Coroutine[Any, Any, None], *, traced: bool
    ) -> None:
        # Schedule the main reader only after optional subscriptions have succeeded.
        try:
            reports = (
                await stack.enter_async_context(
                    self.diagnostics.watch(self.error_sources)
                )
                if self.errors and self.diagnostics
                else None
            )
        except BaseException:
            source.close()
            raise
        self.feed.state(
            "Gateway connected",
            connection="online",
            mode=self.mode,
            trace=traced,
            errors=reports is not None,
            problem=None,
            trace_ports=list(self.trace_ports),
            ack_ports=list(self.ack_ports) if self.mode == "raw" else [],
            error_sources=[int(s) for s in self.error_sources]
            if reports is not None
            else [],
        )
        if reports is None:
            await source
            return

        async def seed() -> None:
            diag = self.diagnostics
            if diag is None:
                return
            for src in self.error_sources:
                for reason in ErrorReason:
                    if reason.supports(src):
                        value = await diag.counter(src, reason)
                        self._counter(value)
            self.feed.publish("system", "info", "Gateway error totals synchronized")

        async def counters() -> None:
            async for counter in reports:
                item = self._counter(counter)
                self.feed.publish(
                    "error",
                    "report",
                    counter.reason.name,
                    fields=item,
                    port=int(counter.source)
                    if counter.source != ErrorSource.GATEWAY
                    else None,
                    quality="error",
                    reason=f"{counter.source.name}: {counter.reason.name.replace('_', ' ').lower()}",
                )
            raise ConnectionError("Error reporting stream ended")

        async with asyncio.TaskGroup() as group:
            group.create_task(seed())
            group.create_task(counters())
            await source
            raise ConnectionError("Frame stream ended")

    def _counter(self, value: Any) -> Row:
        item: Row = data(value)
        item["count"] = str(value.count)
        key = f"{value.source}:{value.reason}"
        previous = self.feed.counters.get(key)
        if previous is None or int(previous["count"]) <= value.count:
            self.feed.counters[key] = item
        return item

    def _native(
        self, frame: NativeFrame, direction: str, *, observed: bool, **meta: Any
    ) -> None:
        key = self.feed.observe(frame) if observed else None
        if key and frame.message.name in {
            "AirGroupMemberStatus",
            "AirGroupMasterStatus",
            "SetpointStatus",
        }:
            self.inventory.observed_device(key)
        quality, why = native_quality(frame)
        if meta.get("outcome") not in (None, "ack"):
            quality, why = (
                "error",
                "Gateway transmit: " + str(meta["outcome"]).replace("_", " "),
            )
        elif meta.get("early_start"):
            quality, why = "error", "Frame started before the minimum interframe gap"
        self.feed.publish(
            "tn4",
            direction,
            frame.message.name or f"0x{frame.message_id:03X}",
            message_id=frame.message_id,
            port=frame.port,
            subnet=frame.subnet,
            node=frame.node,
            peer=key,
            gateway_ms=frame.timestamp_ms,
            hex=frame.body.hex(),
            spans=native_spans(frame),
            fields=frame.decoded,
            quality=quality,
            reason=why,
            **meta,
        )

    def _wire(self, packet: Packet, tx: bool, *, captures: bool = True) -> None:
        try:
            self._record_wire(packet, tx, captures=captures)
        except (ProtocolError, ValueError) as err:
            self.feed.publish(
                "tpck",
                "host_tx" if tx else "host_rx",
                "Malformed packet",
                hex=packet.data.hex(),
                wire_hex=serialize(packet).hex(),
                quality="error",
                reason=str(err),
            )

    def _record_wire(self, packet: Packet, tx: bool, *, captures: bool) -> None:
        direction = "host_tx" if tx else "host_rx"
        wire = serialize(packet).hex()
        if packet.type == TYPE_TRPC:
            message = TrpcPacket.from_packet(packet)
            spans = trpc_spans(packet, message)
            quality, why = trpc_quality(packet, message)
            self.feed.publish(
                "trpc",
                direction,
                spans[1]["label"],
                service=message.service_name,
                hex=packet.data.hex(),
                wire_hex=wire,
                spans=spans,
                fields=dict(message.body),
                quality=quality,
                reason=why,
            )
        elif packet.type == TYPE_TN4 and len(packet.data) >= 2:
            raw, port = packet.data, packet.data[1]
            if tx and raw[0] == 1:
                frame = NativeFrame(port, 0, raw[2:])
                quality, why = native_quality(frame)
                self.feed.publish(
                    "host",
                    direction,
                    "Raw Send",
                    port=port,
                    hex=raw.hex(),
                    wire_hex=wire,
                    spans=[
                        span(raw[:2], f"Send · bus {port}", "service"),
                        *native_spans(frame),
                    ],
                    fields=frame.decoded,
                    quality=quality,
                    reason=why,
                )
            elif not tx and raw[0] in (0, 4) and not captures:
                return  # RawDispatcher owns these capture records.
            elif not tx:
                record = decode_raw_packet(packet)
                if isinstance(record, NativeFrame):
                    self._native(record, "rx", observed=True, wire_hex=wire)
                else:
                    detail = data(record)
                    why = detail.get("reason")
                    self.feed.publish(
                        "tn4" if isinstance(record, InvalidCapture) else "host",
                        "rx_invalid"
                        if isinstance(record, InvalidCapture)
                        else direction,
                        type(record).__name__,
                        port=port,
                        hex=raw.hex(),
                        wire_hex=wire,
                        spans=[span(raw, type(record).__name__, "payload")],
                        fields=detail,
                        quality="error" if why is not None else "good",
                        reason=str(why).replace("_", " ")
                        if why is not None
                        else "Link acknowledged",
                    )
        else:
            self.feed.publish(
                "tpck",
                direction,
                f"Type {packet.type:#04x}",
                hex=packet.data.hex(),
                wire_hex=wire,
                quality="partial",
                reason="Unrecognized TPCK packet type or service",
            )

    async def _normal_frames(self, trace: NormalTrace) -> None:
        async for event in trace:
            if isinstance(event, CaptureGap):
                self.feed.gap(event.dropped)
                continue
            sample = event.decoded
            if sample is None and event.packet.type == TYPE_TRPC:
                self._wire(event.packet, event.outbound)
                continue
            if sample is None and event.packet.type == TYPE_NORMAL_TRACE:
                raw = event.packet.data
                known = not raw or raw[0] in {0, 1, 4}
                self.feed.publish(
                    "tpck",
                    "host_rx",
                    "Normal trace record",
                    hex=raw.hex(),
                    wire_hex=serialize(event.packet).hex(),
                    quality="error" if known else "partial",
                    reason="Invalid trace record length, port or native body"
                    if known
                    else "Unknown trace record kind",
                )
                continue
            if isinstance(sample, TrpcPacket) and not self.vendor:
                self._wire(event.packet, event.outbound)
                continue
            if isinstance(sample, TraceRx):
                self._native(
                    sample.frame,
                    "rx",
                    observed=not bool(sample.flags & RxFlag.EARLY_START),
                    ack=bool(sample.flags & RxFlag.ACK),
                    early_start=bool(sample.flags & RxFlag.EARLY_START),
                    wire_hex=serialize(event.packet).hex(),
                )
                continue
            if isinstance(sample, TraceTx):
                self._native(
                    sample.frame,
                    "tx",
                    observed=False,
                    origin=data(sample.origin),
                    outcome=data(sample.failure)
                    if sample.failure is not None
                    else "ack",
                    wire_hex=serialize(event.packet).hex(),
                )
                continue
            row = trace_record(event)
            if (
                self.vendor
                and not event.outbound
                and isinstance(sample, TrpcPacket)
                and sample.service
                in (
                    TrpcService.REPORT,
                    TrpcService.RESPONSE_REQUEST,
                    TrpcService.RESPONSE_UPDATE,
                )
            ):
                row["peer"] = self.feed.stock_report(sample)
            if isinstance(sample, TrpcPacket):
                row["spans"] = trpc_spans(event.packet, sample)
                row["quality"], row["reason"] = trpc_quality(event.packet, sample)
            if isinstance(sample, InvalidCapture):
                why = data(sample.reason)
                row.update(
                    quality="error",
                    reason=f"Invalid tN4 frame: {str(why).replace('_', ' ')}",
                )
            row["wire_hex"] = serialize(event.packet).hex()
            layer = str(row.pop("layer", "tpck"))
            direction = str(row.pop("direction", "rx"))
            method = row.get(
                "method",
                row.get("message_id", row.get("reason", row.get("type", "Packet"))),
            )
            value = row.pop("body_hex", row.pop("payload_hex", row.pop("bits_hex", "")))
            self.feed.publish(
                layer,
                direction,
                f"0x{method:X}" if isinstance(method, int) else str(method),
                hex=value,
                **row,
            )

    async def _raw_frames(self, raw: BridgeStream) -> None:
        while True:
            record = await raw.read()
            if isinstance(record, NativeFrame):
                self._native(record, "rx", observed=True)
            elif isinstance(record, CaptureGap):
                self.feed.gap(record.dropped)
            else:
                row = data(record)
                self.feed.publish(
                    "tn4",
                    "rx_invalid" if "bits" in row else "port_error",
                    str(row.get("reason", "Invalid")),
                    port=record.port,
                    hex=row.get("bits", ""),
                    fields=row,
                    quality="error",
                    reason=str(row.get("reason", "Unknown error")).replace("_", " "),
                )

    async def command(self, name: str, args: list[str], apply: bool) -> str:
        # Validate before interrupting capture. Never retry an operator operation.
        parsed = operations.arguments(self.endpoint, self.vendor, name, args, apply)
        if self.lock.locked():
            raise RuntimeError("Another operation is in progress")
        async with self.lock:
            await self.stop()
            self.feed.state(
                "Capture paused for operation", connection="busy", operation=name
            )
            try:
                with observe_bridge_packets(lambda p, tx: self._wire(p, tx)):
                    async with asyncio.timeout(300):
                        return await operations.execute(parsed)
            finally:
                self.feed.state(
                    "Resuming capture", connection="connecting", operation=None
                )
                self.start()

    async def action(self, name: str, values: Row, apply: bool) -> Any:
        if self.lock.locked():
            raise RuntimeError("Another operation is in progress")
        async with self.lock:
            if name == "monitor":
                mode, errors = values.get("mode"), values.get("errors")
                if (
                    mode not in ("normal", "raw")
                    or type(errors) is not bool
                    or (self.vendor and (mode != "normal" or errors))
                ):
                    raise ValueError("Unsupported monitoring configuration")

                def selected(
                    name: str, default: tuple[int, ...], limit: int
                ) -> tuple[int, ...]:
                    raw = values.get(name, list(default))
                    if not isinstance(raw, list) or any(
                        type(v) is not int or v not in range(limit) for v in raw
                    ):
                        raise ValueError(f"Invalid {name}")
                    return tuple(sorted(set(raw)))

                traced = selected("trace_ports", self.trace_ports, 5)
                acks = selected("ack_ports", self.ack_ports, 5)
                sources = tuple(
                    ErrorSource(s)
                    for s in selected(
                        "error_sources", tuple(int(s) for s in self.error_sources), 6
                    )
                )
                if (mode != "raw" and acks) or (errors and not sources):
                    raise ValueError(
                        "Link ACKs require Raw; enabled errors need at least one source"
                    )
                if (mode, errors, traced, acks, sources) != (
                    self.mode,
                    self.errors,
                    self.trace_ports,
                    self.ack_ports,
                    self.error_sources,
                ):
                    await self.stop()
                    (
                        self.mode,
                        self.errors,
                        self.trace_ports,
                        self.ack_ports,
                        self.error_sources,
                    ) = mode, errors, traced, acks, sources
                    self.start()
                return {"mode": mode, "errors": errors}
            if self.feed.status["connection"] != "online":
                raise RuntimeError("Gateway is not connected")
            if name == "inventory-read" and self.client:
                if self.inventory.read_lock.locked():
                    raise RuntimeError("An inventory read is already running")
                await self.inventory.refresh(self.client)
                return {
                    "inventory": self.feed.status.get("inventory"),
                    "count": len(self.inventory.devices),
                }
            if name == "device-setting" and apply and self.client:
                return await self.inventory.set(self.client, values)
            if name == "trace-state" and self.client and not self.vendor:
                return data(await BridgeManagement(self.client).trace_state())
            if name == "error-read" and self.diagnostics:
                counter = await self.diagnostics.counter(
                    ErrorSource(int(values["source"])), ErrorReason[values["reason"]]
                )
                return {**data(counter), "count": str(counter.count)}
            if name == "trpc" and self.client:
                method = TrpcMethod[values["method"]]
                service = values["service"]
                if service not in ("request", "update") or (
                    service == "update" and not apply
                ):
                    raise ValueError("A tRPC Update requires explicit Apply")
                fields_ = values.get("fields", {})
                if not isinstance(fields_, dict) or not all(
                    isinstance(k, str) and type(v) is int for k, v in fields_.items()
                ):
                    raise ValueError("tRPC fields must be integer key/value pairs")
                send = (
                    self.client.update_response
                    if service == "update"
                    else self.client.request_response
                )
                result = await send(method, fields=fields_)
                if result is None:
                    raise TimeoutError(
                        "No matching response; outcome unknown. Not retried."
                    )
                return {
                    "method": result.method_name,
                    "service": result.service_name,
                    "fields": result.body,
                    "extra": result.extra.hex(),
                }
            if name == "raw-send" and self.raw:
                if not apply:
                    raise ValueError("Raw Send requires explicit Apply")
                body, port = bytes.fromhex(values["hex"]), int(values["port"])
                if port not in range(5):
                    raise ValueError("Invalid port")
                async with asyncio.timeout(20):
                    outcome = await self.raw.send(port, body)
                return data(outcome)
            raise ValueError("Operation is not available in this mode")
