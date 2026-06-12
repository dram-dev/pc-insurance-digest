// Palette, label maps, number/date formats, and the shared tooltip.
// One source of truth so every chart reads the same.

export const TOPIC_COLORS = {
  macro_linkage:         "#8c9bab",  // dominant catch-all stays muted
  regulatory_rate:       "#9775fa",
  reinsurance_cycle:     "#748ffc",
  ai_insurtech:          "#69db7c",
  underwriting_results:  "#a9e34b",
  cat_event:             "#ff6b6b",
  commercial_specialty:  "#38d9a9",
  personal_lines:        "#4dabf7",
  ma_capital:            "#ffd43b",
  social_inflation:      "#da77f2",
  distribution:          "#b08968",
  reserving:             "#f783ac",
  cyber:                 "#3bc9db",
  supply_chain:          "#e8935a",
  climate_risk:          "#ffa94d",
  rates_cost_of_capital: "#bac8ff",
  analytics_modeling:    "#ced4da",
};

export const TOPIC_LABELS = {
  cat_event: "Catastrophe", reinsurance_cycle: "Reinsurance", regulatory_rate: "Regulatory & Rate",
  underwriting_results: "Underwriting", reserving: "Reserving", ma_capital: "M&A / Capital",
  climate_risk: "Climate Risk", cyber: "Cyber", social_inflation: "Social Inflation",
  ai_insurtech: "AI & Insurtech", distribution: "Distribution", personal_lines: "Personal Lines",
  commercial_specialty: "Commercial & Specialty", macro_linkage: "Macro Linkage",
  rates_cost_of_capital: "Rates & Capital", supply_chain: "Supply Chain",
  analytics_modeling: "Analytics & Modeling",
};

export const topicColor = (t) => TOPIC_COLORS[t] || "#6c757d";
export const topicLabel = (t) => TOPIC_LABELS[t] || t || "—";

export const SOURCE_LABELS = {
  rss: "Trade press (RSS)", edgar: "EDGAR", reddit: "Reddit", hn: "Hacker News",
  substack: "Substack", serff: "SERFF", usgs: "USGS", spc: "SPC", nhc: "NHC",
  nifc: "NIFC", fred: "FRED", state_doi: "State DOI", legiscan: "LegiScan",
  courtlistener: "CourtListener", collision: "Collision data",
  investor_supp: "Investor supplements", industry_research: "Industry research",
};
export const sourceLabel = (s) => SOURCE_LABELS[s] || s;

// ── formats ──────────────────────────────────────────────────────────────────
export const fmt = {
  int: d3.format(","),
  s1: d3.format(".1f"),
  s2: d3.format(".2f"),
  s3: d3.format(".3f"),
  pct0: d3.format(".0%"),
  pct1: d3.format(".1%"),
  signedPct1: d3.format("+.1%"),
  si: d3.format("~s"),
  musd: (v) => (v == null ? "—" : "$" + d3.format(",.0f")(v) + "M"),
  usd: (v) => (v == null ? "—" : "$" + d3.format(",.0f")(v)),
  dateUtc: d3.utcFormat("%Y-%m-%d"),
  dtUtc: d3.utcFormat("%b %d %H:%M"),
  monthDay: d3.utcFormat("%b %d"),
};

// Parse warehouse timestamps as UTC. Handles 'YYYY-MM-DD HH:MM:SS',
// ISO with offset, and bare dates.
export function parseUtc(ts) {
  if (!ts) return null;
  let s = String(ts);
  if (s.length === 10) s += "T00:00:00Z";
  else {
    s = s.replace(" ", "T");
    if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
  }
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

export function agoUtc(ts) {
  const d = parseUtc(ts);
  if (!d) return "—";
  const mins = Math.max(0, (Date.now() - d.getTime()) / 60000);
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 60 * 36) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

// ── tooltip ──────────────────────────────────────────────────────────────────
const tipEl = () => document.getElementById("tooltip");

export const tip = {
  show(html, event) {
    const el = tipEl();
    el.innerHTML = html;
    el.style.opacity = 1;
    tip.move(event);
  },
  move(event) {
    const el = tipEl();
    const { innerWidth: w, innerHeight: h } = window;
    const r = el.getBoundingClientRect();
    let x = event.clientX + 14, y = event.clientY + 12;
    if (x + r.width > w - 8) x = event.clientX - r.width - 12;
    if (y + r.height > h - 8) y = event.clientY - r.height - 10;
    el.style.left = x + "px";
    el.style.top = y + "px";
  },
  hide() { tipEl().style.opacity = 0; },
};

export function ttRows(title, rows) {
  const body = rows
    .filter((r) => r)
    .map(([k, v]) => `<div class="tt-row"><span>${k}</span><b>${v}</b></div>`)
    .join("");
  return `${title ? `<div class="tt-title">${esc(title)}</div>` : ""}${body}`;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
