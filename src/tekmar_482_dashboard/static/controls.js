// Operation editors return argument tokens; none accepts a free-form string.
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const range = (n) => Array.from({ length: n }, (_, i) => i);
const hex = (n) => n.toString(16).toUpperCase().padStart(2, "0");
const pad = (n) => String(n).padStart(2, "0");
const tidy = (s) => String(s).replaceAll("_", " ");
function select(title, items, initial) {
  const n = el("select");
  n.setAttribute("aria-label", title);
  for (const item of items) {
    const [v, l] = Array.isArray(item) ? item : [item, tidy(item)];
    n.add(new Option(l, v));
  }
  if (
    initial != null &&
    [...n.options].some((o) => o.value === String(initial))
  )
    n.value = initial;
  return n;
}
function command(label, icon, fn) {
  const b = el("button", null, "widget-btn");
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  const img = el("img");
  img.src = "/static/icons/" + icon + ".svg";
  img.alt = "";
  b.append(img);
  b.onclick = fn;
  return b;
}
function picker(name, items, initial) {
  const node = select(name, items, initial);
  return { node, read: () => [node.value] };
}
function box(...children) {
  const n = el("div", null, "value-widget");
  n.append(...children);
  return n;
}
function check(label) {
  const node = el("input");
  node.type = "checkbox";
  node.setAttribute("aria-label", label);
  return node;
}
function optional(w, name) {
  const toggle = check("Set " + name),
    wrap = el("fieldset");
  wrap.className = "optional-widget";
  wrap.disabled = true;
  wrap.append(w.node);
  toggle.onchange = () => {
    wrap.disabled = !toggle.checked;
  };
  return {
    node: box(toggle, wrap),
    read: () => (toggle.checked ? w.read() : []),
  };
}
function word(name, width = 1) {
  const digits = range(width * 2).map((i) =>
    select(
      name + " hex digit " + (i + 1),
      range(16).map((n) => [n, n.toString(16).toUpperCase()]),
    ),
  );
  const value = () => digits.reduce((v, n) => v * 16 + Number(n.value), 0);
  const out = el("output", "0");
  const node = box(...digits, out);
  node.classList.add("hex-word");
  node.onchange = () => {
    out.value = value();
  };
  return { node, read: () => [String(value())] };
}
function numberStep(name, start, allowNegative = false) {
  let value = Number(start) || 0;
  const step = select(name + " step", [0.1, 0.5, 1, 10, 100, 1000], 1),
    out = el("output", String(value));
  const adjust = (dir) => {
    value = Math.round((value + dir * Number(step.value)) * 10) / 10;
    if (!allowNegative) value = Math.max(0, value);
    out.value = value;
  };
  const up = command("Increase " + name, "chevron-right", () => adjust(1)),
    down = command("Decrease " + name, "chevron-right", () => adjust(-1));
  up.classList.add("increase");
  down.classList.add("decrease");
  return { node: box(down, out, up, step), read: () => [String(value)] };
}
function sequence(name, makeRow, required = false, max = Infinity) {
  const entries = [],
    rows = el("div", null, "value-rows");
  const add = command("Add " + name, "plus", () => {
    if (entries.length >= max) return;
    const w = makeRow();
    const remove = command("Remove " + name, "x", () => {
      const i = entries.indexOf(w);
      entries.splice(i, 1);
      row.remove();
      add.disabled = false;
    });
    const row = box(w.node, remove);
    entries.push(w);
    rows.append(row);
    add.disabled = entries.length >= max;
  });
  if (required) add.click();
  return { node: box(rows, add), read: () => entries.flatMap((w) => w.read()) };
}
function bytes(name, max = 63) {
  const w = sequence(
    "byte",
    () =>
      picker(
        name + " byte",
        range(256).map((n) => [hex(n), hex(n)]),
      ),
    false,
    max,
  );
  return {
    node: w.node,
    read: () => {
      const value = w.read().join("");
      return value ? [value] : [];
    },
  };
}
function wallClock() {
  const t = new Date(),
    year = select(
      "Year",
      range(256).map((x) => 2000 + x),
      t.getFullYear(),
    ),
    month = select(
      "Month",
      range(12).map((x) => x + 1),
      t.getMonth() + 1,
    );
  const day = select("Day", []),
    hour = select(
      "Hour",
      range(24).map((n) => [n, pad(n)]),
      t.getHours(),
    ),
    minute = select(
      "Minute",
      range(60).map((n) => [n, pad(n)]),
      t.getMinutes(),
    );
  const days = (initial) => {
    const max = new Date(+year.value, +month.value, 0).getDate();
    day.replaceChildren(...range(max).map((n) => new Option(n + 1, n + 1)));
    day.value = Math.min(initial, max);
  };
  days(t.getDate());
  year.onchange = month.onchange = () => days(Number(day.value));
  return {
    node: box(year, month, day, hour, minute),
    read: () => [
      year.value +
        "-" +
        pad(month.value) +
        "-" +
        pad(day.value) +
        "T" +
        pad(hour.value) +
        ":" +
        pad(minute.value),
    ],
  };
}
function multi(p) {
  const n = el("div", null, "choice-list"),
    checks = p.choices.map((v) => {
      const l = el("label"),
        c = check(tidy(v));
      c.value = v;
      l.append(c, document.createTextNode(tidy(v)));
      n.append(l);
      return c;
    });
  return {
    node: n,
    read: () => checks.filter((c) => c.checked).map((c) => c.value),
  };
}

