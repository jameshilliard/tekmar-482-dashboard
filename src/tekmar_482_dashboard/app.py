from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from tekmar_482 import (
    DeviceMode,
    ErrorReason,
    FrameCounts,
    NativePath,
    TekmarError,
    ThaSetback,
    TrafficCounters,
    TrpcMethod,
    WritableBlock,
)
from tekmar_482._native_payloads import DECODERS
from tekmar_482.trpc import method_spec

from .gateway import Gateway
from .operations import catalog


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=60)
    args: list[str] = Field(default_factory=list, max_length=80)
    values: dict[str, Any] = Field(default_factory=dict, max_length=20)
    confirmed: bool = False
    apply: bool = False


def origin_allowed(origin: str | None, host: str) -> bool:
    if origin is None:
        return True  # Non-browser clients may omit Origin.
    try:
        url = urlsplit(origin)
    except ValueError:
        return False
    return url.scheme in {"http", "https"} and url.netloc == host


def create_app(gateway: Gateway, token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await gateway.compat.start()
        gateway.start()
        try:
            yield
        finally:
            try:
                await gateway.compat.stop()
            finally:
                await gateway.stop()

    app = FastAPI(
        title="Tekmar 482 / tN4 console",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    assets = Path(__file__).parent / "static"
    local_names = {socket.gethostname(), socket.getfqdn(), "localhost"}
    app.mount("/static", StaticFiles(directory=assets), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            assets / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {**gateway.feed.snapshot(), "clients": gateway.compat.snapshot()}

    @app.get("/api/clients")
    async def clients() -> dict[str, Any]:
        return gateway.compat.snapshot()

    @app.get("/api/clients/{client_id}/trace")
    async def client_trace(client_id: int, after: int = 0) -> dict[str, Any]:
        history = gateway.compat.traces.get(client_id)
        if history is None:
            raise HTTPException(404, "Connection is no longer in the client history")
        return history.read(max(0, after))

    @app.get("/api/peers/{key}")
    async def peer(key: str) -> dict[str, Any]:
        if key not in gateway.feed.peers:
            raise HTTPException(404, "Source not observed on this connection")
        return gateway.feed.peers[key]

    @app.get("/api/devices/{address}")
    async def device(address: int) -> dict[str, Any]:
        row = gateway.feed.inventory.get(address)
        if row is None:
            raise HTTPException(404, "Device not in the current inventory")
        return {
            **row,
            "native_sources": [
                p
                for p in gateway.feed.peers.values()
                if p.get("address") == address
                and "node" in p
                and p.get("port") == row.get("port")
                and (
                    p.get("model") == row.get("model")
                    or not (p.get("model") or p.get("family"))
                )
            ],
        }

    @app.get("/api/catalog")
    async def commands() -> dict[str, Any]:
        return {
            "commands": catalog(gateway.vendor),
            "methods": [m.name for m in TrpcMethod],
            "method_fields": {
                m.name: [{"name": f.name, "width": f.size} for f in spec.fields]
                for m in TrpcMethod
                if (spec := method_spec(m))
            },
            "reasons": [r.name for r in ErrorReason],
            "vendor": gateway.vendor,
            "locked": token is not None,
            "modes": [m.name for m in DeviceMode],
            "setbacks": {s.name: int(s) for s in ThaSetback},
            "native_methods": {k: v[0] for k, v in DECODERS.items()},
            "blocks": [
                {"id": b.value, "name": b.name, "fields": [f.label for f in b.fields]}
                for b in WritableBlock
            ],
        }

    def traffic() -> TrafficCounters:
        if gateway.traffic is None:
            raise HTTPException(409, "Firmware traffic counters are unavailable")
        return gateway.traffic

    def counts(c: FrameCounts) -> dict[str, str]:
        # JSON numbers cannot preserve all 64 bits in a browser.
        return {
            k: str(getattr(c, k))
            for k in ("rx_valid", "tx_attempts", "tx_ack", "tx_no_ack", "tx_failed")
        }

    @app.get("/api/traffic/ports")
    async def traffic_ports() -> list[dict[str, Any]]:
        reader = traffic()
        result = []
        try:
            for port in range(5):
                t = await reader.port(port)
                result.append(
                    {
                        "port": port,
                        **counts(t.counts),
                        "rx_invalid": str(t.rx_invalid),
                        "rx_untracked": str(t.rx_untracked),
                        "tx_unattributed": str(t.tx_unattributed),
                    }
                )
        except (OSError, TekmarError, TimeoutError) as err:
            raise HTTPException(503, "Gateway counter read failed") from err
        return result

    @app.get("/api/traffic/devices")
    async def traffic_devices() -> list[dict[str, Any]]:
        try:
            return [
                {
                    "port": d.path.port,
                    "subnet": d.path.subnet,
                    "node": d.path.node,
                    **counts(d.counts),
                }
                async for d in traffic().devices()
            ]
        except (OSError, TekmarError, TimeoutError) as err:
            raise HTTPException(503, "Gateway counter read failed") from err

    @app.get("/api/traffic/{port}/{subnet}/{node}")
    async def traffic_device(
        port: int, subnet: int, node: int
    ) -> dict[str, str] | None:
        if port not in range(5) or subnet not in range(256) or node not in range(32):
            raise HTTPException(422, "Invalid native path")
        try:
            d = await traffic().device(NativePath(port, subnet, node))
        except (OSError, TekmarError, TimeoutError) as err:
            raise HTTPException(503, "Gateway counter read failed") from err
        return counts(d.counts) if d is not None else None

    def operator(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> None:
        if not origin_allowed(
            request.headers.get("origin"), request.headers.get("host", "")
        ):
            raise HTTPException(403, "Cross-origin operation denied")
        server = request.scope.get("server")
        if request.url.hostname not in local_names and (
            not server or request.url.hostname != server[0]
        ):
            raise HTTPException(403, "Unrecognized operator host")
        if token is not None and not secrets.compare_digest(
            (authorization or "").encode(), f"Bearer {token}".encode()
        ):
            raise HTTPException(401, "Operator key required")

    @app.post("/api/operator", dependencies=[Depends(operator)])
    async def authenticate() -> dict[str, bool]:
        return {"operator": True}

    @app.post("/api/operations", dependencies=[Depends(operator)])
    async def operate(op: Operation) -> dict[str, Any]:
        if not op.confirmed:
            raise HTTPException(422, "Explicit operator confirmation is required")
        try:
            if op.name in {
                "monitor",
                "trace-state",
                "error-read",
                "trpc",
                "raw-send",
                "inventory-read",
                "device-setting",
            }:
                result = await gateway.action(op.name, op.values, op.apply)
            else:
                result = await gateway.command(op.name, op.args, op.apply)
        except (KeyError, TypeError, ValueError) as err:
            raise HTTPException(422, str(err)) from err
        except TimeoutError as err:
            raise HTTPException(504, "Operation timed out; no automatic retry") from err
        except Exception as err:
            raise HTTPException(409, str(err) or type(err).__name__) from err
        gateway.feed.publish(
            "operator", "result", op.name, fields={"applied": op.apply}
        )
        return {"result": result}

    @app.websocket("/ws")
    async def events(ws: WebSocket) -> None:
        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        feed = gateway.feed

        async def stream() -> None:
            cursor = None
            while True:
                update = feed.snapshot(cursor)
                update["clients"] = gateway.compat.snapshot()
                cursor = update["seq"]
                ready = feed.changed
                await ws.send_json(update)
                try:
                    async with asyncio.timeout(10):
                        await ready.wait()
                except TimeoutError:
                    pass
                await asyncio.sleep(0.08)

        async def disconnect() -> None:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass  # No commands are accepted over the monitoring socket.

        tasks = [asyncio.create_task(stream()), asyncio.create_task(disconnect())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, OSError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app
