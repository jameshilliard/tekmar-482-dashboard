import { TraceHistory } from "./history.js";
import { Endpoints } from "./endpoints.js";
import { badge, live, tracePath, verdict } from "./instruments.js";
import { parameters } from "./parameters.js";
import { wireDiagram } from "./wire.js";

const layouts = {
  tn4: [
    ["stamp", "Time / #"],
    ["io", "482 I/O"],
    ["message", "Message"],
    ["header", "tN4 header"],
    ["payload", "tN4 payload"],
    ["record", "Trace record"],
    ["framing", "TPCK framing"],
  ],
  trpc: [
    ["stamp", "Time / #"],
    ["io", "482 I/O"],
    ["service", "Service"],
    ["method", "Method"],
    ["payload", "Parameters"],
    ["framing", "TPCK framing"],
  ],
  gateway: [
    ["stamp", "Time / #"],
    ["io", "482 I/O"],
    ["message", "Event"],
    ["payload", "Event details"],
    ["framing", "TPCK framing"],
  ],
};
const protocol = (e) =>
  e.protocol || (e.layer === "tn4" || e.layer === "trpc" ? e.layer : "gateway");
const clock = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
function label(tag, value, cls) {
  const el = document.createElement(tag);
  el.textContent = value;
  if (cls) el.className = cls;
  return el;
}
function rowFor(e, columns, open, directory) {
  const row = document.createElement("tr"),
    cells = {};
  row.dataset.seq = e.seq;
  row.dataset.protocol = protocol(e);
  row.className = "trace-row " + verdict(e);
  for (const [key] of columns) {
    const cell = row.insertCell();
    cell.dataset.col = key;
    cells[key] = document.createElement("div");
    cells[key].className = "cell-fields";
    cell.append(cells[key]);
  }
  cells.stamp.append(
    label("time", clock.format(e.at * 1000)),
    label("small", "#" + e.seq),
  );
  const direction = tracePath(e),
    io = label("b", direction, "io-badge " + direction.toLowerCase());
  io.title =
    direction === "RX"
      ? "Received by the gateway"
      : direction === "TX"
        ? "Transmitted by the gateway"
        : "Gateway event";
  cells.io.append(io);
  if (e.port != null) cells.io.append(label("small", "Bus " + e.port));
  if (cells.message) cells.message.append(label("strong", e.name));
  directory.fill(cells.endpoint, e);
  row.dataset.directory = directory.version;
  if (e.layout) wireDiagram(e, false, cells);
  else if (e.fields && Object.keys(e.fields).length)
    cells.payload.append(parameters(e.fields));
  const summary = cells.message || cells.method;
  if (!summary.hasChildNodes()) summary.append(label("strong", e.name));
  summary.append(badge(e));
  if (e.notice) summary.append(label("small", e.notice, "reported-notice"));
  if (["error", "partial"].includes(verdict(e)) && e.reason)
    summary.append(label("small", e.reason, "explanation"));
  row.tabIndex = 0;
  row.onclick = () => open(e);
  row.onkeydown = (key) => {
    if (key.key === "Enter") {
      key.preventDefault();
      open(e);
    }
  };
  return row;
}

export class TraceTabs {
  constructor(root, open, changed) {
    this.active = "tn4";
    this.chosen = false;
    this.devices = new Endpoints(
      root.querySelector("#endpoint-filter"),
      changed,
    );
    this.views = new Map();
    const buttons = [...root.querySelectorAll("[role=tab]")];
    for (const [i, button] of buttons.entries()) {
      const key = button.dataset.protocol,
        columns = [...layouts[key]];
      columns.splice(2, 0, ["endpoint", "End device"]);
      const node = root
        .querySelector("template")
        .content.firstElementChild.cloneNode(true);
      node.id = button.getAttribute("aria-controls");
      node.setAttribute("aria-labelledby", button.id);
      node.hidden = key !== this.active;
      node
        .querySelector(".trace-scroll")
        .setAttribute("aria-label", button.textContent + " packet history");
      const head = node.querySelector("thead tr"),
        cols = node.querySelector("colgroup");
      for (const [id, name] of columns) {
        const col = document.createElement("col"),
          th = label("th", name);
        col.dataset.col = id;
        th.dataset.col = id;
        th.scope = "col";
        cols.append(col);
        head.append(th);
      }
      const flow = node.querySelector(".flow");
      const history = new TraceHistory(
        node.querySelector(".trace-scroll"),
        node.querySelector(".follow"),
        (e) => rowFor(e, columns, open, this.devices),
        (e) => live(e, flow),
        (row, e) => {
          if (Number(row.dataset.directory) !== this.devices.version) {
            this.devices.fill(
              row.querySelector("[data-col=endpoint] .cell-fields"),
              e,
            );
            row.dataset.directory = this.devices.version;
          }
        },
      );
      this.views.set(key, { button, node, history });
      root.querySelector("#trace-panels").append(node);
      button.addEventListener("click", () => {
        this.select(key);
        changed();
      });
      button.addEventListener("keydown", (event) => {
        const n = buttons.length;
        const j = {
          ArrowLeft: (i + n - 1) % n,
          ArrowRight: (i + 1) % n,
          Home: 0,
          End: n - 1,
        }[event.key];
        if (j !== undefined) {
          event.preventDefault();
          buttons[j].focus();
          buttons[j].click();
        }
      });
    }
  }
  select(key, manual = true) {
    if (manual) this.chosen = true;
    this.active = key;
    for (const [k, { button, node }] of this.views) {
      button.tabIndex = k === key ? 0 : -1;
      button.setAttribute("aria-selected", String(k === key));
      node.hidden = k !== key;
    }
  }
  inventory(devices, peers) {
    this.devices.update(devices, peers);
  }
  matches(e) {
    return protocol(e) === this.active && this.devices.allows(e);
  }
  update(events, filter) {
    this.views.get(this.active).history.update(events, filter);
  }
}
