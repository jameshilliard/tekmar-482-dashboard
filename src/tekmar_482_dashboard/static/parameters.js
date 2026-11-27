// Preserve nested protocol structure while keeping every value in one column.
const slots = ["Wake", "Unoccupied", "Occupied", "Sleep", "Away"];
const title = (key) =>
  String(key)
    .replaceAll("_", " ")
    .replace(/^./, (c) => c.toUpperCase());
function scalar(value) {
  if (value == null) return "Not reported";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value !== "object") return String(value).replaceAll("_", " ");
  if (value.fahrenheit != null && value.celsius != null)
    return (
      Number(value.fahrenheit).toFixed(1) +
      " °F / " +
      Number(value.celsius).toFixed(1) +
      " °C" +
      (value.raw != null ? " (raw " + value.raw + ")" : "")
    );
  return null;
}
export function parameters(values) {
  const table = document.createElement("table");
  table.className = "parameters";
  const columns = document.createElement("colgroup");
  columns.append(document.createElement("col"), document.createElement("col"));
  table.append(columns);
  function visit(obj, path = [], parent = "", owner = null) {
    const body = table.createTBody(),
      groups = [];
    const entries = Object.entries(obj);
    for (const [key, value] of entries) {
      const name = Array.isArray(obj)
        ? parent === "slots" && Number(key) < slots.length
          ? slots[key]
          : value?.target
            ? title(value.target)
            : "Item " + (Number(key) + 1)
        : title(key);
      let printed = scalar(value);
      if (
        parent === "slots" &&
        typeof value === "number" &&
        ["heat", "cool", "floor"].includes(owner?.target)
      ) {
        printed =
          value > 0 && value < 255
            ? ((value / 2) * 1.8 + 32).toFixed(1) +
              " °F / " +
              (value / 2).toFixed(1) +
              " °C (raw " +
              value +
              ")"
            : "Unavailable (raw " + value + ")";
      }
      if (printed === null && Object.keys(value).length) {
        groups.push([value, [...path, name], key]);
        continue;
      }
      const row = body.insertRow(),
        label = document.createElement("th");
      label.scope = "row";
      label.textContent = name;
      const cell = document.createElement("td");
      cell.textContent = printed ?? "Not reported";
      row.append(label, cell);
    }
    if (body.rows.length && path.length) {
      const row = body.insertRow(0),
        heading = document.createElement("th");
      heading.colSpan = 2;
      heading.scope = "rowgroup";
      row.className = "parameter-group";
      heading.textContent = path.join(" › ");
      row.append(heading);
    }
    if (!body.rows.length) body.remove();
    for (const [child, trail, name] of groups) visit(child, trail, name, obj);
  }
  visit(values);
  return table;
}

export function thermostatSummary(settings = {}, peer = {}) {
  const summary = [];
  const mode = settings.mode_setting?.name?.toLowerCase();
  if (mode) summary.push(title(mode));
  const targets = [
    ["heat", "Heat"],
    ["cool", "Cool"],
    ["slab", "Floor"],
  ];
  for (const [key, name] of targets) {
    const reading = settings[key + "_setpoints"]?.current?.fahrenheit;
    if (
      reading != null &&
      (mode == null ||
        !["heat", "cool", "off"].includes(mode) ||
        mode === key ||
        (key === "slab" && mode === "heat"))
    )
      summary.push(name + " target " + Number(reading).toFixed(1) + " °F");
  }
  const demand = settings.active_demand?.name;
  if (demand) summary.push(title(demand));
  else if (peer.calling != null)
    summary.push(peer.calling ? "Calling" : "Idle");
  return summary;
}
