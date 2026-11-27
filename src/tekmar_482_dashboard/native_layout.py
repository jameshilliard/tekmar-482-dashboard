"""Display provenance: byte order and packed fields for native reports."""

from dataclasses import dataclass

from tekmar_482 import NativeFrame
from tekmar_482._native_payloads import block_fields, control_words
from tekmar_482._native_payloads.layouts import FIXED_BLOCKS

from .wire_layout import Bit, Span, Values, group, span

type BitField = tuple[str, int] | tuple[str, int, str]


@dataclass(frozen=True)
class Packed:
    label: str
    # Least significant field first; the display orders bit 7 on the left.
    groups: tuple[BitField, ...]


def flags(label: str, *groups: BitField) -> Packed:
    return Packed(label, groups)


type Field = str | tuple[str, int] | Packed
STATUS = flags(
    "Status", ("Enabled", 1), ("Setback", 3), ("Optimum start", 2), ("Priority", 2)
)


def air_status(side: str = "") -> Packed:
    prefix = side + "_" if side else ""
    return flags(
        (side.title() + " status") if side else "Air status",
        ("Enabled", 1, prefix + "enabled" if side else "heat_cool_enabled"),
        ("Demand", 1, prefix + "demand" if side else "heat_cool_demand"),
        ("Schedule", 3, prefix + "schedule" if side else "schedule_status"),
        ("Optimum start", 2, prefix + "optimum_start"),
        ("Weather shutdown", 1, prefix + "weather_shutdown"),
    )


WATER = flags("Water subnet", ("Bus", 3), ("Control", 5))
NODE = flags("Destination", ("Node", 5), ("Flags", 3))
SUPPORT = flags("Supported slots", *((f"Slot {i + 1}", 1) for i in range(8)))
SIMPLE: dict[str, tuple[Field, ...]] = {
    "NetworkError": (
        "code",
        flags("Fault descriptor", ("Instance", 6), ("Priority", 2)),
    ),
    "WaterCycleStart": (flags("Water cycle", ("Length", 7), ("Hold override", 1)),),
    "MaximumBoilerPercent": ("max_value",),
    "RelayStatusHeartbeat": (WATER,),
    "GeneralDemand": (
        WATER,
        flags("Demand", ("Reserved", 6), ("Cooling", 1), ("Demand", 1)),
    ),
    "StageHeatDemand": (
        WATER,
        flags(
            "Demand",
            ("Stage", 1),
            ("Override", 2),
            ("First report", 1),
            ("Setpoint", 1),
            ("DHW", 1),
            ("Priority", 2),
        ),
        flags("Output", ("Pump delay", 6), ("Pump not required", 1), ("Output on", 1)),
    ),
    "DHWStatus": (
        STATUS,
        flags(
            "Operation", ("Actuator", 2), ("Demand", 1), ("Heat/cool", 2), ("Mode", 3)
        ),
        "target_temp",
        "tank_temp",
    ),
    "TankStatus": ("tank_id", "pump_output_pct", "tank_temp_raw", "target_temp_raw"),
    "SetpointStatus": (
        STATUS,
        flags(
            "Operation",
            ("Actuator", 2),
            ("Demand", 1),
            ("Cooling", 1),
            ("Mode", 2),
            ("Priority", 2),
        ),
        ("target_temp_raw", 2),
        ("temp1_raw", 2),
        ("temp2_raw", 2),
        flags(
            "Enable", ("Aux relay", 2), ("Requested", 2), ("Actual", 2), ("Reserved", 2)
        ),
        "status_excuse1",
        "status_excuse2",
    ),
    "BlockRequest": (
        ("block_id", 2),
        "target_subnet",
        ("target_nodes_mask", 4),
        ("arguments", 4),
    ),
    "FanDuctStatus": (
        flags(
            "Fan status",
            ("Fan 1", 2),
            ("Fan 2", 2),
            ("Optimum start", 2),
            ("Freeze protect", 1),
            ("Reserved", 1),
        ),
        ("duct_temp", 2),
        ("coil_temp", 2),
    ),
    "SlabStatus": (
        flags("Slab", ("Status", 3), ("Reserved", 5)),
        "target",
        ("current", 2),
    ),
    "ScheduleStatus": (
        flags(
            "Owner",
            ("Schedule", 2),
            ("Master", 1),
            ("Previous ID", 2),
            ("Previous master", 1),
            ("Events", 2),
        ),
        flags(
            "Schedule", ("Current", 2), ("Next", 2), ("Duplicate", 1), ("Reserved", 3)
        ),
        "time_to_next_edge",
    ),
    "RealTimeClockReport": (
        "minute",
        flags("Clock", ("Weekday", 3), ("Hour", 5)),
        flags("Date", ("Month index", 4), ("DST", 1), ("Reserved", 3)),
        "day_raw",
        "year_from_2000",
        "dst_mode",
    ),
    "OutdoorTemperatureReport": (("outdoor_temp", 2), ("second_word_raw", 2)),
    "AirGroupMasterConfig": (
        flags("Group", ("Group", 5), ("Reserved", 1), ("No reply", 1), ("Master", 1)),
        flags("Previous group", ("Group", 5), ("Reserved", 2), ("Master", 1)),
    ),
    "CycleLengthRequest": ("air_group_id", "requested_length"),
    "NetworkLock": (
        flags("Locks", ("Access lock", 1), ("Address lock", 1), ("Unmapped", 6)),
    ),
    "SecureGatewayAccess": (
        flags("Access", ("Request", 1), ("Open", 1), ("Unmapped", 6)),
    ),
    "SetpointRemoteEnable": ("group_id", "enable"),
    "SetpointEnableReport": (
        "group_id",
        flags(
            "Enable", ("Requested", 1), ("Unmapped", 3), ("Actual", 1), ("Unmapped", 3)
        ),
    ),
    "ZoneSynchAdjustment": ("synch_cycle", "purging", "target_mode"),
    "TakingAddress": (
        "previous_subnet",
        flags("Address", ("Previous node", 5), ("Locked", 1), ("Unmapped", 2)),
    ),
    "RHQuery": ("target_subnet", ("node_selector", 4)),
    "CalibrationMode": ("mode",),
    "NetworkTestTime": (("additional_time", 2),),
    "NetworkTestMode": ("test_mode", ("expected_duration", 2)),
    "IORequest": ("subnet_id", "node_id", "channel_id"),
    "IODiscoverRequest": ("subnet_id", "node_id"),
    "IORequestResponse": (
        "subnet_id",
        "node_id",
        "channel_id",
        "type_id",
        ("io_value", 2),
    ),
    "IOChangeReport": (
        "subnet_id",
        "node_id",
        "channel_id",
        "type_id",
        ("io_value", 2),
    ),
    "IODiscoverReport": (
        "subnet_id",
        "node_id",
        "channel_id",
        "channel_count",
        ("label_hex", 6),
        "type_id",
        ("io_value", 2),
    ),
}


