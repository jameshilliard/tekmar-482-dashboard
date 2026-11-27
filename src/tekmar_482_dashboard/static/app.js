import { rawEditor, toolEditors, transactionEditors } from "./controls.js";
import { trafficPanel } from "./traffic.js";
import { parameters as facts, thermostatSummary } from "./parameters.js";
import { deviceCard, instrument, tracePath, packet } from "./instruments.js";
import { TraceTabs } from "./traces.js";
import { ClientInspector } from "./client-trace.js";
import { wireDiagram } from "./wire.js";
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const icon = (name) => {
  const img = el("img", null, "icon-image");
  img.src = "/static/icons/" + name + ".svg";
  img.alt = "";
  return img;
};
$$("[data-icon]").forEach((n) => n.replaceWith(icon(n.dataset.icon)));
const number = (n) => new Intl.NumberFormat().format(n);
const stamp = (n) =>
  new Date(n * 1000).toLocaleTimeString(undefined, { hour12: false });
const hex = (n) => "0x" + Number(n).toString(16).toUpperCase().padStart(2, "0");

const choose = (select, items) =>
  select.replaceChildren(
    ...items.map(([value, label]) => new Option(label, value)),
  );
const btn = (title, fn) => {
  const n = el("button", title);
  n.type = "button";
  n.onclick = fn;
  return n;
};
const tidy = (s) => String(s ?? "").replaceAll("_", " ");
const state = {
  device_details: {},
  events: [],
  ports: [],
  peers: [],
  devices: [],
  counters: [],
  status: {},
  seq: 0,
};
let catalog = { commands: [], methods: [], reasons: [], vendor: false },
  portFilter = null,
  view = "mixed",
  page = "monitor",
  filterPeer = null;
let paused = false,
  shown = state,
  key = "",
  pending = null,
  connected = false,
  retry = 500,
  busy = false;
