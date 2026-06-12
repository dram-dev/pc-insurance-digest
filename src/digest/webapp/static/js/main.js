// Shell: hash router, global time-range store, freshness indicator.

import { get } from "./api.js";
import { agoUtc, esc, parseUtc, fmt } from "./theme.js";
import * as pulse from "./views/pulse.js";
import * as signals from "./views/signals.js";
import * as market from "./views/market.js";
import * as losslab from "./views/losslab.js";
import * as ops from "./views/ops.js";

const VIEWS = {
  pulse:   { title: "Pulse",      mod: pulse,   ranged: true },
  signals: { title: "Signals",    mod: signals, ranged: true },
  market:  { title: "Market",     mod: market,  ranged: true },
  losslab: { title: "Loss Lab",   mod: losslab, ranged: false },
  ops:     { title: "Operations", mod: ops,     ranged: true },
};

const RANGES = [
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
  { days: 365, label: "1y" },
  { days: 0, label: "All" },
];

// Global state — views re-render when the range changes.
export const store = {
  range: 90,
  meta: null,
};

let current = null;

function buildRangeControl() {
  const box = document.getElementById("range-control");
  box.innerHTML = "";
  for (const r of RANGES) {
    const b = document.createElement("button");
    b.textContent = r.label;
    if (r.days === store.range) b.classList.add("active");
    b.addEventListener("click", () => {
      store.range = r.days;
      buildRangeControl();
      render();
    });
    box.appendChild(b);
  }
}

async function render() {
  const name = (location.hash.replace(/^#\//, "") || "pulse").split("?")[0];
  const view = VIEWS[name] || VIEWS.pulse;
  current = name;

  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === (VIEWS[name] ? name : "pulse")));
  document.getElementById("view-title").textContent = view.title;
  document.getElementById("range-control").classList.toggle("hidden", !view.ranged);

  const root = document.getElementById("view");
  root.innerHTML = "";
  try {
    await view.mod.render(root, store);
  } catch (e) {
    root.innerHTML = `<div class="empty"><div><span class="glyph">⚠︎</span>${esc(e.message)}</div></div>`;
    console.error(e);
  }
}

async function boot() {
  buildRangeControl();
  try {
    store.meta = await get("meta");
    const f = document.getElementById("freshness");
    const last = store.meta.last_ingested_at;
    f.innerHTML = `<span class="dot"></span>data ${esc(agoUtc(last))}<br>` +
      `<span style="color:var(--text-3)">${esc(fmt.dtUtc(parseUtc(last)))} UTC · ` +
      `${fmt.int(store.meta.counts.items)} items</span>`;
  } catch (e) {
    console.error("meta failed", e);
  }
  window.addEventListener("hashchange", render);
  render();
}

boot();
