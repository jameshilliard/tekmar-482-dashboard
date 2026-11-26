"""Read-only inventory and capability-checked, explicitly requested settings."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from tekmar_482 import (
    AvailableInfo,
    DeviceAttributeBit,
    DeviceMode,
    DiscoveredDevice,
    GatewaySnapshot,
    Tekmar482Client,
    Tekmar482State,
    ThaSetback,
    TrpcCommand,
    TrpcPacket,
)
from tekmar_482.state import MessageApplyResult
from tekmar_482.units import degh_to_fahrenheit

from .feed import Feed, Row, data


def controls(device: DiscoveredDevice) -> list[str]:
    choices = {
        "heat": device.supports_heat_setpoint,
        "cool": device.supports_cool_setpoint,
        "floor": device.supports_slab_setpoint,
        "target": device.supports_setpoint_device,
        "fan": device.supports_fan_percent,
        "mode": device.supports_mode_setting,
        "humidity_min": device.attributes.humidity_setpoint_min,
        "humidity_max": device.attributes.humidity_setpoint_max,
    }
    return [name for name, active in choices.items() if active]


class Inventory:
    def __init__(self, feed: Feed) -> None:
        self.feed = feed
        self.devices: dict[int, DiscoveredDevice] = {}
        self.state: Tekmar482State | None = None
        self.dirty = asyncio.Event()
        self._seen: set[str] = set()
        self.read_lock = asyncio.Lock()

    def observed_device(self, key: str) -> None:
        if key not in self._seen:
            self._seen.add(key)
            self.dirty.set()

    async def watch(self, client: Tekmar482Client) -> None:
        # A cold gateway may have no tHA inventory until its first native reports.
        while True:
            self.dirty.clear()
            await self.refresh(client)
            await self.dirty.wait()
            await asyncio.sleep(2)

    def observe(self, packet: TrpcPacket, command: TrpcCommand | None) -> None:
        if (
            self.state is not None
            and self.state.apply_message_result(packet, command)
            is MessageApplyResult.CHANGED
        ):
            address = packet.get("address")
            if address is not None:
                self._runtime(address)

    def _runtime(self, address: int) -> None:
        if self.state is None or (runtime := self.state.runtime(address)) is None:
            return
        row = self.feed.inventory.get(address)
        if row is not None:
            row["updated_seq"] = self.feed.seq + 1
            row["settings"] = data(runtime.decoded_values)
            row["raw_settings"] = data(runtime.raw_values)
            row["temperature_f"] = (
                degh_to_fahrenheit(runtime.current_temperature)
                if runtime.current_temperature is not None
                else None
            )

    async def refresh(self, client: Tekmar482Client) -> None:
        async with self.read_lock:
            await self._refresh(client)

    async def _refresh(self, client: Tekmar482Client) -> None:
        try:
            self.feed.status["inventory"] = "reading metadata"
            discovery = await client.discover(
                timeout=60, manage_reporting=False, setback_enable=None
            )
            previous = self.devices
            self.devices = {d.address: d for d in discovery.devices}
            self.feed.inventory = {
                d.address: {
                    "key": f"tha:{d.address}",
                    "updated_seq": self.feed.seq + 1,
                    "address": d.address,
                    "port": d.address_parts[0],
                    "model": d.model,
                    "family": d.kind.value if d.kind else None,
                    "settings": {},
                    **(
                        {
                            k: v
                            for k, v in self.feed.inventory.get(d.address, {}).items()
                            if k in {"settings", "raw_settings", "temperature_f"}
                        }
                        if previous.get(d.address) == d
                        else {}
                    ),
                    "metadata": data(d),
                    "capabilities": {
                        b.name.lower(): d.attributes.supports(b)
                        for b in DeviceAttributeBit
                        if b.name
                    }
                    if d.attributes.raw is not None
                    else {},
                    "controls": controls(d),
                    "fan_step": d.fan_percent_step,
                    "setback_events": d.setback_events,
                }
                for d in discovery.devices
            }
            self.feed.status["inventory"] = "reading configuration"
            self.feed.publish(
                "system",
                "info",
                "Inventory received",
                fields={"devices": len(self.devices)},
            )
            records = await client.poll_devices(
                discovery.devices,
                protocol_version=discovery.gateway.protocol_version,
                timeout=2,
                include_setbacks=True,
            )
            self.state = Tekmar482State(
                discovery, AvailableInfo(GatewaySnapshot(discovery.gateway), records)
            )
            for address in self.devices:
                self._runtime(address)
            self.feed.status["inventory"] = "ready"
            self.feed.publish(
                "system",
                "info",
                "Configuration received",
                fields={"devices": len(records)},
            )
        except Exception as err:
            self.feed.status["inventory"] = "incomplete"
            self.feed.publish(
                "system",
                "info",
                "Inventory incomplete",
                fields={"reason": str(err) or type(err).__name__},
            )

    async def set(self, client: Tekmar482Client, values: Row) -> Any:
        address = int(values["address"])
        device = self.devices.get(address)
        param = values["setting"]
        if device is None or param not in controls(device):
            raise ValueError("Setting is not supported by the discovered device")
        value = values["value"]
        if param in {"fan", "humidity_min", "humidity_max"} and type(value) is not int:
            raise ValueError("Percentage must be a whole number")
        slot = ThaSetback(int(values.get("setback", ThaSetback.CURRENT)))
        if param == "mode":
            result = await client.set_mode(address, DeviceMode[value], confirm=True)
        elif param in {"heat", "cool", "floor", "target"}:
            temp = float(value)
            if not math.isfinite(temp):
                raise ValueError("Temperature must be finite")
            setter = {
                "heat": client.set_latest_heat_setpoint_celsius,
                "cool": client.set_latest_cool_setpoint_celsius,
                "floor": client.set_latest_slab_setpoint_celsius,
                "target": client.set_latest_setpoint_device_celsius,
            }[param]
            result = await setter(address, temp, setback=slot, confirm=True)
        elif param == "fan":
            percent = device.encode_fan_percent(int(value))
            result = await client.set_latest_fan_percent(
                address, percent, setback=slot, confirm=True
            )
        else:
            humidity = (
                client.set_humidity_min
                if param == "humidity_min"
                else client.set_humidity_max
            )
            result = await humidity(address, int(value))
        if result is None:
            raise TimeoutError("Device setting was not confirmed; not retried")
        return {
            "address": address,
            "method": result.method_name,
            "service": result.service_name,
            "fields": dict(result.body),
        }
