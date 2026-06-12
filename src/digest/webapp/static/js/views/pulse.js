// Pulse — the news-timing hero view: flow, signal timeline, regime, cadence.

import { get } from "../api.js";
import {
  topicColor, topicLabel, sourceLabel, fmt, parseUtc, tip, ttRows, esc,
} from "../theme.js";
import {
  card, kpi, el, empty, responsiveSvg, axes, legend, setSub,
} from "../components.js";

const CYCLE_COLORS = {
  hard_market: "#ff6b6b", transitioning_to_hard: "#ffa94d", stable: "#8c9bab",
  transitioning_to_soft: "#74c0fc", soft_market: "#4dabf7",
};
const CAT_COLORS = {
  low_season: "#38d9a9", active_season: "#ffd43b", post_major_event: "#ff6b6b",
};
const label = (s) => s.replace(/_/g, " ");

export async function render(root, store) {
  const days = store.range;
  const [tl, ev, dist, reg, lat, cad, funnel, runs] = await Promise.all([
    get("timeline", { days }),
    get("events", { days, limit: 500 }),
    get("score-distribution", { days }),
    get("regimes"),
    get("latency", { days }),
    get("cadence", { days }),
    get("ops-funnel", { days }),
    get("ops-runs", { days }),
  ]);

  // ── KPIs ──────────────────────────────────────────────────────────────────
  const kpis = el("div", { class: "kpis" });
  root.appendChild(kpis);
  const kept = d3.sum(tl.rows, (r) => r.n);
  const ingested = d3.sum(funnel.rows, (r) => r.ingested);
  const keptOfIngested = d3.sum(funnel.rows, (r) => r.kept);
  kpi(kpis, "Kept items", fmt.int(kept),
    `${fmt.int(ingested)} ingested · ${ingested ? fmt.pct0(keptOfIngested / ingested) : "—"} keep rate`);

  const top = ev.rows[0];
  kpi(kpis, "Top signal", top ? fmt.s2(top.score) : "—",
    top ? `${esc(top.title)}` : "no scored items in window");

  const regime = store.meta?.regime;
  kpi(kpis, "Market cycle", regime ? label(regime.market_cycle) : "—",
    regime ? `cat load: ${label(regime.cat_load)} · ×${fmt.s2(regime.multiplier)}` : "detector not yet run");

  const activeSources = new Set(
    runs.rows.filter((r) => r.items_new > 0).map((r) => r.source)).size;
  kpi(kpis, "Sources delivering", fmt.int(activeSources),
    `of ${fmt.int(store.meta?.sources?.length ?? 0)} that have ever delivered`);

  const highTier = ev.rows.filter((r) => r.tier === "high").length;
  kpi(kpis, "High-conviction", fmt.int(highTier),
    `of top ${fmt.int(ev.rows.length)} scored · self-calibrated P90`);

  const grid = el("div", { class: "grid" });
  root.appendChild(grid);

  drawNewsFlow(grid, tl, days);
  drawSignalTimeline(grid, ev, dist, days);
  drawRegimeRibbon(grid, reg);
  drawLatency(grid, lat);
  drawCadence(grid, cad, days);
}

