"""End devices explicitly identified by a frame, not inferred bus listeners."""

from tekmar_482 import NativeFrame, TrpcPacket
from tekmar_482.management import BridgeMethod

from .wire_layout import Values


def native_key(port: int, subnet: int, node: int) -> str:
    return f"{port}:{subnet:02X}:{node & 31:02X}"


def native_endpoints(
    frame: NativeFrame, fields: Values, *, received: bool
) -> dict[str, list[str]]:
    paths = (
        {"from": [native_key(frame.port, frame.subnet, frame.node)]} if received else {}
    )
    subnet = fields.get("target_subnet")
    if not isinstance(subnet, int) or "truncated" in fields:
        return paths
    if frame.message.name == "BlockRequest" and isinstance(
        mask := fields.get("target_nodes_mask"), str
    ):
        paths["to"] = [
            native_key(frame.port, subnet, n)
            for n in range(1, 32)
            if int(mask, 16) & (1 << (n - 1))
        ]
    elif (
        frame.message.name == "BlockUpdate"
        and isinstance(n := fields.get("target_node"), int)
        and n
    ):
        paths["to"] = [native_key(frame.port, subnet, n)]
    return paths


def trpc_endpoints(message: TrpcPacket) -> dict[str, list[str]]:
    if message.method_id == BridgeMethod.NORMAL_IDENTITY and len(message.extra) == 3:
        port, subnet, node = message.extra
        if port in range(5) and 1 <= node <= 31:
            return {"device": [native_key(port, subnet, node)]}
    if (address := message.get("address")) is not None and address != 0xFFFF:
        return {"device": [f"tha:{address}"]}
    return {}
