from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

import uvicorn
from tekmar_482 import GatewayEndpoint

from .app import create_app
from .gateway import Gateway


def operator_key(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.stat().st_mode & 0o077:
            raise ValueError(
                "The operator key file must be private (chmod 600)"
            ) from None
        key = path.read_text().strip()
        if len(key) < 32:
            raise ValueError(
                "The operator key must contain at least 32 characters"
            ) from None
        return key
    key = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as handle:
        handle.write(key + "\n")
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local tN4 dashboard")
    link = parser.add_mutually_exclusive_group(required=True)
    link.add_argument("--serial", metavar="DEVICE")
    link.add_argument("--tcp", metavar="HOST:PORT")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--vendor", action="store_true")
    parser.add_argument("--mode", choices=("normal", "raw"), default="normal")
    parser.add_argument(
        "--errors",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable error reporting (default on for custom firmware)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8482)
    parser.add_argument(
        "--key-file",
        type=Path,
        help="optionally require an operator key for commands (default unlocked)",
    )
    args = parser.parse_args()
    if args.vendor and (args.errors or args.mode != "normal"):
        parser.error(
            "Stock firmware cannot provide native capture or gateway error reports"
        )
    if args.tcp:
        address, sep, port = args.tcp.rpartition(":")
        if not sep or not address or not port.isdecimal():
            parser.error("expected HOST:PORT")
        endpoint = GatewayEndpoint.tcp(
            address.strip("[]"), int(port), baudrate=args.baudrate
        )
    else:
        endpoint = GatewayEndpoint.serial(args.serial, baudrate=args.baudrate)
    gateway = Gateway(
        endpoint,
        vendor=args.vendor,
        mode=args.mode,
        errors=args.errors if args.errors is not None else not args.vendor,
    )
    uvicorn.run(
        create_app(gateway, operator_key(args.key_file) if args.key_file else None),
        host=args.host,
        port=args.port,
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