// ── News flow: stacked area of kept items per UTC day by topic ──────────────
function drawNewsFlow(grid, tl, days) {
  const chart = card(grid, {
    title: "News flow",
    span: 12,
    note: "Kept items per UTC day, stacked by triage topic. Event time = published, falling back to ingest time. Click a legend chip to exclude a topic.",
  });
  if (!tl.rows.length) return empty(chart, "no kept items in this window");

  const allDays = Array.from(new Set(tl.rows.map((r) => r.day))).sort();
  const start = parseUtc(allDays[0]);
  const end = parseUtc(allDays[allDays.length - 1]);
  const dayRange = d3.utcDay.range(start, d3.utcDay.offset(end, 1)).map(fmt.dateUtc);

  const totals = d3.rollup(tl.rows, (v) => d3.sum(v, (r) => r.n), (r) => r.topic);
  const topics = Array.from(totals.keys()).sort((a, b) => totals.get(b) - totals.get(a));
  const byDay = d3.index(tl.rows, (r) => r.day, (r) => r.topic);

  setSub(chart, `n=${fmt.int(d3.sum(tl.rows, (r) => r.n))} kept · ${dayRange.length} days`);

  let active = new Set(topics);
  let redraw = () => {};

  const draw = (svg, w) => {
    const m = { t: 12, r: 12, b: 24, l: 40 };
    const h = 260, iw = w - m.l - m.r, ih = h - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const keys = topics.filter((t) => active.has(t));
    const series = dayRange.map((day) => {
      const row = { day, date: parseUtc(day) };
      for (const t of keys) row[t] = byDay.get(day)?.get(t)?.n ?? 0;
      return row;
    });
    const stack = d3.stack().keys(keys)(series);

    const x = d3.scaleUtc().domain([start, end]).range([0, iw]);
    const y = d3.scaleLinear()
      .domain([0, d3.max(stack, (s) => d3.max(s, (d) => d[1])) || 1]).nice()
      .range([ih, 0]);

    axes(g, { x, y, w: iw, h: ih, xFmt: d3.utcFormat("%b %d") });

    const area = d3.area()
      .x((d) => x(d.data.date))
      .y0((d) => y(d[0]))
      .y1((d) => y(d[1]))
      .curve(d3.curveMonotoneX);

    g.selectAll(".layer").data(stack).join("path")
      .attr("d", area)
      .attr("fill", (d) => topicColor(d.key))
      .attr("fill-opacity", 0.82)
      .attr("stroke", "rgba(0,0,0,.35)")
      .attr("stroke-width", 0.5);

    // hover: vertical rule + per-day breakdown
    const rule = g.append("line").attr("y1", 0).attr("y2", ih)
      .attr("stroke", "rgba(255,255,255,.25)").attr("stroke-dasharray", "2,3")
      .style("display", "none");
    svg.append("rect")
      .attr("x", m.l).attr("y", m.t).attr("width", iw).attr("height", ih)
      .attr("fill", "transparent")
      .on("mousemove", (e) => {
        const [mx] = d3.pointer(e, g.node());
        const day = fmt.dateUtc(d3.utcDay.round(x.invert(mx)));
        const row = series.find((r) => r.day === day);
        if (!row) return;
        rule.style("display", null).attr("x1", x(row.date)).attr("x2", x(row.date));
        const rows = keys.filter((t) => row[t] > 0)
          .sort((a, b) => row[b] - row[a]).slice(0, 8)
          .map((t) => [`<span style="color:${topicColor(t)}">●</span> ${esc(topicLabel(t))}`, row[t]]);
        const total = d3.sum(keys, (t) => row[t]);
        tip.show(ttRows(day + " UTC", [["total kept", total], ...rows]), e);
      })
      .on("mouseleave", () => { rule.style("display", "none"); tip.hide(); });
  };

  redraw = responsiveSvg(chart, 260, draw);
  legend(chart.closest(".card"),
    topics.map((t) => ({ key: t, label: topicLabel(t), color: topicColor(t) })),
    (act) => { active = act; redraw(); });
}

