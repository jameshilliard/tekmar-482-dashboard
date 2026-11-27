"""Optional LAN packet-server view of the shared Normal tRPC connection."""

from __future__ import annotations

import asyncio
from binascii import Error as HexError
from binascii import unhexlify
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from time import time
from typing import Any

from tekmar_482 import Packet, Tekmar482Client, TekmarError, TrpcMethod, TrpcService
from tekmar_482.packet import TYPE_TRPC
from tekmar_482.trpc import method_spec

# The upstream integration looks up these IDs by name. Never expose a new
# gateway method merely because our library learns to decode it.
_METHODS = frozenset(
    {
        TrpcMethod.NULL_METHOD,
        TrpcMethod.NETWORK_ERROR,
        TrpcMethod.REPORTING_STATE,
        TrpcMethod.OUTDOOR_TEMP,
        TrpcMethod.DEVICE_ATTRIBUTES,
        TrpcMethod.MODE_SETTING,
        TrpcMethod.ACTIVE_DEMAND,
        TrpcMethod.CURRENT_TEMPERATURE,
        TrpcMethod.CURRENT_FLOOR_TEMPERATURE,
        TrpcMethod.SETPOINT_GROUP_ENABLE,
        TrpcMethod.SETPOINT_DEVICE,
        TrpcMethod.HEAT_SETPOINT,
        TrpcMethod.COOL_SETPOINT,
        TrpcMethod.SLAB_SETPOINT,
        TrpcMethod.RELATIVE_HUMIDITY,
        TrpcMethod.HUMIDITY_SET_MAX,
        TrpcMethod.HUMIDITY_SET_MIN,
        TrpcMethod.FAN_PERCENT,
        TrpcMethod.TAKING_ADDRESS,
        TrpcMethod.DEVICE_INVENTORY,
        TrpcMethod.SETBACK_ENABLE,
        TrpcMethod.SETBACK_STATE,
        TrpcMethod.SETBACK_EVENTS,
        TrpcMethod.FIRMWARE_REVISION,
        TrpcMethod.PROTOCOL_VERSION,
        TrpcMethod.DEVICE_TYPE,
        TrpcMethod.DEVICE_VERSION,
        TrpcMethod.DATE_TIME,
    }
)
_HOST = {TrpcService.REQUEST, TrpcService.UPDATE}
_GATEWAY = {
    TrpcService.REPORT,
    TrpcService.RESPONSE_REQUEST,
    TrpcService.RESPONSE_UPDATE,
}
_MAX_LINE = 2 * (1 + 255) + 2  # type + largest TPCK payload, with CRLF
_OUTBOX = 256
_MAX_PEERS = 16


def supported(packet: Packet, *, from_client: bool) -> TrpcMethod | None:
    data = packet.data
    if (
        packet.type != TYPE_TRPC
        or len(data) < 5
        or data[0] not in (_HOST if from_client else _GATEWAY)
    ):
        return None
    code = int.from_bytes(data[1:5], "little")
    if code not in _METHODS:
        return None
    method = TrpcMethod(code)
    spec = method_spec(method)
    assert spec is not None
    size = len(data) - 5
    if from_client:
        # Upstream writes all fields, including request placeholders. Also accept
        # compact enquiries with just their selector, never a truncated update.
        minimum = 0
        if "address" in spec.field_names:
            minimum = 3 if "setback" in spec.field_names else 2
        elif method is TrpcMethod.SETPOINT_GROUP_ENABLE:
            minimum = 1
        if size != spec.size and not (
            data[0] == TrpcService.REQUEST and size == minimum
        ):
            return None
    elif size < spec.size:
        return None
    return method


@dataclass(slots=True)
class ClientInfo:
    id: int
    peer: str
    connected: float = field(default_factory=time)
    activity: float | None = None
    received: int = 0  # forwarded client -> gateway
    sent: int = 0  # drained gateway -> client
    filtered: int = 0  # unsupported or malformed client input
    last_request: str | None = None
    last_reply: str | None = None
    closed: float | None = None
    reason: str | None = None


@dataclass(slots=True)
class Peer:
    info: ClientInfo
    writer: asyncio.StreamWriter
    outbox: asyncio.Queue[tuple[bytes, str]] = field(
        default_factory=lambda: asyncio.Queue(_OUTBOX)
    )


