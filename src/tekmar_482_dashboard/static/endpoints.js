// Only associate native paths and tHA inventory when the confirmed model and
// port match and there is exactly one such path for the address.
const pathLabel = (key) =>
  key.startsWith("tha:")
    ? "tHA " + key.slice(4)
    : "Bus " + key.replace(":", " · ");

export function inventoryPaths(devices, peers) {
  const paths = new Map();
  for (const d of devices) {
    const matches = peers.filter(
      (p) =>
        p.node != null &&
        p.port === d.port &&
        p.address === d.address &&
        p.model != null &&
        p.model === d.model,
    );
    if (matches.length === 1) paths.set(d.key, matches[0]);
  }
  return paths;
}

export function additionalDeviceKeys(paths, peers) {
  const indexed = [...paths.values()];
  return new Set(
    peers
      .filter((p) => {
        if ((!p.model && !p.family) || indexed.includes(p)) return false;
        // A repeated configuration report is a trace source, not another control.
        return !(
          p.family === "thermostat" &&
          p.model != null &&
          p.node != null &&
          p.temperature_f == null &&
          p.calling == null &&
          indexed.some(
            (q) =>
              q.port !== p.port &&
              q.subnet === p.subnet &&
              q.node === p.node &&
              q.model === p.model,
          )
        );
      })
      .map((p) => p.key),
  );
}
export class Endpoints {
  constructor(select, onChange) {
    this.select = select;
    this.onChange = onChange;
    this.labels = new Map();
    this.aliases = new Map();
    this.version = 0;
    select.onchange = () => onChange();
  }
  update(devices, peers) {
    const list = [...devices, ...peers.filter((p) => p.node != null)];
    const signature = JSON.stringify(
      list
        .map((d) => [d.key, d.model, d.family, d.port, d.address])
        .sort((a, b) => a[0].localeCompare(b[0])),
    );
    if (signature === this.signature) return false;
    this.signature = signature;
    this.version++;
    this.labels = new Map(
      list.map((d) => [
        d.key,
        [d.model ? "tekmar " + d.model : null, d.family?.replaceAll("_", " ")]
          .filter(Boolean)
          .join(" · "),
      ]),
    );
    this.aliases.clear();
    for (const [key, p] of inventoryPaths(devices, peers))
      this.aliases.set(p.key, key);
    const old = this.select.value;
    this.select.replaceChildren(new Option("All devices", ""));
    for (const d of list
      .filter((d) => !this.aliases.has(d.key))
      .sort((a, b) => a.key.localeCompare(b.key, undefined, { numeric: true })))
      this.select.add(new Option(this.name(d.key), d.key));
    if (old) this.set(old, false);
    return true;
  }
  canonical(key) {
    return this.aliases.get(key) || key;
  }
  name(key) {
    return [this.labels.get(key) || "Unidentified", pathLabel(key)].join(" · ");
  }
  allows(e) {
    return (
      !this.select.value ||
      Object.values(e.endpoints || {})
        .flat()
        .some((key) => this.canonical(key) === this.select.value)
    );
  }
  set(key, notify = true) {
    key = this.canonical(key);
    if (key && ![...this.select.options].some((o) => o.value === key))
      this.select.add(new Option(this.name(key), key));
    this.select.value = key;
    if (notify) this.onChange();
  }
  fill(box, e) {
    box.replaceChildren();
    for (const [role, keys] of Object.entries(e.endpoints || {})) {
      for (const key of keys.slice(0, 2)) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "endpoint-link";
        b.title = "Filter by " + this.name(key);
        const model = document.createElement("b"),
          path = document.createElement("small");
        model.textContent = this.labels.get(key) || "Unidentified";
        path.textContent =
          (role === "device" ? "" : role + " ") + pathLabel(key);
        b.append(model, path);
        b.onclick = (ev) => {
          ev.stopPropagation();
          b.closest("dialog")?.close();
          this.set(key);
        };
        b.onkeydown = (ev) => ev.stopPropagation();
        box.append(b);
      }
      if (keys.length > 2) {
        const more = document.createElement("small");
        more.textContent = "+" + (keys.length - 2) + " addressed nodes";
        box.append(more);
      }
    }
  }
}