// ── Signal timeline: scatter of top scored items, time × score ──────────────
function drawSignalTimeline(grid, ev, dist, days) {
  const chart = card(grid, {
    title: "Signal timeline",
    span: 12,
    note: "Each dot is a kept item at its event time (UTC) and latest heuristic score (log scale). " +
      "Dashed rules are the observed tier boundaries — trailing-quantile self-calibration. " +
      "Hollow dots are backfilled historical filings. Click a dot to open the source.",
  });
  const rows = ev.rows.filter((r) => r.score > 0 && parseUtc(r.published_at || r.ingested_at));
  if (!rows.length) return empty(chart, "no scored items in this window");
  setSub(chart, `top ${fmt.int(rows.length)} by score · tiers: ` +
    `${fmt.int(dist.tier_counts.high || 0)} high / ${fmt.int(dist.tier_counts.medium || 0)} med / ${fmt.int(dist.tier_counts.low || 0)} low`);

  for (const r of rows) r._t = parseUtc(r.published_at || r.ingested_at);

  responsiveSvg(chart, 320, (svg, w) => {
    const m = { t: 14, r: 14, b: 24, l: 44 };
    const iw = w - m.l - m.r, ih = 320 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const x = d3.scaleUtc().domain(d3.extent(rows, (r) => r._t)).nice().range([0, iw]);
    const [lo, hi] = d3.extent(rows, (r) => r.score);
    const y = d3.scaleLog().domain([Math.max(lo * 0.9, 0.01), hi * 1.1]).range([ih, 0]);

    axes(g, {
      x, y, w: iw, h: ih, xFmt: d3.utcFormat("%b %d"),
      yFmt: (v) => fmt.s2(v), yTicks: 5,
    });

    // tier boundary rules
    for (const [tier, cut] of Object.entries(dist.tier_cuts || {})) {
      if (cut == null || cut <= y.domain()[0]) continue;
      g.append("line").attr("x1", 0).attr("x2", iw).attr("y1", y(cut)).attr("y2", y(cut))
        .attr("stroke", tier === "high" ? "#ffd43b" : "#74c0fc")
        .attr("stroke-opacity", .45).attr("stroke-dasharray", "4,4");
      g.append("text").attr("x", iw - 4).attr("y", y(cut) - 4)
        .attr("text-anchor", "end").attr("font-size", 10)
        .attr("fill", tier === "high" ? "#ffd43b" : "#74c0fc").attr("fill-opacity", .8)
        .text(`${tier} ≥ ${fmt.s2(cut)}`);
    }

    const r = d3.scaleSqrt().domain([0, hi]).range([1.5, 7]);
    g.selectAll("circle").data(rows).join("circle")
      .attr("cx", (d) => x(d._t)).attr("cy", (d) => y(d.score))
      .attr("r", (d) => r(d.score))
      .attr("fill", (d) => d.backfill ? "none" : topicColor(d.topic))
      .attr("stroke", (d) => topicColor(d.topic))
      .attr("stroke-width", (d) => d.backfill ? 1.2 : 0.5)
      .attr("fill-opacity", 0.78)
      .style("cursor", (d) => d.url ? "pointer" : "default")
      .on("mousemove", (e, d) => tip.show(ttRows(d.title, [
        ["topic", esc(topicLabel(d.topic))],
        ["source", esc(sourceLabel(d.source))],
        ["score", `${fmt.s2(d.score)} ${d.tier ? "· " + d.tier : ""}`],
        ["published", d.published_at ? fmt.dtUtc(parseUtc(d.published_at)) + " UTC" : "— (using ingest time)"],
        ["ingested", fmt.dtUtc(parseUtc(d.ingested_at)) + " UTC"],
        d.backfill ? ["provenance", "backfill"] : null,
      ]), e))
      .on("mouseleave", tip.hide)
      .on("click", (e, d) => { if (d.url) window.open(d.url, "_blank", "noopener"); });
  });
}

