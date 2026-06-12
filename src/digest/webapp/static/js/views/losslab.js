// Loss Lab — actuarial views: loss-triangle explorer, development curves,
// frequency/severity trends, reserving signals, and the severity tape.

import { get } from "../api.js";
import { fmt, tip, ttRows, esc } from "../theme.js";
import { card, el, empty, responsiveSvg, select, setSub } from "../components.js";

// LOB names arrive as long XBRL member slugs; canonical_lob is the curated
// family. Label = canonical, disambiguated by the trimmed raw slug.
const prettyLob = (s) => (s || "").replace(/_/g, " ").trim();
function lobLabel(raw, canonical) {
  const canon = prettyLob(canonical);
  let detail = prettyLob(raw)
    .replace(/\b(segment|property and casualty|insurance product line|product line|insurance)\b/g, " ")
    .replace(/\s+/g, " ").trim();
  if (!canon || canon === detail) return detail || canon || raw;
  if (!detail || canon.includes(detail)) return canon;
  return `${canon} · ${detail.length > 42 ? detail.slice(0, 42) + "…" : detail}`;
}

export async function render(root) {
  const [catalog, fsIns, resv, sev] = await Promise.all([
    get("triangle-catalog"),
    get("freq-sev-insurers"),
    get("reserving"),
    get("severity"),
  ]);

  const grid = el("div", { class: "grid" });
  root.appendChild(grid);

  await drawTriangleExplorer(grid, catalog);
  await drawFreqSev(grid, fsIns);
  drawReserving(grid, resv);
  drawSeverityTape(grid, sev);
}

// ── Triangle explorer ────────────────────────────────────────────────────────
async function drawTriangleExplorer(grid, catalog) {
  const heat = card(grid, {
    title: "Loss triangle",
    span: 7,
    note: "Cumulative losses ($M) by accident year × development age (months), from 10-K XBRL disclosures. Each row matures left to right; the diagonal is the latest evaluation.",
  });
  const dev = card(grid, {
    title: "Development curves",
    span: 5,
    note: "The same triangle, read as curves: one line per accident year. Recent years are brighter — and shorter, since they have had less time to develop.",
  });
  if (!catalog.rows.length) {
    empty(heat, "no triangles ingested yet");
    empty(dev, "");
    return;
  }

  // group catalog: insurer → lobs (with metrics available per lob)
  const byInsurer = d3.group(catalog.rows, (r) => r.insurer);
  const insurers = Array.from(byInsurer.keys());
  // order lines by book size (peak cumulative loss), so the marquee line leads
  const biggest = (ins) => {
    const lobs = d3.group(byInsurer.get(ins), (r) => r.lob);
    return Array.from(lobs.entries()).sort((a, b) =>
      d3.max(b[1], (r) => r.peak_value ?? 0) - d3.max(a[1], (r) => r.peak_value ?? 0));
  };

  const state = { insurer: insurers.includes("PGR") ? "PGR" : insurers[0], lob: null, metric: null };

  const controls = el("div", { class: "controls" });
  heat.closest(".card").insertBefore(controls, heat);
  let lobSel, metricSel;

  const rebuildSelectors = () => {
    controls.innerHTML = "";
    select(controls, {
      label: "Insurer", value: state.insurer,
      options: insurers.map((i) => ({ value: i, label: i })),
      onChange: (v) => { state.insurer = v; state.lob = null; state.metric = null; rebuildSelectors(); },
    });
    const lobs = biggest(state.insurer);
    if (!state.lob || !lobs.some(([l]) => l === state.lob)) state.lob = lobs[0][0];
    select(controls, {
      label: "Line", value: state.lob,
      options: lobs.map(([l, rows]) => ({
        value: l, label: lobLabel(l, rows[0].canonical_lob),
      })),
      onChange: (v) => { state.lob = v; state.metric = null; rebuildSelectors(); },
    });
    const metrics = byInsurer.get(state.insurer).filter((r) => r.lob === state.lob).map((r) => r.metric);
    if (!state.metric || !metrics.includes(state.metric))
      state.metric = metrics.includes("incurred") ? "incurred" : metrics[0];
    select(controls, {
      label: "Metric", value: state.metric,
      options: metrics.map((m) => ({ value: m, label: m })),
      onChange: (v) => { state.metric = v; rebuildSelectors(); },
    });
    load();
  };

  const load = async () => {
    const tri = await get("triangle", { insurer: state.insurer, lob: state.lob, metric: state.metric });
    heat.innerHTML = "";
    dev.innerHTML = "";
    if (!tri.cells.length) { empty(heat, "no cells for this selection"); empty(dev, ""); return; }
    setSub(heat, `${tri.insurer} · as of ${esc(tri.as_of?.slice(0, 10) ?? "?")} · ${tri.cells.length} cells`);
    setSub(dev, `${tri.cells.length ? d3.max(tri.cells, (c) => c.accident_year) - d3.min(tri.cells, (c) => c.accident_year) + 1 : 0} accident years`);
    drawHeatmap(heat, tri);
    drawDevCurves(dev, tri);
  };

  rebuildSelectors();
}