class Compatibility:
    def __init__(
        self, host: str, port: int | None, changed: Callable[[], None]
    ) -> None:
        self.host, self.port, self.changed = host, port, changed
        self.server: asyncio.Server | None = None
        self.client: Tekmar482Client | None = None
        self.peers: dict[asyncio.Task[None], Peer] = {}
        self.recent: deque[ClientInfo] = deque(maxlen=16)
        self.total = self.withheld = 0

    async def start(self) -> None:
        if self.port is not None:
            self.server = await asyncio.start_server(
                self._accept, self.host, self.port, limit=_MAX_LINE
            )

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            self.disconnect("Listener stopped")
            await self.server.wait_closed()
            await asyncio.gather(*tuple(self.peers), return_exceptions=True)
            self.server = None

    @contextmanager
    def attach(self, client: Tekmar482Client) -> Iterator[None]:
        if self.port is None:
            yield
            return
        self.client = client
        try:
            with client.session.observe_received_packets(self.publish):
                yield
        finally:
            self.client = None
            self.disconnect("Normal gateway connection ended")

    def disconnect(self, reason: str) -> None:
        for task, peer in tuple(self.peers.items()):
            peer.info.reason = reason
            peer.writer.close()
            task.cancel()

    def snapshot(self) -> dict[str, Any]:
        bound = (
            self.server.sockets[0].getsockname()
            if self.server and self.server.sockets
            else None
        )
        return {
            "listening": bool(bound),
            "available": self.client is not None,
            "host": self.host if self.port is not None else None,
            "port": bound[1] if bound else self.port,
            "total": self.total,
            "withheld": self.withheld,
            "active": [asdict(p.info) for p in self.peers.values()],
            "recent": [asdict(i) for i in self.recent],
        }

    def publish(self, packet: Packet) -> None:
        if not self.peers:
            return
        method = supported(packet, from_client=False)
        if method is None:
            self.withheld += 1
            return
        line = (bytes([packet.type]) + packet.data).hex().upper().encode(
            "ascii"
        ) + b"\n"
        item = line, TrpcService(packet.data[0]).name + " " + method.name
        for task, peer in self.peers.items():
            if not peer.writer.is_closing():
                try:
                    peer.outbox.put_nowait(item)
                except asyncio.QueueFull:
                    peer.info.reason = "Client not reading"
                    peer.writer.close()
                    task.cancel()

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.client is None or self.server is None or len(self.peers) >= _MAX_PEERS:
            writer.close()
            return
        self.total += 1
        host, port, *_ = writer.get_extra_info("peername")
        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        peer = Peer(ClientInfo(self.total, address), writer)
        task = asyncio.create_task(self._serve(reader, peer, self.client))
        self.peers[task] = peer
        task.add_done_callback(self._finished)
        self.changed()

    def _finished(self, task: asyncio.Task[None]) -> None:
        peer = self.peers.pop(task)
        peer.writer.close()
        peer.info.closed = time()
        if not task.cancelled() and (error := task.exception()) is not None:
            peer.info.reason = str(error) or type(error).__name__
        peer.info.reason = peer.info.reason or "Client disconnected"
        self.recent.appendleft(peer.info)
        self.changed()

    async def _serve(
        self, reader: asyncio.StreamReader, peer: Peer, client: Tekmar482Client
    ) -> None:
        tasks = [
            asyncio.create_task(self._receive(reader, peer, client)),
            asyncio.create_task(self._send(peer)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (ValueError, OSError, TimeoutError, TekmarError) as err:
            peer.info.reason = str(err) or type(err).__name__
        finally:
            peer.writer.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(OSError):
                await peer.writer.wait_closed()

    async def _receive(
        self, reader: asyncio.StreamReader, peer: Peer, client: Tekmar482Client
    ) -> None:
        while line := await reader.readline():
            if not line.endswith(b"\n") or len(line) > _MAX_LINE:
                raise ValueError("Incomplete or oversized packet line")
            try:
                raw = unhexlify(line[:-1].removesuffix(b"\r"))
            except HexError:
                raw = b""
            packet = Packet(raw[0], raw[1:]) if 0 < len(raw) <= 256 else None
            method = supported(packet, from_client=True) if packet else None
            peer.info.activity = time()
            if packet is None or method is None:
                peer.info.filtered += 1
                self.changed()
                continue
            if self.client is not client:
                raise ConnectionError("Normal gateway connection ended")
            service = TrpcService(packet.data[0])
            if method is TrpcMethod.DEVICE_INVENTORY and service is TrpcService.REQUEST:
                # This exact two-byte request uses the session's serialized walk.
                # Its original RX packets (including the sentinel) go through publish.
                await client.get_inventory_addresses(
                    start_address=int.from_bytes(packet.data[5:], "little"), timeout=30
                )
            else:
                async with asyncio.timeout(30):
                    await client.write_packet(packet)
            peer.info.received += 1
            peer.info.last_request = service.name + " " + method.name
            self.changed()

    async def _send(self, peer: Peer) -> None:
        while True:
            line, name = await peer.outbox.get()
            peer.writer.write(line)
            async with asyncio.timeout(10):
                await peer.writer.drain()
            peer.info.sent += 1
            peer.info.activity, peer.info.last_reply = time(), name
            self.changed()
