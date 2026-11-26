# tekmar 482 dashboard

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

## Monitor

Normal mode displays inventory, reported settings, native traffic in both
directions and tRPC traffic. All available diagnostic sources and Normal
trace ports are selected by default. Raw mode defaults to passive capture
without link acknowledgments. A native source address is not, on its own,
a confirmed physical device or model.

Mixed and Trace show a full-width packet history. Green marks decoded
frames, yellow marks partial interpretations and red shows faults with
their reasons. Scroll into history to hold your place while capture
continues. Return to the top or select **Jump to live** to follow again.
Host wire bytes are reconstructed from accepted TPCK frames; they are
not a byte-for-byte recording of malformed UART traffic.

## Controls and privacy

Monitoring reads inventory and configuration; it does not set connected
device parameters. Review and confirm a control operation before it is
sent. Some exclusive client tools briefly suspend capture. Firmware
updates require a client installed from a firmware-bearing source
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