function drawHeatmap(container, tri) {
  const years = Array.from(new Set(tri.cells.map((c) => c.accident_year))).sort();
  const devs = Array.from(new Set(tri.cells.map((c) => c.dev_period))).sort((a, b) => a - b);
  const lookup = new Map(tri.cells.map((c) => [`${c.accident_year}:${c.dev_period}`, c.cumulative_value]));
  const max = d3.max(tri.cells, (c) => c.cumulative_value);

  const H = years.length * 30 + 46;
  responsiveSvg(container, H, (svg, w) => {
    const m = { t: 22, r: 8, b: 8, l: 46 };
    const iw = w - m.l - m.r;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const cw = iw / devs.length, ch = 28;
    const color = d3.scalePow().exponent(0.5).domain([0, max])
      .range(["#10161f", "#3d6f9e"]).interpolate(d3.interpolateRgb);

    devs.forEach((d, j) => {
      g.append("text").attr("x", j * cw + cw / 2).attr("y", -8)
        .attr("text-anchor", "middle").attr("font-size", 10).attr("fill", "var(--text-3)")
        .text(d + "m");
    });
    years.forEach((ay, i) => {
      g.append("text").attr("x", -8).attr("y", i * ch + ch / 2 + 3.5)
        .attr("text-anchor", "end").attr("font-size", 10.5)
        .attr("fill", "var(--text-2)").attr("class", "mono").text(ay);
      devs.forEach((d, j) => {
        const v = lookup.get(`${ay}:${d}`);
        if (v == null) return;
        g.append("rect")
          .attr("x", j * cw + 1).attr("y", i * ch + 1)
          .attr("width", cw - 2).attr("height", ch - 2).attr("rx", 3)
          .attr("fill", color(v))
          .on("mousemove", (e) => tip.show(ttRows(`AY ${ay} · ${d} months`, [
            ["cumulative", fmt.musd(v)],
          ]), e))
          .on("mouseleave", tip.hide);
        if (cw > 48) {
          g.append("text")
            .attr("x", j * cw + cw / 2).attr("y", i * ch + ch / 2 + 3.5)
            .attr("text-anchor", "middle").attr("font-size", 9.5)
            .attr("fill", "rgba(230,235,242,.85)").attr("class", "mono")
            .style("pointer-events", "none")
            .text(fmt.si(Math.round(v)));
        }
      });
    });
  });
}