const rows = (body, data, empty, width, cells, click) => {
  const out = document.createDocumentFragment();
  if (!data.length) {
    const tr = el("tr"),
      td = el("td", empty, "empty");
    td.colSpan = width;
    tr.append(td);
    out.append(tr);
  }
  for (const item of data) {
    const tr = el("tr");
    for (const c of cells(item)) {
      const td = el("td");
      if (c instanceof Node) td.append(c);
      else td.textContent = c ?? "–";
      tr.append(td);
    }
    if (click) {
      tr.tabIndex = 0;
      tr.onclick = () => click(item);
      tr.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          click(item);
        }
      };
    }
    out.append(tr);
  }
  body.replaceChildren(out);
};
function setPage(which) {
  page = which;
  $("#traffic-totals").replaceChildren(
    ...(which === "diagnostics"
      ? [trafficPanel(null, (key) => traceTabs.devices.name(key))]
      : []),
  );
  for (const id of ["monitor", "operations", "diagnostics", "clients"])
    $("#" + id).hidden = id !== which;
  $$("[data-page]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.page === which),
  );
  $("#title").textContent = $("#crumb").textContent = {
    monitor: "Network monitor",
    diagnostics: "Diagnostics",
    clients: "Clients",
    operations: "Operations",
  }[which];
  render();
}
function setView(v) {
  view = v;
  $("#panes").dataset.view = v;
  document.body.dataset.layout = v;
  $$("[data-view]")
    .filter((b) => b.tagName === "BUTTON")
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.view === v)),
    );
  render();
}
function selectOperation(op) {
  $$("[data-operation]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.operation === op)),
  );
  ["climate", "trpc", "raw", "catalog"].forEach(
    (id) => ($("#" + id).hidden = id !== op),
  );
}
$$("[data-page]").forEach((b) => (b.onclick = () => setPage(b.dataset.page)));
$$("button[data-view]").forEach(
  (b) => (b.onclick = () => setView(b.dataset.view)),
);
$$("[data-operation]").forEach(
  (b) => (b.onclick = () => selectOperation(b.dataset.operation)),
);
const match = (item, f, scoped = true) => {
  return (
    (!scoped || portFilter === null || item.port === portFilter) &&
    (!scoped ||
      !filterPeer ||
      item.key === filterPeer ||
      item.peer === filterPeer) &&
    (!f || JSON.stringify(item).toLowerCase().includes(f))
  );
};
function displayedEvents() {
  const search = $("#search").value.trim().toLowerCase();
  return shown.events.filter(
    (e) => traceTabs.matches(e) && match(e, search, traceTabs.active === "tn4"),
  );
}
const traceTabs = new TraceTabs($(".trace-pane"), eventDetails, render);
const clientInspector = new ClientInspector((e, id, context) =>
  openDetail(e.name, [
    el(
      "p",
      "Client #" +
        id +
        " · Record #" +
        e.seq +
        (context ? " · " + context : ""),
      "capture-line",
    ),
    e.layout ? wireDiagram(e, true) : el("p", e.reason),
  ]),
);
function spark(canvas, activity) {
  const w = 180,
    h = 34,
    d = 2;
  canvas.width = w * d;
  canvas.height = h * d;
  const c = canvas.getContext("2d");
  c.scale(d, d);
  c.strokeStyle = "#dfe7e0";
  c.lineWidth = 1;
  for (let x = 0; x < w; x += 30) {
    c.beginPath();
    c.moveTo(x + 0.5, 4);
    c.lineTo(x + 0.5, h);
    c.stroke();
  }
  c.beginPath();
  c.moveTo(0, h - 2);
  c.lineTo(w, h - 2);
  c.stroke();
  const max = Math.max(3, ...activity),
    step = w / Math.max(1, activity.length - 1);
  c.beginPath();
  activity.forEach((v, i) => {
    const y = h - 2 - (v / max) * (h - 5);
    if (!i) c.moveTo(0, y);
    else c.lineTo(i * step, y);
  });
  c.strokeStyle = activity.some(Boolean) ? "#16845e" : "#adbbb3";
  c.lineWidth = 1.6;
  c.stroke();
}
function render() {
  if (!paused) shown = state;
  const st = state.status;
  $("#connection").textContent = connected
    ? st.connection === "online"
      ? "Gateway online"
      : tidy(st.connection || "connecting")
    : "Disconnected";
  $("#connection").classList.toggle(
    "online",
    connected && st.connection === "online",
  );
  $("#ws-dot").classList.toggle("off", !connected || paused);
  $("#ws-status").textContent = !connected
    ? "WebSocket disconnected"
    : paused
      ? "Display paused"
      : "WebSocket connected";
  $("#sequence").textContent = "Sequence " + number(shown.seq);
  $("#transport").textContent =
    (st.transport ?? "Waiting") + " / " + (st.baudrate ?? "—");
  $("#problem").hidden = !st.problem && !filterPeer;
  if (st.problem) $("#problem").textContent = st.problem;
  else if (filterPeer)
    $("#problem").replaceChildren(
      document.createTextNode("Source " + filterPeer + "  "),
      btn("Clear filter", () => {
        filterPeer = null;
        render();
      }),
    );
  if (page === "diagnostics") {
    renderDiagnostics();
    return;
  }
  if (page === "clients") {
    renderClients(state.clients);
    return;
  }
  if (page === "operations") {
    if (catalog.modes) climateDevices();
    return;
  }
  if (page !== "monitor") return;
  $("#devices-n").textContent = number(shown.peers.length);
  $("#rx-n").textContent = number(shown.ports.reduce((s, p) => s + p.rx, 0));
  $("#tx-n").textContent = number(shown.ports.reduce((s, p) => s + p.tx, 0));
  $("#gateway-mode").textContent = st.mode || "—";
  for (const p of shown.ports) {
    let b = $('[data-port="' + p.port + '"]');
    if (!b) {
      b = el("button", null, "port");
      b.type = "button";
      b.dataset.port = p.port;
      b.title = "Filter bus " + p.port;
      const head = el("div", null, "port-head");
      head.append(el("span", "Bus " + p.port), el("span", "", "state"));
      b.append(head, el("canvas"));
      $("#ports").append(b);
      b.onclick = () => {
        portFilter = portFilter === p.port ? null : p.port;
        filterPeer = null;
        render();
      };
    }
    b.setAttribute("aria-pressed", String(portFilter === p.port));
    b.classList.toggle("active-filter", portFilter === p.port);
    $(".state", b).textContent = p.activity.some(Boolean) ? "Active" : "Quiet";
    spark($("canvas", b), p.activity);
  }
  if (view !== "devices") {
    traceTabs.inventory(shown.devices, shown.peers);
    if (
      !traceTabs.chosen &&
      st.firmware === "Stock" &&
      traceTabs.active !== "trpc"
    )
      traceTabs.select("trpc", false);
    traceTabs.update(
      displayedEvents(),
      JSON.stringify([
        traceTabs.active === "tn4" ? [portFilter, filterPeer] : null,
        $("#search").value,
        traceTabs.devices.select.value,
      ]),
    );
  }
  if (view === "trace") return;
  const search = $("#search").value.trim().toLowerCase();
  const peers = shown.peers
    .filter((p) => match(p, search))
    .sort(
      (a, b) =>
        Boolean(b.model) - Boolean(a.model) ||
        (b.temperature_f != null) - (a.temperature_f != null) ||
        a.port - b.port ||
        a.subnet - b.subnet ||
        a.node - b.node,
    );
  showCards(peers);
  $("#peer-count").textContent = number(peers.length) + " sources";
  rows(
    $("#peers"),
    peers,
    "Waiting for matching native sources",
    4,
    (p) => {
      const source = el(
        "span",
        p.model
          ? "tekmar " + p.model
          : p.temperature_f != null
            ? "Air temperature"
            : p.port == null
              ? "tHA device"
              : "Source " + hex(p.node),
      );
      source.append(
        el(
          "small",
          (p.address != null ? "tHA " + p.address + " · " : "") +
            (p.port == null ? "RS-232" : hex(p.subnet) + " / " + hex(p.node)),
          "mono",
        ),
      );
      return [
        source,
        p.port,
        p.temperature_f == null ? "—" : p.temperature_f.toFixed(1) + " °F",
        number(p.count),
      ];
    },
    peerDetails,
  );
}
const deviceViews = new Map();
function showCards(peers) {
  const devices = (shown.devices || []).filter(
    (d) =>
      (!filterPeer || peers.some((p) => p.address === d.address)) &&
      (portFilter === null || portFilter === d.port) &&
      (!$("#search").value ||
        JSON.stringify(d)
          .toLowerCase()
          .includes($("#search").value.toLowerCase())),
  );
  const box = $("#device-cards"),
    present = new Set();
  function append(d, summary, open) {
    present.add(d.key);
    const sig = JSON.stringify([
      d.model,
      d.family,
      d.address,
      d.port,
      d.node,
      d.subnet,
      d.temperature_f,
      summary,
    ]);
    const old = deviceViews.get(d.key);
    if (old?.sig === sig) return;
    const node = old?.node || el("article", null, "device-record");
    node.dataset.device = d.key;
    node.replaceChildren(deviceCard(d, open, summary));
    if (!old) box.append(node);
    deviceViews.set(d.key, { node, sig });
  }
  if (portFilter === null && !filterPeer && !$("#search").value) {
    const status = shown.status;
    append(
      { key: "gateway", model: "482", family: "gateway" },
      [status.mode, status.connection].filter(Boolean),
      () => {
        const s = shown.status;
        openDetail("tekmar 482 · Gateway", [
          el("h3", "Gateway configuration"),
          facts(s),
          trafficPanel(null, (key) => traceTabs.devices.name(key)),
          btn("Open diagnostics", () => {
            $("#detail").close();
            setPage("diagnostics");
          }),
        ]);
      },
    );
  }
  function matchesDevice(p, d) {
    return (
      p.node != null &&
      p.port === d.port &&
      p.address === d.address &&
      p.model != null &&
      p.model === d.model
    );
  }
  for (const d of devices) {
    const r = shown.device_details[d.key],
      p = peers.find((p) => matchesDevice(p, d));
    append(
      { ...d, temperature_f: d.temperature_f ?? p?.temperature_f },
      thermostatSummary(r?.settings, p),
      () => deviceDetails(d.address),
    );
  }
  for (const p of peers) {
    if ((!p.model && !p.family) || devices.some((d) => matchesDevice(p, d)))
      continue;
    append(p, thermostatSummary({}, p), () =>
      peerDetails(shown.peers.find((row) => row.key === p.key) || p),
    );
  }
  for (const [key, view] of deviceViews)
    if (!present.has(key)) {
      view.node.remove();
      deviceViews.delete(key);
    }
}
async function deviceDetails(address) {
  try {
    const resp = await fetch("/api/devices/" + address);
    if (!resp.ok) throw Error("Device is no longer in the inventory");
    const d = await resp.json(),
      intro = el("div", null, "device-intro");
    intro.append(
      instrument(d),
      facts({
        address: d.address,
        port: d.port,
        family: d.family,
        type_code: d.metadata.type_code,
        device_version: d.metadata.version,
        setback_events: d.setback_events,
      }),
    );
    const config = d.settings || {};
    const body = [
      intro,
      btn("Filter trace", () => {
        $("#detail").close();
        traceTabs.inventory(shown.devices, shown.peers);
        traceTabs.devices.set(d.key, false);
        traceTabs.select("trpc");
        setView("trace");
      }),
      el("h3", "Reported configuration"),
      Object.keys(config).length
        ? facts(config)
        : el("p", "No configuration values reported yet.", "meta"),
    ];
    if (d.controls.length) body.push(settingForm(d));
    else body.push(el("p", "No writable tRPC settings reported.", "meta"));
    for (const [label, values] of [
      ["Device metadata", d.metadata],
      ["Reported capabilities", d.capabilities],
      ["Raw setting values", d.raw_settings],
    ]) {
      if (values && Object.keys(values).length)
        body.push(el("h3", label), facts(values));
    }
    for (const p of d.native_sources || []) {
      const group = el("section");
      group.append(
        el("h3", "Native configuration · " + p.key),
        trafficPanel(p),
        facts(p),
      );
      body.push(group);
    }
    openDetail(
      (d.model ? "tekmar " + d.model : "Device") + " · " + d.address,
      body,
    );
  } catch (err) {
    showResult(err.message);
  }
}
function settingForm(d) {
  const form = el("form", null, "work-form"),
    fields = el("div", null, "formrow");
  const label = (text, input) => {
    const l = el("label", text);
    l.append(input);
    return l;
  };
  const setting = el("select"),
    slot = el("select"),
    mode = el("select"),
    display = el("output");
  choose(
    setting,
    d.controls.map((k) => [k, tidy(k)]),
  );
  choose(
    mode,
    catalog.modes.map((k) => [k, tidy(k)]),
  );
  choose(
    slot,
    Object.entries(catalog.setbacks)
      .filter(
        ([k]) =>
          k === "CURRENT" || k === "AWAY" || k.endsWith("_" + d.setback_events),
      )
      .map(([k, v]) => [v, tidy(k)]),
  );
  slot.value = catalog.setbacks.CURRENT;
  const target = el("div", null, "stepper"),
    down = btn("", () => adjust(-1)),
    up = btn("", () => adjust(1)),
    apply = el("button", "Review change", "primary");
  for (const [b, n] of [
    [down, "Decrease"],
    [up, "Increase"],
  ]) {
    b.title = n;
    b.ariaLabel = n;
    b.append(icon("chevron-right"));
    b.className = n.toLowerCase();
  }
  display.setAttribute("aria-live", "polite");
  target.append(down, display, up);
  const sl = label("Setback", slot),
    ml = label("Mode", mode),
    vl = el("div", null, "step-field"),
    caption = el("span");
  vl.append(caption, target);
  let value, base, step, temperature;
  function update() {
    display.textContent =
      value == null ? "Not reported" : value + (temperature ? " °C" : " %");
    apply.disabled =
      setting.value === "mode"
        ? !mode.value || mode.value === base
        : value == null || value === base;
  }
  function reset() {
    const s = setting.value,
      tag = slot.selectedOptions[0]?.textContent
        .replaceAll(" ", "_")
        .toLowerCase();
    const k = {
      heat: "heat_setpoints",
      cool: "cool_setpoints",
      floor: "slab_setpoints",
      target: "setpoint_targets",
      fan: "fan_percent",
      humidity_min: "humidity_setpoint_min",
      humidity_max: "humidity_setpoint_max",
    }[s];
    const cell = d.settings?.[k],
      reading = s.startsWith("humidity") ? cell : cell?.[tag];
    temperature = ["heat", "cool", "floor", "target"].includes(s);
    vl.hidden = s === "mode";
    ml.hidden = s !== "mode";
    sl.hidden = s === "mode" || s.startsWith("humidity");
    caption.textContent = temperature ? "Target" : "Percentage";
    step = temperature
      ? s === "target"
        ? 0.1
        : 0.5
      : s === "fan"
        ? d.fan_step || 1
        : 1;
    value =
      reading?.celsius ??
      reading?.percent ??
      (typeof reading === "number" ? reading : null);
    base = s === "mode" ? d.settings?.mode_setting?.name?.toUpperCase() : value;
    if (s === "mode") mode.value = base || "";
    update();
  }
  function adjust(dir) {
    const start =
      value ??
      (temperature ? (d.settings?.current_temperature?.celsius ?? 20) : 0);
    value = Math.round((start + dir * step) * 10) / 10;
    if (!temperature) value = Math.min(100, Math.max(0, value));
    update();
  }
  setting.onchange = slot.onchange = reset;
  mode.onchange = update;
  fields.append(label("Setting", setting), sl, vl, ml);
  form.append(fields, apply);
  reset();
  form.onsubmit = (e) => {
    e.preventDefault();
    if (apply.disabled) return;
    $("#detail").close();
    review({
      name: "device-setting",
      apply: true,
      values: {
        address: d.address,
        setting: setting.value,
        setback: Number(slot.value),
        value: setting.value === "mode" ? mode.value : value,
      },
    });
  };
  return form;
}
$("#refresh-devices").onclick = () => review({ name: "inventory-read" });
function climateDevices() {
  const select = $("#climate-device"),
    ds = state.devices.filter((d) => d.controls?.length);
  const sig = ds.map((d) => [d.key, d.model]).join("|");
  if (select.dataset.items === sig) return;
  const old = select.value;
  choose(
    select,
    ds.map((d) => [
      d.key,
      "tekmar " + (d.model || "device") + " · " + d.address,
    ]),
  );
  if (ds.some((d) => d.key === old)) select.value = old;
  select.dataset.items = sig;
  select.onchange();
}
$("#climate-device").onchange = () => {
  const d = state.device_details[$("#climate-device").value];
  $("#climate-settings").replaceChildren(
    d
      ? settingForm(d)
      : el("p", "No controllable devices in this inventory.", "meta"),
  );
};
function openDetail(title, body) {
  $("#detail-title").textContent = title;
  $("#detail-body").replaceChildren(...body);
  if (!$("#detail").open) $("#detail").showModal();
}
function eventDetails(e) {
  const info = ["#" + e.seq, stamp(e.at), "482 " + tracePath(e)];
  if (e.port != null) info.push("Bus " + e.port);
  if (e.host_monotonic_ns != null)
    info.push("Host clock " + e.host_monotonic_ns + " ns");
  const endpoints = el("div", null, "frame-endpoints");
  traceTabs.devices.fill(endpoints, e);
  const body = [
    el("p", info.join(" · "), "capture-line"),
    endpoints,
    packet(e, true),
  ];
  openDetail(e.name, body);
}
async function peerDetails(p) {
  try {
    const resp = await fetch("/api/peers/" + encodeURIComponent(p.key));
    if (!resp.ok) throw Error("Source is no longer on this connection");
    const record = await resp.json(),
      actions = el("div", null, "commands");
    actions.append(
      btn("Filter this source", () => {
        filterPeer = p.key;
        portFilter = null;
        $("#detail").close();
        traceTabs.select(p.node == null ? "trpc" : "tn4");
        setView("mixed");
      }),
      btn("Device controls", () => {
        $("#detail").close();
        setPage("operations");
        if (state.device_details["tha:" + p.address]?.controls?.length) {
          selectOperation("climate");
          climateDevices();
          $("#climate-device").value = "tha:" + p.address;
          $("#climate-device").onchange();
        } else {
          selectOperation("catalog");
          $("#catalog").elements.command.value = "read-device";
          commandHelp(p);
        }
      }),
    );
    const full = el("section");
    full.append(el("h3", "Decoded device data"), facts(record));
    openDetail(p.model ? "tekmar " + p.model : "Network source " + p.key, [
      facts({
        bus: p.port,
        display_address: p.address,
        model: p.model,
        family: p.family,
        air_temperature:
          p.temperature_f == null ? null : p.temperature_f.toFixed(1) + " °F",
        calling: p.calling,
        last_observation: stamp(p.last),
        frames: p.count,
      }),
      actions,
      ...(p.node != null ? [trafficPanel(p)] : []),
      full,
    ]);
  } catch (e) {
    showResult(e.message);
  }
}
function renderClients(c) {
  const live = c?.active ?? [],
    recent = c?.recent ?? [];
  $("#client-listener").textContent = !connected
    ? "Disconnected"
    : !c?.listening
      ? "Disabled"
      : c.available
        ? "Listening"
        : "Waiting for gateway";
  $("#client-binding").textContent = c?.listening
    ? `TCP · ${c.host.includes(":") ? "[" + c.host + "]" : c.host}:${c.port}`
    : "";
  $("#client-active").textContent = number(live.length);
  clientInspector.update(c);
  $("#client-total").textContent = number(c?.total ?? 0);
  const label = (s) => (s ? tidy(s.toLowerCase()) : null);
  rows(
    $("#client-rows"),
    live,
    "No compatibility clients connected",
    8,
    (p) => [
      p.peer,
      stamp(p.connected),
      number(p.received),
      number(p.sent),
      number(p.filtered),
      label(p.last_request),
      label(p.last_reply),
      p.activity ? stamp(p.activity) : null,
    ],
  );
}

