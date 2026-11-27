import { wireDiagram } from "./wire.js";
import { parameters } from "./parameters.js";

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
export function deviceCard(d, onClick, summary = []) {
  const b = make("button", "device-card");
  b.type = "button";
  b.setAttribute(
    "aria-label",
    "Open details for " +
      (d.model ? "tekmar " + d.model : readable(d.family)) +
      (d.address == null ? "" : " · " + d.address),
  );
  b.title = b.getAttribute("aria-label");
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
            (d.port == null ? "Inventory" : "Bus " + d.port) +
            (d.node == null
              ? ""
              : " · " +
                [d.subnet, d.node]
                  .map((v) => v.toString(16).toUpperCase().padStart(2, "0"))
                  .join(":")),
    ),
    make("span", "reading", readable(d.family || "Unidentified")),
  );
  if (summary.length)
    text.append(make("small", "device-summary", summary.join(" · ")));
  b.append(instrument(d), text);
  b.onclick = onClick;
  return b;
}
// Direction is relative to the 482 on the link shown in the active tab.
const route = {
  rx: "RX",
  tx: "TX",
  rx_invalid: "RX",
  host_tx: "RX",
  host_rx: "TX",
  port_error: "TX",
  report: "TX",
};
export function tracePath(e) {
  return route[e.direction] || "—";
}
export function verdict(e) {
  return (
    e.quality || (["system", "operator"].includes(e.layer) ? "info" : "partial")
  );
}
export function badge(e) {
  const grade = verdict(e);
  const value =
    {
      good: "✓ Decoded",
      partial: "? Partial",
      error: "× Invalid",
      info: "Info",
    }[grade] || "? Partial";
  const flag = make("span", "verdict " + grade, value);
  flag.title = e.reason || grade;
  return flag;
}
export function packet(e, inspect = false) {
  const grade = verdict(e),
    out = make("div", "packet " + grade),
    heading = make("div", "packet-title");
  heading.append(
    badge(e),
    ...(inspect ? [] : [make("b", "", readable(e.name))]),
    make(
      "small",
      "",
      e.layer.toUpperCase() +
        (e.service ? " / " + e.service : "") +
        (e.ack === true ? " · ACK" : e.ack === false ? " · NO ACK" : ""),
    ),
  );
  out.append(heading);
  if (e.notice) out.append(make("small", "reported-notice", e.notice));
  if (e.layout) out.append(wireDiagram(e, inspect));
  else if (e.fields && Object.keys(e.fields).length)
    out.append(parameters(e.fields));
  if (grade === "error" || grade === "partial")
    out.append(
      make("small", "explanation", e.reason || "Unrecognized payload"),
    );
  return out;
}
export function live(e, flow) {
  if (!e) {
    flow.textContent = "Waiting for traffic";
    return;
  }
  const grade = verdict(e);
  flow.className = "flow " + grade;
  if (!route[e.direction]) {
    flow.replaceChildren(make("b", "flow-tag", e.name + " · #" + e.seq));
    return;
  }
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
    make("b", "flow-tag", tracePath(e) + " · #" + e.seq),
  );
}