function drawDevCurves(container, tri) {
  const years = Array.from(new Set(tri.cells.map((c) => c.accident_year))).sort();
  const series = years.map((ay) => ({
    ay,
    pts: tri.cells.filter((c) => c.accident_year === ay)
      .sort((a, b) => a.dev_period - b.dev_period),
  }));
  const H = 320;
  responsiveSvg(container, H, (svg, w) => {
    const m = { t: 12, r: 44, b: 26, l: 50 };
    const iw = w - m.l - m.r, ih = H - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const x = d3.scaleLinear().domain(d3.extent(tri.cells, (c) => c.dev_period)).range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(tri.cells, (c) => c.cumulative_value)]).nice()
      .range([ih, 0]);

    const ya = g.append("g").attr("class", "axis")
      .call(d3.axisLeft(y).ticks(5).tickSize(-iw).tickFormat(fmt.si));
    ya.select(".domain").remove();
    ya.selectAll("line").attr("stroke", "var(--grid-line)");
    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).tickValues(Array.from(new Set(tri.cells.map((c) => c.dev_period))))
        .tickFormat(d3.format("d")));
    xa.select(".domain").attr("stroke", "var(--grid-line)");
    g.append("text").attr("x", iw / 2).attr("y", ih + 24).attr("text-anchor", "middle")
      .attr("font-size", 10).attr("fill", "var(--text-3)").text("development age (months)");

    const ramp = d3.scaleLinear().domain([0, Math.max(1, years.length - 1)])
      .range(["#3a4a5e", "#5eb1ef"]).interpolate(d3.interpolateRgb);
    const lineGen = d3.line().x((c) => x(c.dev_period)).y((c) => y(c.cumulative_value));

    series.forEach((s, i) => {
      const c = ramp(i);
      g.append("path").attr("d", lineGen(s.pts))
        .attr("fill", "none").attr("stroke", c).attr("stroke-width", 1.6).attr("stroke-opacity", .9);
      const last = s.pts[s.pts.length - 1];
      g.append("circle").attr("cx", x(last.dev_period)).attr("cy", y(last.cumulative_value))
        .attr("r", 2.6).attr("fill", c)
        .on("mousemove", (e) => tip.show(ttRows(`AY ${s.ay}`, [
          ["latest age", last.dev_period + " months"],
          ["cumulative", fmt.musd(last.cumulative_value)],
        ]), e))
        .on("mouseleave", tip.hide);
      if (i === years.length - 1 || i === 0) {
        g.append("text").attr("x", x(last.dev_period) + 6).attr("y", y(last.cumulative_value) + 3.5)
          .attr("font-size", 10).attr("fill", c).attr("class", "mono").text(s.ay);
      }
    });
  });
}

// ── Frequency / severity / pure premium ─────────────────────────────────────
async function drawFreqSev(grid, fsIns) {
  const chart = card(grid, {
    title: "Frequency × severity",
    span: 12,
    note: "Derived from XBRL claim counts and incurred losses: severity = incurred ÷ claims; frequency proxy = claims per $M earned premium (segment grain); " +
      "pure-premium ratio = their product against premium. GAAP data — true exposure is not disclosed, so frequency is a premium-based proxy.",
  });
  if (!fsIns.rows.length) return empty(chart, "no freq/sev signals yet — run `digest pure-premium`");

  const state = { insurer: fsIns.rows[0].insurer, lob: null };
  const controls = el("div", { class: "controls" });
  chart.closest(".card").insertBefore(controls, chart);

  const rebuild = async () => {
    const data = await get("freq-sev", { insurer: state.insurer });
    const lobs = Array.from(d3.group(data.rows, (r) => r.lob).entries())
      .sort((a, b) => b[1].length - a[1].length);
    if (!state.lob || !lobs.some(([l]) => l === state.lob)) state.lob = lobs[0]?.[0];

    controls.innerHTML = "";
    select(controls, {
      label: "Insurer", value: state.insurer,
      options: fsIns.rows.map((r) => ({ value: r.insurer, label: `${r.insurer} (${r.n})` })),
      onChange: (v) => { state.insurer = v; state.lob = null; rebuild(); },
    });
    select(controls, {
      label: "Line", value: state.lob,
      options: lobs.map(([l, rows]) => ({ value: l, label: `${l.replace(/_/g, " ")} (${rows.length})` })),
      onChange: (v) => { state.lob = v; rebuild(); },
    });

    const rows = data.rows.filter((r) => r.lob === state.lob)
      .sort((a, b) => a.accident_year - b.accident_year);
    chart.innerHTML = "";
    setSub(chart, `${state.insurer} · ${esc(state.lob)} · AY ${rows[0]?.accident_year}–${rows[rows.length - 1]?.accident_year}`);

    const wrap = el("div");
    wrap.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px";
    chart.appendChild(wrap);

    miniTrend(wrap, "Severity ($/claim)", rows, (r) => r.severity_usd, fmt.usd, "#f783ac");
    miniTrend(wrap, "Frequency (claims/$M EP)", rows, (r) => r.frequency_per_musd,
      (v) => fmt.s2(v), "#4dabf7");
    miniTrend(wrap, "Pure premium ratio", rows, (r) => r.pure_premium_ratio,
      (v) => fmt.pct1(v), "#ffd43b");
  };
  await rebuild();
}

