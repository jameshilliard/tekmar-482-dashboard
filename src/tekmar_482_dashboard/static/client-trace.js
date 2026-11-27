import { TraceHistory } from "./history.js";
import { wireDiagram } from "./wire.js";

const flow = { to_gateway: "TX", to_client: "RX", filtered: "Filtered" };
const clock = new Intl.DateTimeFormat(undefined, {
  timeStyle: "medium",
  hour12: false,
});
function context(e) {
  if (e.direction === "filtered") return "Not forwarded";
  if (!e.requests.length)
    return e.direction !== "to_client"
      ? ""
      : e.service === "Report"
        ? "Unsolicited report"
        : "Shared reply";
  const ids = e.requests.map((n) => "#" + n).join(", ");
  return (
    (e.ambiguous ? "Ambiguous match " : "Possible reply to ") +
    ids +
    (e.elapsed_ms != null ? " · " + e.elapsed_ms + " ms" : "")
  );
}

export class ClientInspector {
  constructor(open) {
    this.open = open;
    this.select = document.querySelector("#client-select");
    this.status = document.querySelector("#client-trace-status");
    this.history = new TraceHistory(
      document.querySelector("#client-trace"),
      document.querySelector("#client-follow"),
      (e) => this.row(e),
      () => {},
      () => {},
    );
    this.select.onchange = () => this.choose(Number(this.select.value));
    this.sessions = [];
    this.choose(null);
  }
  row(e) {
    const row = document.createElement("tr"),
      cells = {};
    row.className = "trace-row " + e.quality;
    for (const key of [
      "time",
      "io",
      "framing",
      "service",
      "method",
      "payload",
      "relation",
    ]) {
      const el = document.createElement("div");
      el.className = "cell-fields";
      row.insertCell().append(el);
      cells[key] = el;
    }
    cells.time.textContent = clock.format(e.at * 1000) + " · #" + e.seq;
    cells.io.textContent = flow[e.direction];
    if (e.layout) wireDiagram(e, false, cells);
    if (!cells.method.childElementCount) cells.method.textContent = e.name;
    cells.relation.textContent = context(e);
    if (e.quality !== "good")
      cells.relation.append(document.createElement("br"), e.reason);
    row.tabIndex = 0;
    row.onclick = () => this.open(e, this.id, context(e));
    row.onkeydown = (ev) => {
      if (ev.key === "Enter") row.click();
    };
    return row;
  }
  choose(id) {
    this.abort?.abort();
    this.abort = null;
    this.id = id;
    this.select.value = id == null ? "" : String(id);
    this.start = this.sessions.find((p) => p.id === id)?.connected;
    this.events = [];
    this.seq = this.skipped = 0;
    this.status.textContent = "";
    this.history.update([], id);
    this.load();
  }
  update(clients) {
    this.sessions = [...(clients?.active || []), ...(clients?.recent || [])];
    const key = this.sessions
      .map((p) => [p.id, p.connected, !!p.closed].join(":"))
      .join(",");
    if (key !== this.options) {
      this.options = key;
      this.select.replaceChildren(
        ...this.sessions.map(
          (p) =>
            new Option(
              "#" + p.id + " · " + p.peer + (p.closed ? " · closed" : ""),
              p.id,
            ),
        ),
      );
      this.select.disabled = !this.sessions.length;
      const current = this.sessions.find(
        (p) => p.id === this.id && p.connected === this.start,
      );
      if (current) {
        this.select.value = String(current.id);
        this.load();
      } else this.choose(this.sessions[0]?.id ?? null);
    } else this.load();
  }
  async load() {
    const peer = this.sessions.find((p) => p.id === this.id);
    if (!peer || this.abort || this.seq >= peer.trace_seq) return;
    const ctrl = (this.abort = new AbortController());
    try {
      const response = await fetch(
        "/api/clients/" + peer.id + "/trace?after=" + this.seq,
        { signal: ctrl.signal },
      );
      if (!response.ok) throw Error("Trace unavailable");
      const batch = await response.json();
      if (ctrl !== this.abort || batch.client_id !== this.id) return;
      this.events = this.events.concat(batch.events).slice(-256);
      this.seq = batch.seq;
      this.skipped += batch.skipped;
      this.history.update(this.events, this.id);
      this.status.textContent =
        this.events.length +
        " records" +
        (this.skipped ? " · earlier records expired" : "");
    } catch (e) {
      if (!ctrl.signal.aborted) this.status.textContent = e.message;
    } finally {
      if (this.abort === ctrl) this.abort = null;
    }
  }
}
