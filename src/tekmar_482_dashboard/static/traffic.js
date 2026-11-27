const labels = {
  rx_valid: "RX valid",
  rx_invalid: "RX invalid",
  tx_attempts: "TX attempts",
  tx_ack: "TX ACK",
  tx_no_ack: "TX no ACK",
  tx_failed: "TX failed",
  rx_untracked: "RX untracked",
  tx_unattributed: "TX unattributed",
};
const node = (tag, text) => {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  return el;
};
function table(rows, title) {
  const wrap = node("div"),
    t = node("table"),
    header = t.createTHead().insertRow(),
    body = t.createTBody();
  wrap.className = "traffic-table";
  const keys = Object.keys(labels).filter((k) =>
    rows.some((r) => r[k] != null),
  );
  if (title) header.append(node("th", title));
  for (const key of keys) header.append(node("th", labels[key]));
  for (const r of rows) {
    const tr = body.insertRow();
    if (title) tr.append(node("th", r.label));
    for (const key of keys)
      tr.insertCell().textContent = BigInt(r[key]).toLocaleString();
  }
  wrap.append(t);
  return wrap;
}
export function trafficPanel(path = null, nameFor = () => null) {
  const panel = node("section"),
    heading = node("h3", "Traffic since gateway boot"),
    result = node("div", "Reading counters…");
  panel.className = "traffic-panel";
  panel.append(heading, result);
  const url = path
    ? "/api/traffic/" + [path.port, path.subnet, path.node].join("/")
    : "/api/traffic/ports";
  async function fetchData(url) {
    const response = await fetch(url);
    if (!response.ok)
      throw Error(
        response.status === 409
          ? "Counters unavailable on this gateway"
          : "Could not read gateway counters",
      );
    return response.json();
  }
  async function tick() {
    if (!panel.isConnected || panel.closest("[hidden], dialog:not([open])"))
      return;
    if (!document.hidden) {
      try {
        const data = await fetchData(url);
        result.replaceChildren(
          data == null
            ? node("small", "Path not tracked")
            : table(
                path
                  ? [data]
                  : data.map((r) => ({ ...r, label: "Bus " + r.port })),
                path ? null : "Port",
              ),
        );
      } catch (err) {
        result.textContent = err.message;
      }
    }
    setTimeout(tick, 10000);
  }
  if (!path) {
    const button = node("button", "Read per-device totals"),
      devices = node("div");
    button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      try {
        const rows = await fetchData("/api/traffic/devices");
        devices.replaceChildren(
          table(
            rows.map((r) => {
              const key =
                r.port +
                ":" +
                [r.subnet, r.node]
                  .map((n) => n.toString(16).padStart(2, "0").toUpperCase())
                  .join(":");
              return { ...r, label: nameFor(key) || key };
            }),
            "Native device",
          ),
        );
      } catch (err) {
        devices.textContent = err.message;
      } finally {
        button.disabled = false;
      }
    };
    panel.append(button, devices);
  }
  queueMicrotask(tick);
  return panel;
}
