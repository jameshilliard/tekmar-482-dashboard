"""Paced read-only identity queries for sources actually seen in Normal trace."""

import asyncio
from collections.abc import Iterable

from tekmar_482 import BridgeManagement, NativePath, ProtocolError

from .feed import Feed

RETRY_SECONDS = (0, 15, 60)


async def identify_sources(
    feed: Feed, manager: BridgeManagement, ports: Iterable[int]
) -> None:
    attempts: dict[NativePath, tuple[int, float]] = {}
    loop = asyncio.get_running_loop()
    while True:
        for port in ports:
            # New observations outrank unanswered retries on the same bus.
            candidates = []
            for p in feed.peers.values():
                if p.get("port") != port or not p.get("node") or p.get("model"):
                    continue
                ident = p.get("identity") or {}
                if (
                    ident.get("product_number") is not None
                    and ident.get("j_number") is not None
                ):
                    continue  # Unknown product, but its identity is already on record.
                path = NativePath(port, p["subnet"], p["node"])
                count, sent = attempts.get(path, (0, 0.0))
                if (
                    count < len(RETRY_SECONDS)
                    and loop.time() - sent >= RETRY_SECONDS[count]
                ):
                    candidates.append((count, sent, path))
            if candidates:
                count, _, path = min(candidates, key=lambda c: (c[0], c[1]))
                try:
                    await manager.request_identity(path)
                except (ProtocolError, TimeoutError) as err:
                    feed.publish(
                        "system",
                        "info",
                        "Identity read not confirmed",
                        port=port,
                        fields={
                            "subnet": path.subnet,
                            "node": path.node,
                            "reason": str(err),
                        },
                    )
                attempts[path] = count + 1, loop.time()
        await asyncio.sleep(1)