function miniTrend(wrap, title, rows, accessor, format, color) {
  const pts = rows.map((r) => ({ ay: r.accident_year, v: accessor(r) })).filter((p) => p.v != null);
  const box = el("div");
  box.innerHTML = `<div class="kpi-label" style="margin-bottom:4px">${esc(title)}</div>`;
  wrap.appendChild(box);
  if (pts.length < 2) {
    box.appendChild(el("div", { class: "empty", style: "min-height:80px" },
      `<div><span class="glyph">◌</span>n=${pts.length} — not enough accident years</div>`));
    return;
  }
  const holder = el("div");
  box.appendChild(holder);
  responsiveSvg(holder, 150, (svg, w) => {
    const m = { t: 8, r: 10, b: 20, l: 8 };
    const iw = w - m.l - m.r, ih = 150 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const x = d3.scalePoint().domain(pts.map((p) => p.ay)).range([0, iw]).padding(0.4);
    const ext = d3.extent(pts, (p) => p.v);
    const pad = (ext[1] - ext[0]) * 0.15 || ext[1] * 0.1 || 1;
    const y = d3.scaleLinear().domain([ext[0] - pad, ext[1] + pad]).range([ih, 0]);

    g.append("path")
      .attr("d", d3.line().x((p) => x(p.ay)).y((p) => y(p.v)).curve(d3.curveMonotoneX)(pts))
      .attr("fill", "none").attr("stroke", color).attr("stroke-width", 1.8);
    g.selectAll("circle").data(pts).join("circle")
      .attr("cx", (p) => x(p.ay)).attr("cy", (p) => y(p.v)).attr("r", 3)
      .attr("fill", color).attr("stroke", "var(--bg-card)").attr("stroke-width", 1.4)
      .on("mousemove", (e, p) => tip.show(ttRows(`AY ${p.ay}`, [[title, format(p.v)]]), e))
      .on("mouseleave", tip.hide);
    const ticks = pts.length > 6 ? pts.filter((_, i) => i % 2 === 0) : pts;
    for (const p of ticks) {
      g.append("text").attr("x", x(p.ay)).attr("y", ih + 14).attr("text-anchor", "middle")
        .attr("font-size", 9.5).attr("fill", "var(--text-3)").attr("class", "mono")
        .text(String(p.ay).slice(2));
    }
  });
}

// ── Reserving signals ────────────────────────────────────────────────────────
function drawReserving(grid, resv) {
  const chart = card(grid, {
    title: "Reserve development",
    span: 7,
    note: "Chain-ladder IBNR change vs the prior evaluation, per insurer × line (latest as-of, incurred where available). " +
      "Red = adverse (IBNR grew), green = favorable. This is the feed behind the leaderboard's reserve_deterioration_boost.",
  });
  const eligible = resv.rows
    .filter((r) => r.deterioration_pct != null)
    .filter((r, _, all) => r.metric === "incurred" || !all.some(
      (o) => o.insurer === r.insurer && o.lob === r.lob && o.metric === "incurred"));
  const rows = eligible
    .sort((a, b) => Math.abs(b.deterioration_pct) - Math.abs(a.deterioration_pct))
    .slice(0, 18)
    .sort((a, b) => b.deterioration_pct - a.deterioration_pct);
  if (!rows.length) return empty(chart, "no reserving signals yet — run `digest reserving`");
  setSub(chart, `${eligible.length} of ${resv.n} signals have a prior evaluation to compare against`);

  const H = rows.length * 25 + 40;
  responsiveSvg(chart, H, (svg, w) => {
    const m = { t: 6, r: 56, b: 24, l: 210 };
    const iw = w - m.l - m.r, ih = rows.length * 25;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const ext = d3.max(rows, (r) => Math.abs(r.deterioration_pct));
    const x = d3.scaleLinear().domain([-ext, ext]).range([0, iw]).nice();
    const y = d3.scaleBand().domain(rows.map((r, i) => i)).range([0, ih]).padding(0.3);

    g.append("line").attr("x1", x(0)).attr("x2", x(0)).attr("y1", 0).attr("y2", ih)
      .attr("stroke", "rgba(255,255,255,.22)");
    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(6).tickFormat(fmt.pct0).tickSizeOuter(0));
    xa.select(".domain").attr("stroke", "var(--grid-line)");

    rows.forEach((r, i) => {
      const yc = y(i) + y.bandwidth() / 2;
      const v = r.deterioration_pct;
      const color = v > 0.005 ? "#ff6b6b" : v < -0.005 ? "#51cf66" : "#8c9bab";
      g.append("text").attr("x", -10).attr("y", yc + 3.5).attr("text-anchor", "end")
        .attr("font-size", 11).attr("fill", "var(--text-2)")
        .text(`${r.insurer} · ${r.lob.replace(/_/g, " ").slice(0, 24)}`);
      g.append("line").attr("x1", x(0)).attr("x2", x(v)).attr("y1", yc).attr("y2", yc)
        .attr("stroke", color).attr("stroke-width", 2);
      g.append("circle").attr("cx", x(v)).attr("cy", yc).attr("r", 4.5)
        .attr("fill", color).attr("stroke", "var(--bg-card)").attr("stroke-width", 1.2)
        .on("mousemove", (e) => tip.show(ttRows(`${r.insurer} · ${r.lob.replace(/_/g, " ")}`, [
          ["direction", r.direction],
          ["Δ IBNR", fmt.signedPct1(v)],
          ["IBNR", fmt.musd(r.ibnr)],
          ["prior IBNR", fmt.musd(r.prior_ibnr)],
          ["ultimate", fmt.musd(r.ultimate)],
          ["metric · as of", `${r.metric} · ${r.as_of.slice(0, 10)}`],
        ]), e))
        .on("mouseleave", tip.hide);
      g.append("text").attr("x", x(v) + (v >= 0 ? 9 : -9)).attr("y", yc + 3.5)
        .attr("text-anchor", v >= 0 ? "start" : "end")
        .attr("font-size", 10).attr("fill", color).attr("class", "mono")
        .text(fmt.signedPct1(v));
    });
  });
}

