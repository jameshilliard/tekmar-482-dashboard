import {
  deviceCard,
  instrument,
  tracePath,
  packet,
  live,
  physical,
} from "./instruments.js";
import { TraceHistory } from "./history.js";
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
const pretty = (obj) => JSON.stringify(obj, null, 2);
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
  source_details: {},
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
  for (const id of ["monitor", "operations", "diagnostics"])
    $("#" + id).hidden = id !== which;
  $$("[data-page]").forEach((b) =>
    b.classList.toggle("selected", b.dataset.page === which),
  );
  $("#title").textContent = $("#crumb").textContent = {
    monitor: "Network monitor",
    diagnostics: "Diagnostics",
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
const match = (item, f) => {
  return (
    (portFilter === null || item.port === portFilter) &&
    (!filterPeer || item.key === filterPeer || item.peer === filterPeer) &&
    (!f || JSON.stringify(item).toLowerCase().includes(f))
  );
};
function displayedEvents() {
  const search = $("#search").value.trim().toLowerCase(),
    layer = $("#layer").value;
  return shown.events.filter(
    (e) => match(e, search) && (!layer || e.layer === layer),
  );
}
const historyView = new TraceHistory(
  $(".trace-scroll"),
  $("#follow"),
  (e) => {
    const row = el("tr"),
      time = row.insertCell();
    row.dataset.seq = e.seq;
    time.className = "mono";
    time.append(
      document.createTextNode(stamp(e.at)),
      el("small", tracePath(e), "path"),
    );
    row.insertCell().textContent = e.port ?? "–";
    row.insertCell().append(packet(e));
    row.tabIndex = 0;
    row.onclick = () => eventDetails(e);
    row.onkeydown = (key) => {
      if (key.key === "Enter") eventDetails(e);
    };
    return row;
  },
  (e) => live(e, $("#flow"), $("#live-bytes")),
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
  if (view !== "devices")
    historyView.update(
      displayedEvents(),
      JSON.stringify([
        portFilter,
        filterPeer,
        $("#search").value,
        $("#layer").value,
      ]),
    );
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
  function append(d, groups, open) {
    present.add(d.key);
    const signature = JSON.stringify([d.model, d.temperature_f, groups]);
    let view = deviceViews.get(d.key);
    if (!view) {
      view = { node: el("article", null, "device-record") };
      view.node.dataset.device = d.key;
      deviceViews.set(d.key, view);
      box.append(view.node);
    }
    if (view.signature === signature) return;
    view.signature = signature;
    view.node.replaceChildren(deviceCard(d, open));
    for (const [name, values] of groups) {
      if (!values || !Object.keys(values).length) continue;
      view.node.append(
        el("h3", name),
        facts(
          Object.fromEntries(
            Object.entries(values)
              .filter(([, v]) => v != null)
              .map(([k, v]) => [k, physical(v)]),
          ),
        ),
      );
    }
  }
  if (portFilter === null && !filterPeer && !$("#search").value) {
    const s = shown.status;
    append(
      { key: "gateway", model: "482", family: "gateway" },
      [
        [
          "Gateway",
          {
            firmware: s.firmware,
            revision: s.revision,
            transport: s.transport,
            baudrate: s.baudrate,
            mode: s.mode,
            features: s.features,
            trace_ports: s.trace_ports,
            error_sources: s.error_sources,
            panel: s.panel,
          },
        ],
      ],
      () => setPage("diagnostics"),
    );
  }
  function nativeGroups(p) {
    if (!p) return [];
    const { identity, blocks, values, ...rest } = p;
    return [
      ["Identification", identity],
      ["Native configuration", blocks],
      [
        "Native status",
        Object.fromEntries(
          [
            "climate",
            "snowmelt",
            "fault",
            "line",
            "water",
            "setpoint",
            "io",
            "plant",
            "schedule",
          ]
            .filter((k) => rest[k] != null && Object.keys(rest[k]).length)
            .map((k) => [k, rest[k]]),
        ),
      ],
      ["Observed message fields", values],
    ];
  }
  for (const d of devices) {
    const r = shown.device_details[d.key] || {};
    append(
      d,
      [
        ["Reported settings", r.settings],
        ["Device metadata", r.metadata],
        ["Reported capabilities", r.capabilities],
        [
          "Controls",
          {
            writable: r.controls,
            fan_step_percent: r.controls?.includes("fan") ? r.fan_step : null,
          },
        ],
        ["Raw setting values", r.raw_settings],
        ...peers
          .filter(
            (p) =>
              p.node != null &&
              p.address === d.address &&
              !p.model &&
              !p.family,
          )
          .flatMap((p) =>
            nativeGroups(shown.source_details[p.key]).map(([name, vals]) => [
              name + " · source " + p.key,
              vals,
            ]),
          ),
      ],
      () => deviceDetails(d.address),
    );
  }
  for (const p of peers) {
    if (!p.model && !p.family) continue;
    append(p, nativeGroups(shown.source_details[p.key]), () => peerDetails(p));
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
    const config = Object.fromEntries(
      Object.entries(d.settings || {})
        .filter(([, v]) => v != null)
        .map(([k, v]) => [k, physical(v)]),
    );
    const body = [
      intro,
      el("h3", "Reported configuration"),
      Object.keys(config).length
        ? facts(config)
        : el("p", "No configuration values reported yet.", "meta"),
    ];
    if (d.controls.length) body.push(settingForm(d));
    else body.push(el("p", "No writable tRPC settings reported.", "meta"));
    for (const p of d.native_sources || []) {
      const group = el("details");
      group.append(
        el("summary", "Native configuration · " + p.key),
        el("pre", pretty(p)),
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
  const form = el("form", null, "work-form");
  form.append(el("h3", "Device settings"));
  const fields = el("div", null, "formrow"),
    setting = el("select"),
    slot = el("select"),
    entry = el("input"),
    mode = el("select"),
    unit = el("span");
  const label = (title, input) => {
    const l = el("label", title);
    l.append(input);
    return l;
  };
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
          k === "CURRENT" ||
          k === "AWAY" ||
          (d.setback_events && k.endsWith("_" + d.setback_events)),
      )
      .map(([k, v]) => [v, tidy(k)]),
  );
  slot.value = catalog.setbacks.CURRENT;
  entry.type = "number";
  entry.step = "any";
  const vl = label("", entry),
    ml = label("Mode", mode),
    sl = label("Setback", slot);
  vl.prepend(unit);
  function change() {
    const s = setting.value;
    vl.hidden = s === "mode";
    ml.hidden = s !== "mode";
    sl.hidden = s === "mode" || s.startsWith("humidity");
    entry.required = s !== "mode";
    unit.textContent = ["heat", "cool", "floor", "target"].includes(s)
      ? "Target (°C)"
      : "Value (%)";
    entry.step =
      s === "fan" ? d.fan_step : s.startsWith("humidity") ? 1 : "any";
  }
  fields.append(label("Setting", setting), vl, ml, sl);
  form.append(fields, el("button", "Review change", "primary"));
  setting.onchange = change;
  change();
  form.onsubmit = (e) => {
    e.preventDefault();
    $("#detail").close();
    review({
      name: "device-setting",
      apply: true,
      values: {
        address: d.address,
        setting: setting.value,
        setback: Number(slot.value),
        value: setting.value === "mode" ? mode.value : Number(entry.value),
      },
    });
  };
  return form;
}
$("#refresh-devices").onclick = () => review({ name: "inventory-read" });
function openDetail(title, body) {
  $("#detail-title").textContent = title;
  $("#detail-body").replaceChildren(...body);
  if (!$("#detail").open) $("#detail").showModal();
}
function eventDetails(e) {
  const nodes = [
    el(
      "p",
      stamp(e.at) + " · " + e.layer + " / " + e.direction + " · #" + e.seq,
      "meta",
    ),
  ];
  if (e.hex) {
    nodes.push(
      el("h3", "Wire payload"),
      el("pre", (e.hex.match(/.{1,2}/g) || []).join(" ").toUpperCase()),
    );
  }
  if (e.fields && Object.keys(e.fields).length) {
    nodes.push(el("h3", "Decoded fields"), facts(e.fields));
  }
  const full = el("details");
  full.append(el("summary", "Complete record"), el("pre", pretty(e)));
  nodes.push(full);
  openDetail(e.name, nodes);
}
function facts(values) {
  const list = el("dl", null, "facts");
  for (const [label, value] of Object.entries(values))
    list.append(
      el("dt", tidy(label)),
      el(
        "dd",
        value == null
          ? "—"
          : typeof value === "object"
            ? pretty(value)
            : String(value),
      ),
    );
  return list;
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
        setView("mixed");
      }),
      btn("Device controls", () => {
        $("#detail").close();
        setPage("operations");
        if (p.port == null) {
          selectOperation("trpc");
          $("#trpc").elements.fields.value = JSON.stringify({
            address: p.address,
          });
          return;
        }
        const f = $("#climate");
        f.elements.port.value = p.port;
        f.elements.subnet.value = hex(p.subnet);
        f.elements.node.value = p.node;
        selectOperation("climate");
      }),
    );
    const full = el("details");
    full.append(
      el("summary", "Decoded device data"),
      el("pre", pretty(record)),
    );
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
      full,
    ]);
  } catch (e) {
    showResult(e.message);
  }
}
function renderDiagnostics() {
  const st = state.status,
    facts = $("#facts");
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
  rows($("#errors"), counters, "No counters received", 4, (e) => [
    tidy(e.source),
    tidy(e.reason),
    e.count,
    e.last_seen_ms,
  ]);
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
      source_details: patch("source_details", m.peers),
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
$("#layer").onchange = render;
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
    throw Error(typeof v.detail === "string" ? v.detail : pretty(v.detail));
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
  $("#result").textContent =
    typeof output === "string" ? output : pretty(output);
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
  $("#review").textContent = pretty(payload);
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
$("#climate").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements,
    target = f.control.value === "target";
  const apply = e.submitter.value === "apply";
  if (target && !f.value.value) {
    showResult("Enter a target value.");
    return;
  }
  review({
    name: target ? "set-thermostat-target" : "set-climate-mode",
    args: [
      f.port.value,
      f.subnet.value,
      f.node.value,
      ...(target
        ? [f.target.value, f.period.value, f.value.value]
        : [f.mode.value]),
    ],
    apply,
  });
};
$("#climate").onchange = (e) => {
  const f = e.currentTarget.elements;
  $$("[data-target]").forEach((x) => (x.hidden = f.control.value !== "target"));
  $("#climate-mode").hidden = f.control.value === "target";
  $("#unit").textContent = ["fan", "humidify", "dehumidify"].includes(
    f.target.value,
  )
    ? "Value (%)"
    : "Value (°C)";
};
$("#trpc").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    review({
      name: "trpc",
      apply: f.service.value === "update",
      values: {
        service: f.service.value,
        method: f.method.value,
        fields: JSON.parse(f.fields.value),
      },
    });
  } catch (err) {
    showResult(err.message);
  }
};
$("#raw").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  review({
    name: "raw-send",
    values: { port: Number(f.port.value), hex: f.hex.value },
    apply: true,
  });
};
function commandHelp() {
  const c = catalog.commands.find(
    (c) => c.name === $("#catalog").elements.command.value,
  );
  $("#help").textContent = c?.help || "";
  $("#tool-apply").hidden = !c?.can_apply;
}
$("#catalog select").onchange = commandHelp;
$("#catalog").onsubmit = (e) => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    const args = JSON.parse(f.args.value);
    if (!Array.isArray(args) || args.some((s) => typeof s !== "string"))
      throw Error("Use an array of argument strings");
    review({
      name: f.command.value,
      args,
      apply: e.submitter.value === "apply",
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
