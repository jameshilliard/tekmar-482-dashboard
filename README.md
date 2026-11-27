# tekmarshark

A local FastAPI/WebSocket instrument console for the tekmar 482. Run it on the
machine connected to the gateway. Browsers share one gateway connection.

## Run

Python 3.12 or newer and uv are required. From this checkout:

```sh
uv run --frozen tekmar-482-dashboard --tcp 192.0.2.10:3001
uv run --frozen --extra serial tekmar-482-dashboard --serial /dev/ttyUSB0
```

The default browser address is `http://127.0.0.1:8482`. Use
`--host 0.0.0.0` to listen on the LAN, and open the server's LAN address.
TCP requires a transparent serial bridge set to the same baud rate as
`--baudrate`; the 482 itself has an RS-232 port. No external packet service
is needed. The custom firmware is the default. Use `--vendor` to monitor
stock tRPC without native tracing or gateway error counters.

On every connection the dashboard checks the build bundled with the Python
client. It leaves a matching gateway alone, updates an older custom build,
and provisions a positively identified LPC2146 bootloader. Stock firmware
cannot request that handoff: the dashboard displays the
[official Test-button procedure](https://www.watts.com/our-story/brands/tekmar/482firmware)
and retries automatically. With `--vendor` it can monitor stock tRPC until
the operator puts the gateway into update mode. A source checkout without a
bundled image can monitor but cannot flash.

For a dashboard checkout using the Git dependency, install a firmware-bearing
client sdist into the dashboard environment, then run the installed command
directly so a project sync does not replace it with an image-less Git wheel:

```sh
uv sync --frozen --extra serial
uv pip install --no-deps --reinstall /path/to/tekmar_482-0.1.0.tar.gz
.venv/bin/tekmar-482-dashboard --serial /dev/ttyUSB0
```

## Compatibility clients

The dashboard can also replace the packet server for the
[WillCodeForCats integration](https://github.com/WillCodeForCats/tekmar-482).
Enable the separate TCP listener explicitly:

```sh
uv run --extra serial tekmar-482-dashboard --serial /dev/ttyUSB0 --host 0.0.0.0 --compat-port 3000
```

Point the integration's packet-server host and port at this machine and port
3000. The HTTP port is not the packet-server port. The listener inherits
`--host`; use `--compat-host` to bind it to a different interface.
It has no authentication, including when a web operator key is configured.
Enable it only for trusted clients: their stock updates can change real
device settings. The upstream 0.9.34 integration also sends a periodic
DateTime update; a passive test installation must suppress its timekeeper.

In Normal mode, every peer sees the gateway's original supported tRPC replies
and reports, including replies caused by other clients. Once a device type is
known the listener passes only the methods the upstream device class handles.
For example, thermostat temperatures and setback events pass unchanged, but
snow control setback events stay in the gateway trace; forwarding even an
unavailable value to that class can terminate the upstream receive loop.
Supported requests and updates are forwarded, not answered from a cache. ReportingState
and SetbackEnable are real, shared gateway settings. Native frames, custom
methods and diagnostic packets remain available to the dashboard only.
Entering Raw mode, losing the gateway or running an exclusive operation
disconnects these clients; no buffered command is replayed on reconnect.
The **Clients** tab shows the listener, active peers, directional counts,
filtered input, recent activity and the last 16 disconnects.
Select a connection for its own bounded, directional tHA history. The
decoder is the same one used for the gateway's tRPC trace; TCP hex records
do not have the UART framing or checksum. A compatible reply is a candidate,
not an acknowledgement of a particular client action. Unsolicited reports
and replies shared by other clients are shown as such.

## Monitor

Normal mode displays inventory, reported settings, native traffic in both
directions and tRPC traffic. All available diagnostic sources and Normal
trace ports are selected by default. Raw mode defaults to passive capture
without link acknowledgments. A native source address is not, on its own,
a confirmed physical device or model. In Normal mode, capable firmware
can request a source's product identity without changing its settings.

Mixed and Trace use separate tN4, tRPC and Gateway tabs with independent
scroll positions. RX and TX are relative to the 482 on the selected link.
Click an endpoint or use **End device** to filter by a source or explicit
destination. A bus-wide broadcast is not attributed to individual devices.
Open a device icon for its complete configuration and available controls.

Diagnostics and device dialogs can read the gateway's own 64-bit traffic
counters. These keep counting with no connected browser or host. Port totals
include invalid RX and non-addressed TX; device totals include valid RX and
explicitly unicast block TX, split by link outcome. Counts restart at boot.

Protocol fields occupy fixed columns. Open a frame for one nested, wire-order
diagram with escaped bytes, offsets and bit ranges. Green marks decoded data;
amber hatching marks the specific unresolved bits or bytes. Red means invalid
data, not a correctly decoded device fault, which gets a separate notice.
Known payload flags show their meaning alongside the bit position and binary
value, including thermostat demand, supported slots and active schedule.
Scroll into history to hold your place while capture continues, or select
**Jump to live** to follow again.
Host wire bytes are reconstructed from accepted TPCK frames; they are
not a byte-for-byte recording of malformed UART traffic.

## Controls and privacy

Monitoring reads inventory and configuration; it does not set connected
device parameters. Review and confirm a control operation before it is
sent. Some exclusive client tools briefly suspend capture. Raw frames,
register edits and schedules use value pickers instead of text commands.
Raw payload length is calculated from the selected bytes. Firmware updates
require a client installed from a firmware-bearing source
distribution; a client wheel built directly from Git has no image.

Controls are unlocked by default. Use only on a trusted LAN; do not
forward this service to the internet. `--key-file PATH` optionally gates
commands with a private operator key; it does **not** hide telemetry.
Data lives in bounded memory unless a browser exports it. Keep keys,
exports and deployment configuration outside Git.

## Development

```sh
uv sync --frozen
uv run python -m unittest
uv run pre-commit run --all-files
```

Tests use synthetic packets and a documentation-only endpoint. Icons are
vendored from Lucide; see the included icon license.