// ── Severity tape ────────────────────────────────────────────────────────────
function drawSeverityTape(grid, sev) {
  const chart = card(grid, {
    title: "Severity tape",
    span: 5,
    note: "FRED loss-cost components and the loss-cost-weighted blended composite. z is the rolling 12-month standard score; ⚑ marks a ±1.5σ anomaly.",
  });
  if (!sev.rows.length) return empty(chart, "severity tape is empty — run `digest severity-tape`");

  const obs = Array.from(new Set(sev.rows.map((r) => r.observation_date)));
  setSub(chart, obs.length === 1
    ? `1 observation (${esc(obs[0])}) — history accrues monthly`
    : `${sev.rows.length} observations`);

  const NAMES = {
    blended_severity: "Blended composite",
    fred_CUSR0000SETA02: "Used vehicles (CPI)",
    fred_CUSR0000SETA01: "New vehicles (CPI)",
    fred_CUSR0000SETD: "Vehicle parts (CPI)",
    fred_CUSR0000SETC: "Motor vehicle repair (CPI)",
    fred_CUSR0000SAM2: "Medical services (CPI)",
    fred_CUSR0000SAH1: "Shelter (CPI)",
    fred_PCU33633363: "Auto parts mfg (PPI)",
  };
  const latest = new Map();
  for (const r of sev.rows) latest.set(r.index_name, r); // rows ordered by date

  const table = el("table", { class: "data" });
  table.innerHTML = `<thead><tr><th>Series</th><th>Category</th><th class="num">Index</th><th class="num">z (12m)</th><th></th></tr></thead>`;
  const tb = el("tbody");
  const rows = Array.from(latest.values())
    .sort((a, b) => (a.index_name === "blended_severity" ? -1 : 1) - (b.index_name === "blended_severity" ? -1 : 1)
      || Math.abs(b.zscore_12m ?? 0) - Math.abs(a.zscore_12m ?? 0));
  for (const r of rows) {
    const tr = el("tr");
    const z = r.zscore_12m;
    tr.innerHTML = `
      <td${r.index_name === "blended_severity" ? ' style="font-weight:650"' : ""}>${esc(NAMES[r.index_name] || r.index_name)}</td>
      <td class="t3">${esc(r.category || "—")}</td>
      <td class="num">${r.value != null ? fmt.s1(r.value) : "—"}</td>
      <td class="num ${z > 0.5 ? "neg" : z < -0.5 ? "pos" : "t2"}">${z != null ? fmt.s2(z) : "—"}</td>
      <td>${r.is_anomaly ? '<span class="badge high">⚑</span>' : ""}</td>`;
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  chart.appendChild(table);
}
