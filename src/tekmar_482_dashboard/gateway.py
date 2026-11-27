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
    TrafficCounters,
    TrpcMethod,
    TrpcPacket,
    TrpcService,
    open_gateway,
)
from tekmar_482.packet import TYPE_NORMAL_TRACE, TYPE_TN4, TYPE_TRPC
from tekmar_482.raw import InvalidCapture, PortError, TxResult, decode_raw_packet
from tekmar_482.wire_trace import observe_bridge_packets

from . import operations
from .devices import Inventory
from .endpoints import native_endpoints, trpc_endpoints
from .feed import Feed, Row, data
from .identity import identify_sources
from .native_layout import native_spans
from .trace_view import native_quality, trpc_quality, trpc_spans
from .wire_layout import Span, diagram, field, group, opaque


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
        self.traffic: TrafficCounters | None = None
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
                            if caps.features & BridgeFeature.TRAFFIC_COUNTS:
                                self.traffic = self.raw.traffic
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
                self.client = self.raw = self.diagnostics = self.traffic = None
            await asyncio.sleep(delay)
            delay = min(15, delay * 2)

    async def _normal(
        self, stack: AsyncExitStack, client: Tekmar482Client, flags: BridgeFeature
    ) -> None:
        manager = BridgeManagement(client)
        self.traffic = manager.traffic if flags & BridgeFeature.TRAFFIC_COUNTS else None
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
                    quality="info",
                    notice=f"{counter.source.name}: {counter.reason.name.replace('_', ' ').lower()}",
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
        self,
        packet: Packet,
        frame: NativeFrame,
        direction: str,
        *,
        observed: bool,
        prefix: list[Span],
        **meta: Any,
    ) -> None:
        decoded = frame.decoded
        key = self.feed.observe(frame, decoded) if observed else None
        if key and frame.message.name in {
            "AirGroupMemberStatus",
            "AirGroupMasterStatus",
            "SetpointStatus",
        }:
            self.inventory.observed_device(key)
        header, payload = native_spans(frame, decoded)
        quality, why = native_quality(frame, decoded, payload)
        if meta.get("outcome") not in (None, "ack"):
            meta["notice"] = "Gateway transmit: " + str(meta["outcome"]).replace(
                "_", " "
            )
        elif meta.get("early_start"):
            meta["notice"] = "Frame started before the minimum interframe gap"
        elif (
            frame.message.name == "NetworkError"
            and frame.message.payload
            and frame.message.payload[0]
        ):
            meta["notice"] = (
                f"Device fault {frame.message.payload[0]:#04x}: {decoded.get('description', 'unidentified')}"
            )
        kind = "Normal trace" if packet.type == TYPE_NORMAL_TRACE else "Raw"
        self.feed.publish(
            "host" if direction == "host_tx" else "tn4",
            direction,
            frame.message.name or f"0x{frame.message_id:03X}",
            message_id=frame.message_id,
            port=frame.port,
            subnet=frame.subnet,
            node=frame.node,
            peer=key,
            hex=frame.body.hex(),
            fields=decoded,
            endpoints=native_endpoints(frame, decoded, received=direction == "rx"),
            quality=quality,
            reason=why,
            **diagram(
                packet,
                group(
                    "Payload · " + kind,
                    [group("Record", prefix, column="record"), header, payload],
                ),
            ),
            **meta,
        )

    @staticmethod
    def _prefix(kind: str, port: int, ms: int | None = None) -> list[Span]:
        prefix = [field(1, kind, "service"), field(1, f"Bus {port}", "envelope")]
        if ms is not None:
            prefix.append(field(4, f"Gateway: {ms} ms", "envelope"))
        return prefix

    def _invalid(self, packet: Packet, record: InvalidCapture, **meta: Any) -> None:
        why = str(data(record.reason)).replace("_", " ")
        prefix = [
            *self._prefix("Invalid RX", record.port, record.timestamp_ms),
            field(1, f"Failure: {why}", "envelope"),
            field(2, f"Bit count: {record.bit_count}", "envelope"),
        ]
        parts = [group("Record", prefix, column="record")]
        if record.bits:
            captured = field(
                len(record.bits), "Captured tN4 bits (MSB first)", "unknown"
            )
            captured["column"] = "payload"
            captured["coverage"] = "invalid"
            parts.append(captured)
        self.feed.publish(
            "tn4",
            "rx_invalid",
            "Invalid tN4 frame",
            port=record.port,
            gateway_ms=record.timestamp_ms,
            bit_count=record.bit_count,
            hex=record.bits.hex(),
            quality="error",
            reason=f"Invalid tN4 frame: {why}",
            **diagram(packet, group("Payload · Invalid capture", parts)),
            **meta,
        )

    def _trpc(self, packet: Packet, sample: TrpcPacket, tx: bool, **meta: Any) -> None:
        parts = trpc_spans(packet, sample)
        quality, why, notice = trpc_quality(packet, sample, parts)
        peer = None
        if (
            self.vendor
            and not tx
            and sample.service
            in (
                TrpcService.REPORT,
                TrpcService.RESPONSE_REQUEST,
                TrpcService.RESPONSE_UPDATE,
            )
        ):
            peer = self.feed.stock_report(sample)
        name = parts[1]["label"]
        children = parts[:2]
        children[0]["column"] = "service"
        children[1]["column"] = "method"
        if parts[2:]:
            children.append(group("Parameters", parts[2:], column="payload"))
        self.feed.publish(
            "trpc",
            "host_tx" if tx else "host_rx",
            name,
            service=sample.service_name,
            hex=packet.data.hex(),
            fields=dict(sample.body),
            endpoints=trpc_endpoints(sample),
            peer=peer,
            quality=quality,
            reason=why,
            notice=notice,
            **diagram(packet, group("Payload · tRPC", children)),
            **meta,
        )

    def _wire(self, packet: Packet, tx: bool, *, captures: bool = True) -> None:
        direction = "host_tx" if tx else "host_rx"
        try:
            if packet.type == TYPE_TRPC:
                self._trpc(packet, TrpcPacket.from_packet(packet), tx)
                return
            if packet.type == TYPE_TN4 and len(packet.data) >= 2:
                raw, port = packet.data, packet.data[1]
                if tx and raw[0] == 1:
                    self._native(
                        packet,
                        NativeFrame(port, 0, raw[2:]),
                        direction,
                        observed=False,
                        prefix=self._prefix("Send", port),
                    )
                    return
                if not tx and raw[0] in (0, 4) and not captures:
                    return  # Delivered once by the stream, which also accounts for gaps.
                if not tx:
                    record = decode_raw_packet(packet)
                    if isinstance(record, NativeFrame):
                        self._native(
                            packet,
                            record,
                            "rx",
                            observed=True,
                            prefix=self._prefix("RX", port, record.timestamp_ms),
                            gateway_ms=record.timestamp_ms,
                        )
                    elif isinstance(record, InvalidCapture):
                        self._invalid(packet, record)
                    else:
                        self._raw_result(packet, record)
                    return
        except (ProtocolError, ValueError) as err:
            self.feed.publish(
                "tpck",
                direction,
                "Malformed packet",
                hex=packet.data.hex(),
                protocol="trpc" if packet.type == TYPE_TRPC else "gateway",
                quality="error",
                reason=str(err),
                **opaque(packet),
            )
            return
        self.feed.publish(
            "tpck",
            direction,
            f"Type {packet.type:#04x}",
            hex=packet.data.hex(),
            quality="partial",
            reason="Unrecognized TPCK packet type or service",
            **opaque(packet),
        )

    def _raw_result(self, packet: Packet, record: TxResult | PortError) -> None:
        name = "Port error" if isinstance(record, PortError) else "TX result"
        parts = self._prefix(name, record.port)
        if isinstance(record, TxResult):
            parts.append(field(1, f"Outcome: {record.status.name}", "field"))
        why = record.reason
        if why is not None:
            reason = field(1, f"Reason: {data(why)}")
            if type(why) is int:
                reason["coverage"] = "unknown"
            parts.append(reason)
        self.feed.publish(
            "host",
            "host_rx",
            name,
            port=record.port,
            hex=packet.data.hex(),
            fields=data(record),
            quality="partial" if type(why) is int else "good",
            reason="Unknown reason code" if type(why) is int else "Valid link result",
            notice=str(data(why)).replace("_", " ") if why is not None else None,
            **diagram(packet, group("Payload · Raw", parts)),
        )

    async def _normal_frames(self, trace: NormalTrace) -> None:
        async for event in trace:
            if isinstance(event, CaptureGap):
                self.feed.gap(event.dropped)
                continue
            packet = event.packet
            stamp = event.host_monotonic_ns
            if packet.type == TYPE_TRPC:
                self._wire(packet, event.outbound)
                continue
            sample = event.decoded
            if isinstance(sample, (TraceRx, TraceTx)):
                frame = sample.frame
                rx = isinstance(sample, TraceRx)
                prefix = self._prefix(
                    "RX" if rx else "TX attempt", frame.port, frame.timestamp_ms
                )
                meta: Row = {
                    "gateway_ms": frame.timestamp_ms,
                    "host_monotonic_ns": stamp,
                }
                if isinstance(sample, TraceRx):
                    meta.update(
                        ack=bool(sample.flags & RxFlag.ACK),
                        early_start=bool(sample.flags & RxFlag.EARLY_START),
                    )
                    flags = field(1, "RX flags", "envelope")
                    flags["bits"] = [
                        {"hi": 7, "lo": 2, "label": "Reserved"},
                        {"hi": 1, "lo": 1, "label": "Early start"},
                        {"hi": 0, "lo": 0, "label": "ACK"},
                    ]
                    prefix.append(flags)
                else:
                    meta.update(
                        origin=data(sample.origin),
                        outcome=data(sample.failure)
                        if sample.failure is not None
                        else "ack",
                    )
                    prefix.extend(
                        [
                            field(1, f"Origin: {meta['origin']}", "envelope"),
                            field(1, f"Outcome: {meta['outcome']}", "envelope"),
                        ]
                    )
                self._native(
                    packet,
                    frame,
                    "rx" if rx else "tx",
                    observed=rx and not meta.get("early_start"),
                    prefix=prefix,
                    **meta,
                )
            elif isinstance(sample, InvalidCapture):
                self._invalid(packet, sample, host_monotonic_ns=stamp)
            elif packet.type == TYPE_NORMAL_TRACE:
                known = not packet.data or packet.data[0] in (0, 1, 4)
                self.feed.publish(
                    "tpck",
                    "host_rx",
                    "Normal trace record",
                    hex=packet.data.hex(),
                    protocol="tn4",
                    quality="error" if known else "partial",
                    reason="Invalid trace record length, port or native body"
                    if known
                    else "Unknown trace record kind",
                    host_monotonic_ns=stamp,
                    **opaque(packet),
                )
            else:
                self._wire(packet, event.outbound)

    async def _raw_frames(self, raw: BridgeStream) -> None:
        while True:
            record = await raw.read()
            if isinstance(record, CaptureGap):
                self.feed.gap(record.dropped)
            elif isinstance(record, NativeFrame):
                prefix = bytes((0, record.port)) + record.timestamp_ms.to_bytes(
                    4, "little"
                )
                self._native(
                    Packet(TYPE_TN4, prefix + record.body),
                    record,
                    "rx",
                    observed=True,
                    prefix=self._prefix("RX", record.port, record.timestamp_ms),
                    gateway_ms=record.timestamp_ms,
                )
            elif isinstance(record, InvalidCapture):
                prefix = (
                    bytes((4, record.port))
                    + record.timestamp_ms.to_bytes(4, "little")
                    + bytes((record.reason,))
                    + record.bit_count.to_bytes(2, "little")
                )
                self._invalid(Packet(TYPE_TN4, prefix + record.bits), record)

    async def command(self, name: str, args: list[str], apply: bool) -> object:
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
                state = await BridgeManagement(self.client).trace_state()
                return {
                    "port_mask": state.port_mask,
                    "gateway_drops": [str(n) for n in state.gateway_drops],
                }
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
