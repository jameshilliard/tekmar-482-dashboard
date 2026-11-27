"""Bounded one-shot operations using the client project's own parser and asyncio API."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from io import StringIO
from typing import Any, Never

from tekmar_482 import GatewayEndpoint
from tekmar_482.cli import build_parser, run

# New client commands must be classified before becoming available to a browser.
GROUPS = {
    "Inspection": (
        "gateway-info",
        "discover",
        "dump",
        "read-block",
        "read-device",
        "read-sensors",
        "read-water",
        "read-status",
        "read-schedule",
        "write-fields",
    ),
    "Device controls": (
        "set-climate-mode",
        "set-thermostat-target",
        "set-device-group",
        "set-snowmelt-group",
        "write-schedule",
        "write-block",
        "scene-response",
    ),
    "Line controls": (
        "network-init",
        "select-scene",
        "set-clock",
        "set-group",
        "set-zone-sync",
    ),
    "Gateway": ("panel", "led-test", "probe", "diagnostics", "update"),
}
VENDOR = {"gateway-info", "discover", "dump"}
PROTECTED = {
    "--tcp",
    "--serial",
    "--baudrate",
    "--vendor",
    "--auto-update",
    "--json",
    "--from-rom",
    "--manage-reporting",
    "--enable-setback",
    "--disable-setback",
    "--confirm",
    "--help",
    "-h",
}
REQUIRES_CONFIRM = {*GROUPS["Device controls"], *GROUPS["Line controls"], "update"}


def parsers() -> dict[str, argparse.ArgumentParser]:
    root = build_parser()
    return next(
        a.choices for a in root._actions if isinstance(a, argparse._SubParsersAction)
    )


def parameters(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    result = []
    for action in parser._actions:
        if any(opt in PROTECTED for opt in action.option_strings):
            continue
        result.append(
            {
                "name": action.dest,
                "flag": next(iter(action.option_strings), None),
                "negative": next(
                    (o for o in action.option_strings if o.startswith("--no-")), None
                ),
                "required": action.required,
                "choices": list(action.choices) if action.choices is not None else [],
                "switch": action.nargs == 0,
                "repeat": isinstance(action, argparse._AppendAction),
                "many": action.nargs in {"+", "*"},
                "numeric": action.type in {int, float}
                or getattr(action.type, "__name__", "") in {"number", "_int_value"},
                "help": action.help or "",
            }
        )
    return result


def catalog(vendor: bool) -> list[dict[str, Any]]:
    choices = parsers()
    return [
        {
            "name": name,
            "group": group,
            "parameters": parameters(choices[name]),
            "can_apply": name in REQUIRES_CONFIRM,
        }
        for group, names in GROUPS.items()
        for name in names
        if not vendor or name in VENDOR
    ]


def arguments(
    endpoint: GatewayEndpoint,
    vendor: bool,
    name: str,
    words: Sequence[str],
    apply: bool,
) -> argparse.Namespace:
    if name not in {n for ns in GROUPS.values() for n in ns} or (
        vendor and name not in VENDOR
    ):
        raise ValueError("Unsupported operation for this firmware")
    if len(words) > 80 or any(len(w) > 512 for w in words):
        raise ValueError("Too many or oversized arguments")
    if any(
        w.split("=")[0] in PROTECTED or w == "--" or w.startswith("-h") for w in words
    ):
        raise ValueError(
            "Transport, firmware and confirmation flags are server controlled"
        )
    if apply and name not in REQUIRES_CONFIRM:
        raise ValueError("This operation does not accept Apply")
    parser = build_parser()
    parser.allow_abbrev = False
    sub = next(
        a.choices[name]
        for a in parser._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    sub.allow_abbrev = False

    # Do not let argparse print help or terminate the ASGI process on bad input.
    def reject(message: str) -> Never:
        raise ValueError(message)

    parser.error = reject  # type: ignore[method-assign]
    sub.error = reject
    connection = (
        ["--serial", endpoint.address]
        if endpoint.port is None
        else ["--tcp", f"{endpoint.address}:{endpoint.port}"]
    )
    argv = [name, *words, *connection, "--baudrate", str(endpoint.baudrate), "--json"]
    if name == "write-fields":  # a local schema query with no transport
        argv = [name, *words, "--json"]
    if vendor:
        argv.append("--vendor")
    if name == "update" and not apply:
        raise ValueError("Firmware update requires explicit Apply")
    if apply and name != "update":
        argv.append("--confirm")
    return parser.parse_args(argv)


class Output(StringIO):
    def write(self, s: str, /) -> int:
        if self.tell() + len(s) > 2_000_000:
            raise ValueError("Operation output exceeded the display limit")
        return super().write(s)


async def execute(args: argparse.Namespace) -> str:
    out = Output()
    code = await run(args, stdout=out)
    if code:
        raise RuntimeError(f"Client operation returned {code}: {out.getvalue()}")
    return out.getvalue().strip()
