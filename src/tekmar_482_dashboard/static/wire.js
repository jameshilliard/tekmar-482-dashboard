// Logical fields and exclusive physical end offsets come from the gateway decoder.
// This renderer never dissects tRPC, tN4 or TPCK a second time.
const element = (tag, cls, value) => {
  const n = document.createElement(tag);
  n.className = cls;
  if (value != null) n.textContent = value;
  return n;
};
const offset = (n) => n.toString(16).toUpperCase().padStart(2, "0");
const coverageLabels = {
  known: "Decoded",
  mixed: "Contains unmapped bits",
  unknown: "Unmapped",
  invalid: "Invalid",
  unused: "Reserved or unused",
};
function coverage(el, state = "known") {
  el.dataset.coverage = state;
  el.title = coverageLabels[state];
}
const bytesByEvent = new WeakMap();
function octets(event) {
  let model = bytesByEvent.get(event);
  if (!model) {
    model = {
      bytes: (event.wire_hex.match(/../g) || []).map((s) => s.toUpperCase()),
      escapes: new Set(event.escapes),
    };
    bytesByEvent.set(event, model);
  }
  return model;
}

export function wireDiagram(event, inspect = false, columns = null) {
  const root = element("div", "wire-view" + (inspect ? " inspecting" : ""));
  const tree = element("div", "frame-diagram");
  tree.setAttribute("role", "group");
  tree.setAttribute("aria-label", "TPCK frame");
  const { bytes, escapes } = octets(event);
  let at = 0;
  function draw(node) {
    if (node.children) {
      const section = element("div", "wire-group");
      section.dataset.label = node.label;
      if (node.column) section.dataset.part = node.column;
      section.append(element("span", "wire-legend", node.label));
      const children = element("div", "wire-fields");
      for (const child of node.children) children.append(draw(child));
      if (!node.children.length && !node.notes)
        children.append(element("span", "no-bytes", "Empty"));
      if (node.notes) {
        const notes = element("span", "derived-fields");
        notes.append(element("small", "derived-label", "Derived"));
        for (const [key, value] of Object.entries(node.notes)) {
          const pair = element("span", "derived-value");
          coverage(
            pair,
            key === "truncated"
              ? "invalid"
              : node.unknown_notes?.includes(key)
                ? "unknown"
                : "known",
          );
          pair.append(
            element("small", "", key.replaceAll("_", " ") + ": "),
            document.createTextNode(String(value).replaceAll("_", " ")),
          );
          notes.append(pair);
        }
        children.append(notes);
      }
      section.append(children);
      return section;
    }
    const leaf = element("span", "wire-field " + node.kind);
    coverage(
      leaf,
      node.coverage ||
        (node.bits?.some((b) => b.coverage === "unknown") ? "mixed" : "known"),
    );
    leaf.dataset.label = node.label;
    leaf.dataset.start = at;
    leaf.dataset.end = node.end;
    const digits = element("span", "octets");
    while (at < node.end) {
      const escaped = escapes.has(at);
      const octet = element("span", "wire-byte" + (escaped ? " escape" : ""));
      octet.dataset.offset = offset(at);
      octet.append(element("code", "", bytes[at]));
      if (escaped) octet.append(element("small", "", "ESC"));
      digits.append(octet);
      at++;
    }
    leaf.append(digits, element("small", "field-label", node.label));
    if (node.bits?.length) {
      const groups = element("span", "bit-groups");
      const value = parseInt(bytes[at - 1], 16);
      for (const bit of node.bits) {
        const width = bit.hi - bit.lo + 1,
          v = (value >>> bit.lo) & ((1 << width) - 1);
        const range = "b" + bit.hi + (width > 1 ? ":" + bit.lo : "");
        const g = element("span", "bit-group");
        coverage(g, bit.coverage);
        g.dataset.label = range + " " + bit.label;
        g.append(
          element("code", "bit-value", v.toString(2).padStart(width, "0")),
          element("small", "", range + " " + bit.label),
        );
        groups.append(g);
      }
      leaf.append(groups);
    }
    return leaf;
  }
  if (columns) {
    // Visit in wire order even when the table places native fields first.
    function place(node) {
      if (!node.column && node.children) {
        for (const child of node.children) place(child);
      } else {
        (columns[node.column] || columns.payload).append(draw(node));
      }
    }
    for (const node of event.layout) place(node);
    return;
  }
  tree.append(...event.layout.map(draw));
  root.append(tree);
  if (inspect) enableSelection(root, tree);
  return root;
}

function enableSelection(root, tree) {
  const fields = [...tree.querySelectorAll(".wire-field")];
  if (!fields.length) return;
  const status = element("output", "wire-selection");
  status.setAttribute("aria-label", "Selected wire field");
  for (const [i, field] of fields.entries()) {
    field.tabIndex = i ? -1 : 0;
    field.setAttribute("role", "button");
    field.setAttribute("aria-label", field.dataset.label);
    field.setAttribute("aria-pressed", "false");
  }
  const range = (el) => {
    const a = Number(el.dataset.start),
      b = Number(el.dataset.end) - 1;
    return "W+" + offset(a) + (b === a ? "" : "–" + offset(b));
  };
  let selected,
    selectedBit,
    parents = [];
  function select(leaf, bit) {
    if (!leaf || (leaf === selected && bit === selectedBit)) return;
    if (selected) {
      selected.tabIndex = -1;
      selected.setAttribute("aria-pressed", "false");
      selectedBit?.classList.remove("chosen-bit");
      for (const p of parents) p.classList.remove("on-path");
    }
    selected = leaf;
    selectedBit = bit;
    leaf.tabIndex = 0;
    leaf.setAttribute("aria-pressed", "true");
    bit?.classList.add("chosen-bit");
    parents = [];
    for (
      let p = leaf.parentElement.closest(".wire-group");
      p;
      p = p.parentElement.closest(".wire-group")
    ) {
      parents.unshift(p);
      p.classList.add("on-path");
    }
    status.textContent =
      [
        range(leaf),
        "TPCK",
        ...parents.map((p) => p.dataset.label),
        leaf.dataset.label,
        ...(bit ? [bit.dataset.label] : []),
      ].join(" › ") +
      " · " +
      coverageLabels[(bit || leaf).dataset.coverage];
  }
  tree.addEventListener("click", (e) => {
    const leaf = e.target.closest(".wire-field");
    if (!leaf) return;
    leaf.focus({ preventScroll: true });
    select(leaf, e.target.closest(".bit-group"));
  });
  tree.addEventListener("focusin", (e) =>
    select(e.target.closest(".wire-field"), null),
  );
  tree.addEventListener("keydown", (e) => {
    const leaf = e.target.closest(".wire-field");
    if (!leaf) return;
    const i = fields.indexOf(leaf);
    const to = {
      ArrowRight: i + 1,
      ArrowLeft: i - 1,
      Home: 0,
      End: fields.length - 1,
    }[e.key];
    if (to !== undefined) {
      e.preventDefault();
      fields[Math.max(0, Math.min(fields.length - 1, to))].focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      select(leaf, null);
    }
  });
  root.append(status);
  select(fields[0], null);
}