def block_slots(block: int, mask: int, length: int, *, report: bool) -> list[Field]:
    n = mask.bit_length()
    if report and mask.bit_count() < length < n:
        return []  # Neither packed nor positional; don't guess.
    layout = FIXED_BLOCKS.get(block, ())
    if block == 0:
        layout = (
            ("product_number", 3),
            ("j_number", 2),
            ("z_number", 1),
            ("max_tn4_version", 1),
        )
    elif names := control_words(block):
        layout = tuple((name, 2) for name in names)
    slots = [(name, width, byte) for name, width in layout for byte in range(width)]
    result: list[Field] = []
    i = 0
    while i < n:
        if mask & (1 << i):
            if i < len(slots):
                name, width, byte = slots[i]
                bits = (1 << width) - 1
                if byte == 0 and ((mask >> i) & bits) == bits:
                    result.append((name, width))
                    i += width
                    continue
                result.append(f"{name} byte {byte + 1}/{width}")
            else:
                result.append(next(iter(block_fields(block, i, 0))))
        elif report and length >= n:
            result.append(f"Slot {i + 1} unsupported")
        i += 1
    return result


def payload_layout(frame: NativeFrame, fields: Values) -> list[Field]:
    msg, raw = frame.message.name, frame.message.payload
    if msg in ("BlockReport", "BlockUpdate"):
        if msg == "BlockReport" and len(raw) >= 3:
            return [
                ("block_id", 2),
                SUPPORT,
                *block_slots(
                    int.from_bytes(raw[:2], "little"), raw[2], len(raw) - 3, report=True
                ),
            ]
        if msg == "BlockUpdate" and len(raw) >= 4:
            return [
                "target_subnet",
                NODE,
                ("block_id", 2),
                *block_slots(
                    int.from_bytes(raw[2:4], "little"),
                    (1 << min(8, len(raw) - 4)) - 1,
                    len(raw) - 4,
                    report=False,
                ),
            ]
    if msg == "AirGroupMemberStatus":
        return [
            flags(
                "Air group",
                ("Group", 5, "air_group_id"),
                ("Mode", 1, "mode"),
                ("Request master", 1, "request_for_master_report"),
                ("Reserved", 1, "reserved0"),
            ),
            air_status(),
            "target",
            ("temperature", 2),
            flags(
                "Outputs",
                ("Output 1", 2, "output_1_status"),
                ("Output 2", 2, "output_2_status"),
                ("Reserved", 4, "reserved5"),
            ),
            ("remote_temperature", 2),
        ]
    if msg == "AirGroupMasterStatus":
        return [
            flags(
                "Air group",
                ("Group", 5, "air_group_id"),
                ("Inhibit heat", 1),
                ("Inhibit cool", 1),
                ("Unmapped", 1),
            ),
            flags("Cycle", ("Length", 7, "cycle_length"), ("New cycle", 1)),
            air_status("heat"),
            "heat_target",
            ("heat_temperature", 2),
            air_status("cool"),
            "cool_target",
            ("cool_temperature", 2),
            flags(
                "Outputs",
                ("Heat 1", 2, "heat1_status"),
                ("Heat 2", 2, "heat2_status"),
                ("Cool 1", 2, "cool1_status"),
                ("Cool 2", 2, "cool2_status"),
            ),
            ("remote_temperature", 2),
        ]
    if msg in ("SnowmeltZoneStatus", "SnowmeltZoneStatusRequest") and len(raw) >= 2:
        parts: list[Field] = [
            flags("Snowmelt address", ("Role", 3), ("ID", 5)),
            "pairs" if msg == "SnowmeltZoneStatus" else "count",
        ]
        for i in range(min(raw[1], len(raw) - 2)):
            parts.append(
                f"pair{i}_key" if msg == "SnowmeltZoneStatus" else f"Key {i + 1}"
            )
            if msg == "SnowmeltZoneStatus":
                prefix = f"pair{i}_"
                keys = [
                    k.removeprefix(prefix)
                    for k in fields
                    if k.startswith(prefix) and k != prefix + "key"
                ]
                if "operating_state" in keys:
                    parts.extend(
                        [prefix + "operating_substate", prefix + "operating_state"]
                    )
                else:
                    parts.append((prefix + keys[0] if keys else "Unmapped value", 2))
        return parts
    if msg in ("MasterStatus", "PlantLoopStatus"):
        parts = [
            ("supply_temp", 2),
            ("return_temp", 2),
            "Target low",
            flags("Target / mode", ("Target [12:8]", 5), ("Heat/cool", 3)),
            "loop_output_pct",
            "pump_output_pct",
            flags(
                "Operating flags",
                ("Setback", 3),
                ("Weather shutdown", 1),
                ("At max", 1),
                ("At min", 1),
                ("Limited", 1),
                ("Reserved", 1),
            ),
        ]
        if msg == "MasterStatus":
            parts.append(("dhw_supply_temp", 2))
        return parts
    if msg == "PlantOutputStatus" and len(raw) >= 2:
        parts = [
            flags("Plant address", ("Device total", 4), ("Device ID", 4)),
            "device_type",
        ]
        t = raw[1]
        if t in (0, 1):
            parts.append("heat_enabled" if t == 0 else "cool_enabled")
        if t in (2, 3):
            parts.extend(["signal_type", ("output_level", 2)])
        if t in (4, 5, 6):
            parts.append(
                flags("Plant state", ("State", 4), ("Reserved", 2), ("Pump", 2))
            )
            if t == 6:
                parts.extend(
                    [
                        "current_modulation_level",
                        "minimum_modulation_level",
                        "maximum_modulation_level",
                    ]
                )
            else:
                parts.append(flags("Stages", ("Available", 4), ("Operating", 4)))
            parts.extend(["input_temp_raw", "outlet_temp_raw"])
            if t != 5:
                parts.append(flags("Output", ("Level", 5), ("Return protection", 3)))
        return parts
    if msg == "MasterPresent":
        return ["boiler_subnet", *(f"mix{i}_subnet" for i in range(1, 8))]
    if msg == "StageHeatDemand":
        return [
            *SIMPLE[msg],
            *((("target_temp", 2),) if len(raw) > 2 and raw[2] & 128 else ()),
        ]
    return list(SIMPLE.get(msg or "", ()))