function renderDiagnostics() {
  const st = state.status,
    facts = $("#facts");
    (p) => clientInspector.choose(p.id),
  );
  rows(
    $("#client-history"),
    recent,
    "No recent disconnects",
    5,
    (p) => [
      p.peer,
      stamp(p.closed),
      number(p.received),
      number(p.sent),
      p.reason,
    ],
    (p) => clientInspector.choose(p.id),
  if (st.connection === "online") {
    const settings = JSON.stringify([
      st.mode,
      st.errors,
      st.trace_ports,
      st.ack_ports,
      st.error_sources,
    ]);
    const form = $("#monitor-config");
    if (form.dataset.settings !== settings) {
      form.dataset.settings = settings;
      form.elements.mode.value = st.mode;
      form.elements.errors.checked = st.errors;
      for (const name of ["trace_ports", "ack_ports", "error_sources"])
        for (const input of form.querySelectorAll("input[name=" + name + "]"))
          input.checked = (st[name] || []).includes(Number(input.value));
    }
  }
  facts.replaceChildren();
  for (const [k, v] of [
    ["Connection", st.connection],
    ["Firmware", st.firmware],
    ["Revision", st.revision],
    ["Transport", st.transport + " · " + st.baudrate + " baud"],
    ["Mode", st.mode],
    ["Native capture", st.trace ? "Active" : "Off"],
    ["Error reporting", st.errors ? "Enabled" : "Off"],
    ["Capabilities", (st.features || []).map(tidy).join(", ")],
  ]) {
    facts.append(el("dt", k), el("dd", v ?? "—"));
  }
  const counters = [...state.counters].sort(
    (a, b) =>
      Number(BigInt(b.count) > 0n) - Number(BigInt(a.count) > 0n) ||
      b.last_seen_ms - a.last_seen_ms,
  );
  const groups = $("#errors");
  for (const source of [
    "gateway",
    ...Array.from({ length: 5 }, (_, p) => "port_" + p),
  ]) {
    let group = groups.querySelector("[data-source=" + source + "]");
    if (!group) {
      group = el("section", null, "counter-group");
      group.dataset.source = source;
      const title = el(
        "h3",
        source === "gateway" ? "Gateway / UART" : "tN4 bus " + source.slice(-1),
      );
      const table = el("table"),
        heading = table.createTHead().insertRow();
      for (const label of ["Reason", "Count", "Last seen (ms)"])
        heading.append(el("th", label));
      table.createTBody();
      group.append(title, table);
      groups.append(group);
    }
    const items = counters.filter((item) => item.source === source);
    const signature = JSON.stringify(items);
    if (signature !== group.dataset.signature) {
      rows($("tbody", group), items, "Not reported", 3, (e) => [
        tidy(e.reason),
        e.count,
        e.count === "0" ? "–" : e.last_seen_ms,
      ]);
      group.dataset.signature = signature;
    }
  }
}
function connect() {
  const ws = new WebSocket(
    (location.protocol === "https:" ? "wss:" : "ws:") +
      "//" +
      location.host +
      "/ws",
  );
  ws.onopen = () => {
    connected = true;
    retry = 500;
    render();
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    const events = (m.type === "snapshot" ? [] : state.events)
      .concat(m.events)
      .slice(-1200);
    const patch = (key, list) => {
      const result = m.type === "snapshot" ? {} : state[key];
      Object.assign(result, m[key]);
      const keys = new Set(list.map((item) => item.key));
      for (const id of Object.keys(result))
        if (!keys.has(id)) delete result[id];
      return result;
    };
    Object.assign(state, m, {
      events,
      device_details: patch("device_details", m.devices),
    });
    if (!paused) render();
  };
  ws.onclose = () => {
    connected = false;
    render();
    setTimeout(connect, retry);
    retry = Math.min(8000, retry * 2);
  };
}
$("#pause").onclick = () => {
  paused = !paused;
  if (paused) shown = structuredClone(state);
  $("#pause").replaceChildren(icon(paused ? "play" : "pause"));
  $("#pause").title = paused ? "Resume display" : "Pause display";
  $("#pause").setAttribute("aria-label", $("#pause").title);
  render();
};
$("#search").oninput = render;
$("#export").onclick = () => {
  const blob = new Blob(
    [
      displayedEvents()
        .map((e) => JSON.stringify(e))
        .join("\n") + "\n",
    ],
    { type: "application/x-ndjson" },
  );
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = "tekmar-482-trace.ndjson";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};
$$("[data-close]").forEach(
  (b) => (b.onclick = () => b.closest("dialog").close()),
);
$("#operator").onclick = () => {
  if (key) {
    key = "";
    $("#operator span").textContent = "Observer";
  } else $("#auth").showModal();
};
async function post(url, body, k = key) {
  const r = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + k,
    },
    body: JSON.stringify(body),
  });
  const v = await r.json();
  if (!r.ok)
    throw Error(
      Array.isArray(v.detail)
        ? v.detail.map((error) => error.msg).join("; ")
        : String(v.detail || "Operation failed"),
    );
  return v;
}
$("#auth-form").onsubmit = async (e) => {
  e.preventDefault();
  const k = e.target.elements.key.value;
  try {
    await post("/api/operator", {}, k);
    key = k;
    e.target.reset();
    $("#auth-error").textContent = "";
    $("#operator span").textContent = "Operator";
    $("#auth").close();
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
};
function showResult(output) {
  $("#output").hidden = false;
  $("#result").replaceChildren(
    typeof output === "object" && output !== null
      ? facts(output)
      : el("p", String(output)),
  );
}
$("#dismiss-output").onclick = () => ($("#output").hidden = true);
function review(payload) {
  if (catalog.locked && !key) {
    $("#auth").showModal();
    return;
  }
  if (busy) {
    showResult("Another operation is in progress.");
    return;
  }
  pending = { ...payload, confirmed: true };
  $("#review").replaceChildren(
    facts({
      operation: payload.name,
      ...payload.values,
      ...(payload.args?.length ? { arguments: payload.args.join(" ") } : {}),
    }),
  );
  $("#confirm").showModal();
}
$("#execute").onclick = async () => {
  const op = pending;
  pending = null;
  $("#confirm").close();
  if (!op || busy) return;
  busy = true;
  showResult("Operation in progress…");
  try {
    const res = await post("/api/operations", op);
    showResult(res.result);
  } catch (err) {
    showResult(err.message);
  } finally {
    busy = false;
  }
};
function mask(name) {
  return $$('input[name="' + name + '"]:checked').map((input) =>
    Number(input.value),
  );
}
$("#monitor-config").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  review({
    name: "monitor",
    values: {
      mode: f.mode.value,
      errors: f.errors.checked,
      trace_ports: mask("trace_ports"),
      ack_ports: f.mode.value === "raw" ? mask("ack_ports") : [],
      error_sources: mask("error_sources"),
    },
  });
};
$("#trace-state").onclick = () => review({ name: "trace-state" });
$("#read-error").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  review({
    name: "error-read",
    values: { source: Number(f.source.value), reason: f.reason.value },
  });
};
let readTransaction, readRaw, readTool;
function trpcFields() {
  readTransaction = transactionEditors(
    $("#trpc-fields"),
    $("#trpc").elements.method.value,
    catalog,
    state.devices,
  );
}
$("#trpc").elements.method.onchange = trpcFields;
$("#trpc").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  if (readTransaction)
    review({
      name: "trpc",
      apply: f.service.value === "update",
      values: {
        service: f.service.value,
        method: f.method.value,
        fields: readTransaction(),
      },
    });
};
$("#raw").onsubmit = (e) => {
  e.preventDefault();
  if (readRaw)
    review({
      name: "raw-send",
      apply: true,
      values: { port: Number(e.target.elements.port.value), hex: readRaw() },
    });
};
function commandHelp(peer) {
  const form = $("#catalog"),
    c = catalog.commands.find((c) => c.name === form.elements.command.value);
  $("#tool-apply").hidden = !c?.can_apply;
  readTool = c ? toolEditors($("#tool-fields"), c, catalog, peer) : null;
}
$("#catalog").elements.command.onchange = () => commandHelp();
$("#catalog").onsubmit = (e) => {
  e.preventDefault();
  if (!readTool) return;
  try {
    review({
      name: e.target.elements.command.value,
      args: readTool(),
      apply: e.submitter?.value === "apply",
    });
  } catch (err) {
    showResult(err.message);
  }
};
async function init() {
  connect();
  const ports = Array.from({ length: 5 }, (_, p) => [p, "Bus " + p]);
  for (const [name, title, options, checked] of [
    ["trace_ports", "Normal trace", ports, true],
    ["ack_ports", "Raw link ACK", ports, false],
    ["error_sources", "Error sources", [...ports, [5, "Gateway"]], true],
  ]) {
    const row = el("fieldset", null, "mask");
    row.append(el("legend", title));
    for (const [port, label] of options) {
      const wrap = el("label", null, "check"),
        box = el("input");
      box.type = "checkbox";
      box.name = name;
      box.value = port;
      box.checked = checked;
      wrap.append(box, document.createTextNode(label));
      row.append(wrap);
    }
    $("#masks").append(row);
  }
  for (const s of $$("select[name=port]")) choose(s, ports);
  choose($("#read-error select[name=source]"), [...ports, [5, "Gateway"]]);
  try {
    const res = await fetch("/api/catalog");
    catalog = await res.json();
    if (!catalog.locked) {
      $("#operator").replaceChildren(
        icon("unlock-keyhole"),
        el("span", "Controls unlocked"),
      );
      $("#operator").disabled = true;
    }
    choose(
      $("#trpc select[name=method]"),
      catalog.methods.map((m) => [m, tidy(m)]),
    );
    trpcFields();
    readRaw = rawEditor($("#raw-fields"), catalog);
    function reasons() {
      const host = $("#read-error").elements.source.value === "5";
      choose(
        $("#read-error").elements.reason,
        catalog.reasons
          .filter((r) => /^(UART_|TPCK_|ADDRESS_NOTICE)/.test(r) === host)
          .map((r) => [r, tidy(r)]),
      );
    }
    $("#read-error").elements.source.onchange = reasons;
    reasons();
    const select = $("#catalog").elements.command;
    for (const group of [...new Set(catalog.commands.map((c) => c.group))]) {
      const g = el("optgroup");
      g.label = group;
      for (const c of catalog.commands.filter((c) => c.group === group))
        g.append(new Option(c.name, c.name));
      select.append(g);
    }
    commandHelp();
  } catch (err) {
    showResult(err.message);
  }
}
init();