export function toolEditors(root, operation, catalog, peer) {
  root.replaceChildren();
  function render(group, params) {
    const readers = [],
      widgets = {};
    for (const p of params) {
      const name = tidy(p.name),
        label = el("div", null, "operation-field"),
        title = el("strong", name);
      let w;
      if (p.switch)
        w = picker(name, [
          ["", "Default"],
          [p.flag, "On"],
          ...(p.negative ? [[p.negative, "Off"]] : []),
        ]);
      else if (p.choices.length)
        w =
          p.many || p.repeat
            ? multi(p)
            : picker(
                name,
                [
                  ...(!p.required ? [["", "Default"]] : []),
                  ...p.choices.map((v) => [v, tidy(v)]),
                ],
                peer?.[p.name],
              );
      else if (["probe_subnet", "io_endpoint", "peer"].includes(p.name)) {
        const pair = () => {
          const a = picker(
              "Port or subnet",
              range(p.name === "probe_subnet" ? 5 : 256),
            ),
            b = picker("Subnet or node", range(256));
          return {
            node: box(a.node, b.node),
            read: () =>
              p.name === "probe_subnet"
                ? [a.read()[0] + ":" + b.read()[0]]
                : [...a.read(), ...b.read()],
          };
        };
        w = p.repeat
          ? sequence(name, () => {
              const pairWidget = pair();
              return {
                node: pairWidget.node,
                read: () => [p.flag, ...pairWidget.read()],
              };
            })
          : optional(pair(), name);
      } else if (p.name === "fields") {
        const fields = () =>
          catalog.blocks.find((b) => b.id === Number(widgets.block.read()[0]))
            ?.fields || [];
        const list = () =>
          sequence(
            "field",
            () => {
              const f = picker("Register field", fields()),
                v = word("Register value", 2);
              return {
                node: box(f.node, v.node),
                read: () => [f.read()[0] + "=" + v.read()[0]],
              };
            },
            true,
          );
        let current = list();
        const wrap = box(current.node);
        widgets.block.node.addEventListener("change", () => {
          current = list();
          wrap.replaceChildren(current.node);
        });
        w = { node: wrap, read: () => current.read() };
      } else if (
        p.name === "block" &&
        ["write-block", "write-fields"].includes(operation.name)
      ) {
        w = picker(name, [
          ...(!p.required ? [["", "All blocks"]] : []),
          ...catalog.blocks.map((b) => [
            b.id,
            "0x" + hex(b.id) + " " + tidy(b.name),
          ]),
        ]);
      } else if (p.name === "wall_time") w = wallClock();
      else if (p.name === "at")
        w = picker(name, [
          "disabled",
          ...range(144).map(
            (i) => pad(Math.floor(i / 6)) + ":" + pad((i % 6) * 10),
          ),
        ]);
      else if (p.name === "keys") w = bytes("Key");
      else {
        if (
          [
            "subnet",
            "source_subnet",
            "io_subnet",
            "node",
            "source_node",
            "io_node",
            "channel",
            "dst_mode",
          ].includes(p.name)
        )
          w = picker(
            name,
            range(["node", "source_node"].includes(p.name) ? 64 : 256),
            peer?.[p.name] ?? p.default,
          );
        else if (["block", "selector"].includes(p.name))
          w = word(name, p.name === "block" ? 2 : 4);
        else
          w = numberStep(
            name,
            p.default ?? (p.name === "value" ? 20 : 1),
            p.name === "value",
          );
        if (!p.required) w = optional(w, name);
      }
      widgets[p.name] = w;
      w.node.dataset.param = p.name;
      label.title = p.help;
      label.append(title, w.node);
      group.append(label);
      let children = () => [];
      if (Object.keys(p.variants).length) {
        const nested = el("div", null, "formrow operation-children");
        group.append(nested);
        const change = () => {
          nested.replaceChildren();
          children = render(nested, p.variants[w.read()[0]] || []);
        };
        w.node.addEventListener("change", change);
        change();
      }
      readers.push(() => {
        const vs = w.read().filter((v) => v !== "");
        if (!vs.length && p.required) throw Error("Select " + name);
        if (!vs.length) return [];
        if (["io_endpoint", "probe_subnet"].includes(p.name)) return vs;
        if (p.switch) return vs;
        if (p.repeat) return vs.flatMap((v) => [p.flag, v]);
        return [...(p.flag ? [p.flag] : []), ...vs, ...children()];
      });
    }
    return () => readers.flatMap((fn) => fn());
  }
  return render(root, operation.parameters);
}