// ── Regime ribbon ────────────────────────────────────────────────────────────
function drawRegimeRibbon(grid, reg) {
  const chart = card(grid, {
    title: "Regime",
    span: 12,
    note: "Market cycle is a 5-state hidden-Markov posterior mode; cat load is mechanical (NHC/USGS/NIFC + FEMA tail). " +
      "The combined multiplier scales every signal score.",
  });
  if (!reg.rows.length) return empty(chart, "regime detector has not run yet");
  const rows = reg.rows.map((r) => ({ ...r, _t: parseUtc(r.as_of) }));
  setSub(chart, `detector live since ${fmt.dateUtc(rows[0]._t)} · ${rows.length} readings`);

  responsiveSvg(chart, 130, (svg, w) => {
    const m = { t: 8, r: 14, b: 22, l: 92 };
    const iw = w - m.l - m.r;
    const laneH = 26, gap = 14;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const end = new Date();
    const x = d3.scaleUtc().domain([rows[0]._t, end]).range([0, iw]);

    const lanes = [
      { name: "market cycle", key: "market_cycle", colors: CYCLE_COLORS, mult: "market_cycle_mult" },
      { name: "cat load", key: "cat_load", colors: CAT_COLORS, mult: "cat_load_mult" },
    ];
    lanes.forEach((lane, li) => {
      const y0 = li * (laneH + gap);
      g.append("text").attr("x", -10).attr("y", y0 + laneH / 2 + 4)
        .attr("text-anchor", "end").attr("font-size", 10.5).attr("fill", "var(--text-3)")
        .text(lane.name);
      rows.forEach((r, i) => {
        const next = rows[i + 1]?._t ?? end;
        g.append("rect")
          .attr("x", x(r._t)).attr("y", y0)
          .attr("width", Math.max(1, x(next) - x(r._t))).attr("height", laneH)
          .attr("rx", 3)
          .attr("fill", lane.colors[r[lane.key]] || "#555")
          .attr("fill-opacity", 0.8)
          .attr("stroke", "var(--bg)").attr("stroke-width", 1)
          .on("mousemove", (e) => tip.show(ttRows(label(r[lane.key]), [
            ["from", fmt.dtUtc(r._t) + " UTC"],
            ["multiplier", "×" + fmt.s2(r[lane.mult])],
            ["combined", "×" + fmt.s2(r.multiplier)],
            ["reading", r.source],
          ]), e))
          .on("mouseleave", tip.hide);
      });
    });
    // shared x axis below lanes
    const axisY = lanes.length * (laneH + gap) - gap + 8;
    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${axisY})`)
      .call(d3.axisBottom(x).ticks(6).tickSizeOuter(0).tickFormat(d3.utcFormat("%b %d")));
    xa.select(".domain").attr("stroke", "var(--grid-line)");
    xa.selectAll("line").attr("stroke", "var(--grid-line)");
  });
}

// ── Pickup latency by source ─────────────────────────────────────────────────
function drawLatency(grid, lat) {
  const chart = card(grid, {
    title: "Pickup latency",
    span: 6,
    note: "Hours from publisher timestamp to ingestion, per source (log scale). Bar = interquartile range, dot = median, tick = p90. Backfilled rows excluded; sources with n<5 hidden.",
  });
  if (!lat.rows.length) return empty(chart, "not enough timestamped items in window");
  setSub(chart, `${lat.rows.length} sources`);

  const rows = lat.rows;
  const H = Math.max(180, rows.length * 26 + 40);
  responsiveSvg(chart, H, (svg, w) => {
    const m = { t: 8, r: 16, b: 26, l: 110 };
    const iw = w - m.l - m.r, ih = H - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const lo = Math.max(0.02, d3.min(rows, (r) => r.p25) || 0.02);
    const hi = Math.max(1, d3.max(rows, (r) => r.p90) || 1);
    const x = d3.scaleLog().domain([lo, hi * 1.3]).range([0, iw]);
    const y = d3.scaleBand().domain(rows.map((r) => r.source)).range([0, ih]).padding(0.42);

    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(5, "~g").tickSize(-ih));
    xa.select(".domain").remove();
    xa.selectAll("line").attr("stroke", "var(--grid-line)");
    g.append("text").attr("x", iw).attr("y", ih + 24).attr("text-anchor", "end")
      .attr("font-size", 10).attr("fill", "var(--text-3)").text("hours →");

    for (const r of rows) {
      const yc = y(r.source) + y.bandwidth() / 2;
      g.append("text").attr("x", -8).attr("y", yc + 3.5).attr("text-anchor", "end")
        .attr("font-size", 11).attr("fill", "var(--text-2)").text(sourceLabel(r.source));
      g.append("line")
        .attr("x1", x(Math.max(lo, r.p25))).attr("x2", x(Math.max(lo, r.p75)))
        .attr("y1", yc).attr("y2", yc)
        .attr("stroke", "var(--accent)").attr("stroke-opacity", .45)
        .attr("stroke-width", y.bandwidth()).attr("stroke-linecap", "round");
      g.append("line")
        .attr("x1", x(Math.max(lo, r.p90))).attr("x2", x(Math.max(lo, r.p90)))
        .attr("y1", yc - y.bandwidth() / 2 - 2).attr("y2", yc + y.bandwidth() / 2 + 2)
        .attr("stroke", "var(--accent)").attr("stroke-opacity", .6);
      g.append("circle").attr("cx", x(Math.max(lo, r.p50))).attr("cy", yc).attr("r", 4)
        .attr("fill", "var(--accent)").attr("stroke", "var(--bg)").attr("stroke-width", 1.2);
      g.append("rect").attr("x", -m.l).attr("y", y(r.source) - 4)
        .attr("width", iw + m.l).attr("height", y.bandwidth() + 8).attr("fill", "transparent")
        .on("mousemove", (e) => tip.show(ttRows(sourceLabel(r.source), [
          ["median", hrs(r.p50)], ["p25 – p75", `${hrs(r.p25)} – ${hrs(r.p75)}`],
          ["p90", hrs(r.p90)], ["n", fmt.int(r.n)],
        ]), e))
        .on("mouseleave", tip.hide);
    }
  });
}

function hrs(h) {
  if (h == null) return "—";
  if (h < 1) return Math.round(h * 60) + "m";
  if (h < 48) return fmt.s1(h) + "h";
  return fmt.s1(h / 24) + "d";
}

// ── Ingest cadence punch card ────────────────────────────────────────────────
function drawCadence(grid, cad, days) {
  const chart = card(grid, {
    title: "Ingest cadence",
    span: 6,
    note: "When items arrive in the warehouse: UTC weekday × hour. The 04:00/16:00 bands are the scheduled am/pm launchd runs.",
  });
  if (!cad.rows.length) return empty(chart, "no items in window");
  const total = d3.sum(cad.rows, (r) => r.n);
  setSub(chart, `n=${fmt.int(total)} ingested`);

  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const lookup = new Map(cad.rows.map((r) => [`${r.weekday}:${r.hour}`, r.n]));
  const max = d3.max(cad.rows, (r) => r.n);

  responsiveSvg(chart, 230, (svg, w) => {
    const m = { t: 6, r: 8, b: 26, l: 36 };
    const iw = w - m.l - m.r, ih = 230 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const cw = iw / 24, ch = ih / 7;
    const color = d3.scalePow().exponent(0.45).domain([0, max])
      .range(["#11161e", "#5eb1ef"]).interpolate(d3.interpolateRgb);

    for (let d = 0; d < 7; d++) {
      g.append("text").attr("x", -8).attr("y", d * ch + ch / 2 + 3.5)
        .attr("text-anchor", "end").attr("font-size", 10).attr("fill", "var(--text-3)")
        .text(DAYS[d]);
      for (let h = 0; h < 24; h++) {
        const n = lookup.get(`${d}:${h}`) || 0;
        g.append("rect")
          .attr("x", h * cw + 1).attr("y", d * ch + 1)
          .attr("width", Math.max(0, cw - 2)).attr("height", Math.max(0, ch - 2))
          .attr("rx", 2.5).attr("fill", color(n))
          .on("mousemove", (e) => tip.show(ttRows(null, [
            [`${DAYS[d]} ${String(h).padStart(2, "0")}:00 UTC`, fmt.int(n) + " items"],
          ]), e))
          .on("mouseleave", tip.hide);
      }
    }
    for (let h = 0; h <= 24; h += 6) {
      g.append("text").attr("x", Math.min(h * cw, iw)).attr("y", ih + 16)
        .attr("text-anchor", h === 0 ? "start" : h === 24 ? "end" : "middle")
        .attr("font-size", 10).attr("fill", "var(--text-3)")
        .text(String(h).padStart(2, "0") + ":00");
    }
  });
}