def native_spans(frame: NativeFrame, fields: Values) -> tuple[Span, Span]:
    raw = frame.body
    used: set[str] = set()
    header = [
        flags("Priority / message", ("Message ID [9:4]", 6), ("Priority", 2)),
        flags(
            f"ID 0x{frame.message_id:03X}", ("Subnet [7:4]", 4), ("Message ID [3:0]", 4)
        ),
        flags(f"Subnet 0x{frame.subnet:02X}", ("Node [5:2]", 4), ("Subnet [3:0]", 4)),
        flags(f"Node {frame.node} / length", ("Length", 6), ("Node [1:0]", 2)),
    ]
    result: list[Span] = []
    index = 0
    for field in [*header, *payload_layout(frame, fields)]:
        if index >= len(raw):
            break
        if isinstance(field, Packed):
            part = span(
                raw[index : index + 1], field.label, "header" if index < 4 else "field"
            )
            pos = 0
            bits: list[Bit] = []
            for spec in field.groups:
                name, width = spec[:2]
                key = spec[2] if len(spec) == 3 else name.lower().replace(" ", "_")
                value = (raw[index] >> pos) & ((1 << width) - 1)
                bit: Bit = {"hi": pos + width - 1, "lo": pos, "label": name}
                if (
                    name == "Unmapped"
                    or (name.startswith("Message ID") and frame.message.name is None)
                    or (name == "Reserved" and value)
                ):
                    bit["coverage"] = "unknown"
                elif name == "Reserved":
                    bit["coverage"] = "unused"
                if index >= 4 and key in fields:
                    val = fields[key]
                    if isinstance(val, str):
                        name += ": " + val.replace("_", " ")
                        if val in {"unknown", "ambiguous", "unsupported"}:
                            bit["coverage"] = "unknown"
                    if len(spec) == 3 or val == value:
                        used.add(key)
                    named = (
                        "schedule_name" if key == "schedule_status" else key + "_name"
                    )
                    if named in fields:
                        used.add(named)
                        name += ": " + str(fields[named]).replace("_", " ")
                bit["label"] = name
                bits.append(bit)
                pos += width
            part["bits"] = bits[::-1]
            result.append(part)
            index += 1
        else:
            name, width = field if isinstance(field, tuple) else (field, 1)
            chunk = raw[index : index + width]
            val = fields.get(
                name + "_f", fields.get(name, int.from_bytes(chunk, "little"))
            )
            used.update((name, name + "_f", name + "_raw"))
            if name.endswith("_raw") and name[:-4] + "_f" in fields:
                name = name[:-4]
                val = fields[name + "_f"]
                used.add(name + "_f")
            unit = " °F" if name + "_f" in fields else ""
            label = (
                name.replace("_", " ")
                + f": {val}{unit}"
                + (" (LE)" if width > 1 and not name.endswith("_hex") else "")
            )
            out = span(chunk, label, "field")
            if name == "code" and fields.get("description") == "Unknown Error":
                out["coverage"] = "unknown"
            if len(chunk) != width:
                out["coverage"] = "invalid"
                out["label"] += f" (only {len(chunk)}/{width} bytes)"
            elif (
                name.startswith("Unmapped")
                or (name.startswith("byte") and name.endswith("_raw"))
                or val in {"unknown", "ambiguous", "unsupported"}
            ):
                out["coverage"] = "unknown"
            elif name.endswith("unsupported"):
                out["coverage"] = "unused"
            result.append(out)
            index += len(chunk)
    if raw[index:]:
        result.append(span(raw[index:], "Unmapped payload bytes", "unknown"))
    # Keep derived interpretations without repeating the scalar fields or captured bytes.
    extra = {
        k: v for k, v in fields.items() if k not in used and not k.endswith("_hex")
    }
    payload = group("tN4 payload", result[4:], extra, column="payload")
    uncertain = [
        k
        for k, v in extra.items()
        if any(tag in k for tag in ("unknown", "opaque", "unresolved"))
        or ("reserved" in k and v not in (0, "0", "0x00"))
        or v in {"unknown", "ambiguous", "unsupported"}
    ]
    if uncertain:
        payload["unknown_notes"] = uncertain
    return group("tN4 header", result[:4], column="header"), payload