export function transactionEditors(root, method, catalog, devices) {
  root.replaceChildren();
  const entries = [];
  for (const { name, width } of catalog.method_fields[method] || []) {
    let w;
    if (name === "address" && devices.length) {
      const addresses = picker(name, [
          ...devices.map((d) => [
            d.address,
            (d.model || "Device") + " · " + d.address,
          ]),
          ["manual", "Other address"],
        ]),
        other = word("Address", width);
      other.node.hidden = true;
      addresses.node.onchange = () => {
        other.node.hidden = addresses.node.value !== "manual";
      };
      w = {
        node: box(addresses.node, other.node),
        read: () =>
          addresses.node.value === "manual" ? other.read() : addresses.read(),
      };
    } else if (name === "setback")
      w = picker(
        name,
        Object.entries(catalog.setbacks).map(([s, id]) => [id, tidy(s)]),
        catalog.setbacks.CURRENT,
      );
    else w = word(name, width);
    const group = el("div", null, "operation-field");
    group.append(el("strong", tidy(name)), w.node);
    root.append(group);
    entries.push([name, w]);
  }
  return () =>
    Object.fromEntries(entries.map(([k, w]) => [k, Number(w.read()[0])]));
}

export function rawEditor(root, catalog) {
  const p = picker("Priority", range(4), 1),
    m = picker(
      "Native message",
      range(1024).map((i) => [
        i,
        "0x" +
          i.toString(16).padStart(3, "0") +
          " " +
          (catalog.native_methods[i] || ""),
      ]),
      0,
    );
  const s = picker("Source subnet", range(256)),
    n = picker("Source node", range(64)),
    payload = bytes("Payload");
  for (const [name, w] of [
    ["Priority", p],
    ["Native message", m],
    ["Source subnet", s],
    ["Source node", n],
    ["Payload bytes", payload],
  ]) {
    const wrap = el("div", null, "operation-field");
    wrap.append(el("strong", name), w.node);
    root.append(wrap);
  }
  return () => {
    const b = payload.read()[0] || "",
      h =
        ((+p.read()[0] << 30) |
          (+m.read()[0] << 20) |
          (+s.read()[0] << 12) |
          (+n.read()[0] << 6) |
          (b.length / 2)) >>>
        0;
    return h.toString(16).padStart(8, "0") + b;
  };
}
