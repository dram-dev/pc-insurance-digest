// DOM building blocks shared by all views: cards, KPIs, chips, selects,
// empty states, and a responsive-SVG harness.

import { esc, topicColor, topicLabel } from "./theme.js";

export function el(tag, attrs = {}, html = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (html) node.innerHTML = html;
  return node;
}

export function card(parent, { title, sub = "", span = 12, note = "" }) {
  const c = el("section", { class: `card span-${span}` });
  c.appendChild(el("header", {},
    `<h3>${esc(title)}</h3>${sub ? `<span class="card-sub">${sub}</span>` : ""}`));
  const chart = el("div", { class: "chart" });
  c.appendChild(chart);
  if (note) c.appendChild(el("footer", { class: "card-note" }, note));
  parent.appendChild(c);
  return chart;
}

export function setSub(chartEl, sub) {
  const header = chartEl.closest(".card")?.querySelector("header");
  if (!header) return;
  let span = header.querySelector(".card-sub");
  if (!span) { span = el("span", { class: "card-sub" }); header.appendChild(span); }
  span.innerHTML = sub;
}

export function kpi(parent, label, value, sub = "") {
  // long non-numeric values (e.g. regime names) read better in UI type
  const textual = String(value).length > 9 && !/^[\d.,%×+\- ]+$/.test(String(value));
  parent.appendChild(el("div", { class: "kpi" },
    `<div class="kpi-label">${esc(label)}</div>
     <div class="kpi-value${textual ? " textual" : ""}">${value}</div>
     <div class="kpi-sub">${sub}</div>`));
}

export function topicChip(topic) {
  return `<span class="chip"><span class="swatch" style="background:${topicColor(topic)}"></span>${esc(topicLabel(topic))}</span>`;
}

export function tierBadge(tier) {
  if (!tier) return `<span class="badge low">—</span>`;
  return `<span class="badge ${esc(tier)}">${esc(tier)}</span>`;
}

export function empty(container, message, glyph = "◌") {
  container.innerHTML =
    `<div class="empty"><div><span class="glyph">${glyph}</span>${message}</div></div>`;
}

// select control: returns the <select>, calls onChange(value) on input.
export function select(parent, { label, options, value, onChange }) {
  const wrap = el("span");
  if (label) wrap.appendChild(el("label", {}, esc(label) + " "));
  const sel = el("select");
  for (const o of options) {
    const opt = el("option", { value: o.value });
    opt.textContent = o.label;
    if (o.value === value) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => onChange(sel.value));
  wrap.appendChild(sel);
  parent.appendChild(wrap);
  return sel;
}

// Responsive SVG: re-runs draw(svg, width) when the card resizes.
// Returns a redraw trigger so callers can re-render on data changes.
export function responsiveSvg(container, height, draw) {
  const svg = d3.select(container).append("svg").attr("height", height);
  let raf = null;
  const render = () => {
    const w = container.clientWidth;
    if (w < 40) return;
    svg.attr("width", w).attr("viewBox", `0 0 ${w} ${height}`);
    svg.selectAll("*").remove();
    draw(svg, w);
  };
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(render);
  });
  ro.observe(container);
  render();
  return render;
}

// Standard horizontal/vertical axes with hairline grid.
export function axes(g, { x, y, w, h, xTicks = 6, yTicks = 5, xFmt, yFmt, grid = "y" }) {
  if (grid.includes("y")) {
    g.append("g").attr("class", "axis")
      .call(d3.axisLeft(y).ticks(yTicks).tickSize(-w).tickFormat(yFmt ?? null))
      .call((s) => s.select(".domain").remove())
      .call((s) => s.selectAll("line").attr("stroke", "var(--grid-line)"));
  }
  const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${h})`)
    .call(d3.axisBottom(x).ticks(xTicks).tickSizeOuter(0).tickFormat(xFmt ?? null));
  xa.select(".domain").attr("stroke", "var(--grid-line)");
  xa.selectAll("line").attr("stroke", "var(--grid-line)");
  return xa;
}

// Clickable legend chips; calls onToggle(activeSet) when toggled.
export function legend(parent, entries, onToggle) {
  const box = el("div", { class: "legend" });
  const active = new Set(entries.map((e) => e.key));
  for (const e of entries) {
    const chip = el("span", { class: "chip", title: "click to toggle" },
      `<span class="swatch" style="background:${e.color}"></span>${esc(e.label)}`);
    chip.addEventListener("click", () => {
      if (active.has(e.key)) { active.delete(e.key); chip.classList.add("off"); }
      else { active.add(e.key); chip.classList.remove("off"); }
      onToggle(active);
    });
    box.appendChild(chip);
  }
  parent.appendChild(box);
  return box;
}
