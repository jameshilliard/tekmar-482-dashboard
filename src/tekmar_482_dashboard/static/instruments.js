const make = (tag, cls, text) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
};
const readable = (s) => String(s ?? "").replaceAll("_", " ");
export function instrument(d) {
  const type =
    d.family === "gateway"
      ? "gateway"
      : d.family === "snowmelt"
        ? "snow"
        : d.family === "thermostat" || d.temperature_f != null
          ? "thermo"
          : "module";
  const art = make(
    "span",
    "instrument " + type + (d.model === "553" ? " touch" : ""),
  );
  art.setAttribute("aria-hidden", "true");
  art.append(
    make("span", "case-label", d.model || "tN4"),
    make(
      "span",
      "lcd",
      d.temperature_f == null
        ? type === "gateway"
          ? "482"
          : type === "snow"
            ? "MELT"
            : "—"
        : d.temperature_f.toFixed(1) + "°",
    ),
    make("span", "keys"),
    make("span", "pins"),
  );
  return art;
}
export function deviceCard(d, onClick) {
  const b = make("button", "device-card");
  b.type = "button";
  const text = make("span", "device-text");
  text.append(
    make(
      "strong",
      "",
      d.model ? "tekmar " + d.model : readable(d.family || "Bus source"),
    ),
    make(
      "small",
      "",
      d.family === "gateway"
        ? "Interface gateway"
        : (d.address != null ? "tHA " + d.address + " · " : "") +
            (d.port == null ? "Inventory" : "Bus " + d.port),
    ),
    make(
      "span",
      "reading",
      d.temperature_f != null
        ? d.temperature_f.toFixed(1) + " °F"
        : readable(d.family || "Unidentified"),
    ),
  );
  b.append(instrument(d), text);
  b.onclick = onClick;
  return b;
}
const route = {
  rx: "BUS → GW",
  tx: "GW → BUS",
  rx_invalid: "BUS → GW",
  host_tx: "HOST → GW",
  host_rx: "GW → HOST",
  port_error: "GW → HOST",
  report: "GW → HOST",
};
export function tracePath(e) {
  return route[e.direction] || readable(e.direction).toUpperCase();
}
function verdict(e) {
  return (
    e.quality || (["system", "operator"].includes(e.layer) ? "info" : "partial")
  );
}
export function byteStrip(e) {
  const row = make("div", "byte-strip");
  const parts =
    e.spans ||
    ((e.hex || "").length
      ? [
          {
            hex: e.hex
              .match(/.{1,2}/g)
              .join(" ")
              .toUpperCase(),
            label: e.reason || "Payload",
            kind: "payload",
          },
        ]
      : []);
  for (const p of parts) {
    const field = make("span", "byte-field " + p.kind);
    field.append(make("code", "", p.hex), make("small", "", p.label));
    row.append(field);
  }
  return row;
}
export function packet(e) {
  const grade = verdict(e),
    out = make("div", "packet " + grade),
    heading = make("div", "packet-title");
  const symbol =
    { good: "✓ Decoded", error: "× Error", partial: "? Partial", info: "Info" }[
      grade
    ] || "? Partial";
  const flag = make("span", "verdict " + grade, symbol);
  flag.title = grade + " · " + (e.reason || "");
  heading.append(
    flag,
    make("b", "", readable(e.name)),
    make(
      "small",
      "",
      e.layer.toUpperCase() +
        (e.service ? " / " + e.service : "") +
        (e.ack === true ? " · ACK" : e.ack === false ? " · NO ACK" : ""),
    ),
  );
  out.append(heading, byteStrip(e));
  if (e.fields && Object.keys(e.fields).length) {
    const grid = make("div", "decoded-grid");
    for (const [k, v] of Object.entries(e.fields)) {
      const pair = make("span", "decoded-pair");
      pair.append(
        make("span", "", readable(k) + "="),
        make("b", "", physical(v)),
      );
      grid.append(pair);
    }
    out.append(grid);
  }
  if (grade === "error" || grade === "partial")
    out.append(
      make("small", "explanation", e.reason || "Unrecognized payload"),
    );
  return out;
}
export function live(e, flow, details) {
  if (!e) {
    flow.textContent = "Waiting for traffic";
    details.replaceChildren();
    return;
  }
  const grade = verdict(e);
  flow.className = "flow " + grade;
  const bus = ["rx", "tx", "rx_invalid"].includes(e.direction),
    back = ["host_rx", "rx", "rx_invalid", "report", "port_error"].includes(
      e.direction,
    );
  const line = (active) =>
    make(
      "span",
      "flow-link" + (active ? " lit" : ""),
      active ? (back ? "◀" : "▶") : "",
    );
  flow.replaceChildren(
    make("span", "station", "Host"),
    line(!bus),
    make("span", "station central", "482 gateway"),
    line(bus),
    make("span", "station", e.port == null ? "tN4" : "Bus " + e.port),
    make("b", "flow-tag", "LIVE · #" + e.seq),
  );
  details.replaceChildren(packet(e));
}
export function physical(value) {
  if (value == null) return "Not reported";
  if (typeof value !== "object") return readable(value);
  if (value.fahrenheit != null && value.celsius != null)
    return (
      Number(value.fahrenheit).toFixed(1) +
      " °F / " +
      Number(value.celsius).toFixed(1) +
      " °C"
    );
  if (value.name != null) return readable(value.name);
  return (
    Object.entries(value)
      .filter(([, v]) => v != null)
      .map(([k, v]) => readable(k) + ": " + physical(v))
      .join(" · ") || "Not reported"
  );
}
