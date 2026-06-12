// Operations — pipeline health: ingest activity, funnel, summarizer latency,
// outcome corroboration, recent errors.

import { get } from "../api.js";
import { sourceLabel, topicLabel, topicColor, fmt, parseUtc, tip, ttRows, esc } from "../theme.js";
import { card, el, empty, responsiveSvg, axes, setSub } from "../components.js";

export async function render(root, store) {
  const days = store.range || 9999;
  const [runs, funnel, summ, outc] = await Promise.all([
    get("ops-runs", { days }),
    get("ops-funnel", { days }),
    get("ops-summarizer", { days }),
    get("outcomes"),
  ]);

  const grid = el("div", { class: "grid" });
  root.appendChild(grid);

  drawActivityMatrix(grid, runs);
  drawFunnel(grid, funnel);
  drawSummarizer(grid, summ);
  drawOutcomes(grid, outc);
  drawErrors(grid, runs);
}

// ── Activity matrix: source × day ────────────────────────────────────────────
function drawActivityMatrix(grid, runs) {
  const chart = card(grid, {
    title: "Ingest activity",
    span: 12,
    note: "New items per source per UTC day. Red ring = at least one failed run that day. Intensity is per-source-normalized so quiet sources stay readable.",
  });
  if (!runs.rows.length) return empty(chart, "no runs in window");

  const days = Array.from(new Set(runs.rows.map((r) => r.day))).sort();
  const totals = d3.rollup(runs.rows, (v) => d3.sum(v, (r) => r.items_new), (r) => r.source);
  const sources = Array.from(totals.keys()).sort((a, b) => totals.get(b) - totals.get(a));
  const lookup = d3.index(runs.rows, (r) => r.source, (r) => r.day);
  setSub(chart, `${sources.length} sources × ${days.length} days · ${fmt.int(d3.sum(runs.rows, (r) => r.items_new))} new items`);

  const H = sources.length * 22 + 40;
  responsiveSvg(chart, H, (svg, w) => {
    const m = { t: 4, r: 46, b: 24, l: 120 };
    const iw = w - m.l - m.r, ih = sources.length * 22;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const cw = iw / days.length;

    sources.forEach((s, i) => {
      const maxRow = d3.max(days, (d) => lookup.get(s)?.get(d)?.items_new ?? 0) || 1;
      const color = d3.scalePow().exponent(0.5).domain([0, maxRow])
        .range(["#10161f", "#5eb1ef"]).interpolate(d3.interpolateRgb);
      g.append("text").attr("x", -8).attr("y", i * 22 + 14)
        .attr("text-anchor", "end").attr("font-size", 10.5).attr("fill", "var(--text-2)")
        .text(sourceLabel(s));
      g.append("text").attr("x", iw + 8).attr("y", i * 22 + 14)
        .attr("font-size", 10).attr("fill", "var(--text-3)").attr("class", "mono")
        .text(fmt.int(totals.get(s)));
      days.forEach((d, j) => {
        const cell = lookup.get(s)?.get(d);
        const n = cell?.items_new ?? 0;
        g.append("rect")
          .attr("x", j * cw + 1).attr("y", i * 22 + 1)
          .attr("width", Math.max(0.5, cw - 2)).attr("height", 18).attr("rx", 3)
          .attr("fill", cell ? color(n) : "transparent")
          .attr("stroke", cell?.failures ? "#ff6b6b" : "var(--hairline-2)")
          .attr("stroke-width", cell?.failures ? 1.2 : 0.4)
          .on("mousemove", (e) => tip.show(ttRows(`${sourceLabel(s)} · ${d}`, [
            ["new items", fmt.int(n)],
            ["fetched", fmt.int(cell?.items_fetched ?? 0)],
            ["runs", fmt.int(cell?.runs ?? 0)],
            cell?.failures ? ["failures", fmt.int(cell.failures)] : null,
          ]), e))
          .on("mouseleave", tip.hide);
      });
    });
    // x labels: ~8 evenly spaced days
    const step = Math.max(1, Math.round(days.length / 8));
    days.forEach((d, j) => {
      if (j % step !== 0) return;
      g.append("text").attr("x", j * cw + cw / 2).attr("y", ih + 16)
        .attr("text-anchor", "middle").attr("font-size", 9.5).attr("fill", "var(--text-3)")
        .text(d.slice(5));
    });
  });
}

// ── Funnel: ingested → kept → summarized per day ─────────────────────────────
function drawFunnel(grid, funnel) {
  const chart = card(grid, {
    title: "Triage funnel",
    span: 6,
    note: "Cohorts by UTC ingest day: every bar is the items that arrived that day; the brighter layers survived triage and reached the summarizer.",
  });
  const rows = funnel.rows.map((r) => ({ ...r, date: parseUtc(r.day) })).filter((r) => r.date);
  if (!rows.length) return empty(chart, "no items in window");
  const tot = d3.sum(rows, (r) => r.ingested);
  setSub(chart, `${fmt.int(tot)} ingested · ${fmt.pct0(d3.sum(rows, (r) => r.kept) / tot)} kept · ${fmt.pct0(d3.sum(rows, (r) => r.summarized) / tot)} summarized`);

  responsiveSvg(chart, 240, (svg, w) => {
    const m = { t: 10, r: 10, b: 24, l: 40 };
    const iw = w - m.l - m.r, ih = 240 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const x = d3.scaleBand().domain(rows.map((r) => r.day)).range([0, iw]).padding(0.25);
    const y = d3.scaleLinear().domain([0, d3.max(rows, (r) => r.ingested)]).nice().range([ih, 0]);
    const ya = g.append("g").attr("class", "axis").call(d3.axisLeft(y).ticks(5).tickSize(-iw));
    ya.select(".domain").remove();
    ya.selectAll("line").attr("stroke", "var(--grid-line)");

    const layers = [
      ["ingested", "rgba(151,161,178,.28)"],
      ["kept", "rgba(94,177,239,.65)"],
      ["summarized", "rgba(81,207,102,.8)"],
    ];
    for (const r of rows) {
      for (const [key, color] of layers) {
        g.append("rect")
          .attr("x", x(r.day)).attr("width", x.bandwidth())
          .attr("y", y(r[key])).attr("height", ih - y(r[key]))
          .attr("rx", 2).attr("fill", color);
      }
      g.append("rect").attr("x", x(r.day)).attr("y", 0).attr("width", x.bandwidth()).attr("height", ih)
        .attr("fill", "transparent")
        .on("mousemove", (e) => tip.show(ttRows(r.day, [
          ["ingested", fmt.int(r.ingested)],
          ["kept", `${fmt.int(r.kept)} (${r.ingested ? fmt.pct0(r.kept / r.ingested) : "—"})`],
          ["summarized", fmt.int(r.summarized)],
        ]), e))
        .on("mouseleave", tip.hide);
    }
    const step = Math.max(1, Math.round(rows.length / 7));
    rows.forEach((r, i) => {
      if (i % step !== 0) return;
      g.append("text").attr("x", x(r.day) + x.bandwidth() / 2).attr("y", ih + 16)
        .attr("text-anchor", "middle").attr("font-size", 9.5).attr("fill", "var(--text-3)")
        .text(r.day.slice(5));
    });
  });
}

// ── Summarizer latency ───────────────────────────────────────────────────────
function drawSummarizer(grid, summ) {
  const chart = card(grid, {
    title: "Summarizer latency",
    span: 6,
    note: `Per-item MLX wall time on successful summaries. Line = daily median, band = median→p90. Backend: ${esc(summ.backends.join(", ") || "—")}.`,
  });
  const rows = summ.rows.map((r) => ({ ...r, date: parseUtc(r.day) })).filter((r) => r.date);
  if (!rows.length) return empty(chart, "no summarizer runs in window");
  setSub(chart, `${fmt.int(d3.sum(rows, (r) => r.n))} summaries`);

  responsiveSvg(chart, 240, (svg, w) => {
    const m = { t: 10, r: 14, b: 24, l: 44 };
    const iw = w - m.l - m.r, ih = 240 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const x = d3.scaleUtc().domain(d3.extent(rows, (r) => r.date)).range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(rows, (r) => r.p90) / 1000]).nice().range([ih, 0]);
    axes(g, { x, y, w: iw, h: ih, xFmt: d3.utcFormat("%b %d"), yFmt: (v) => v + "s" });

    g.append("path")
      .attr("d", d3.area().x((r) => x(r.date)).y0((r) => y(r.p50 / 1000)).y1((r) => y(r.p90 / 1000))
        .curve(d3.curveMonotoneX)(rows))
      .attr("fill", "rgba(94,177,239,.18)");
    g.append("path")
      .attr("d", d3.line().x((r) => x(r.date)).y((r) => y(r.p50 / 1000)).curve(d3.curveMonotoneX)(rows))
      .attr("fill", "none").attr("stroke", "var(--accent)").attr("stroke-width", 1.8);
    g.selectAll("circle").data(rows).join("circle")
      .attr("cx", (r) => x(r.date)).attr("cy", (r) => y(r.p50 / 1000)).attr("r", 2.6)
      .attr("fill", "var(--accent)")
      .on("mousemove", (e, r) => tip.show(ttRows(r.day, [
        ["median", fmt.s1(r.p50 / 1000) + "s"],
        ["p90", fmt.s1(r.p90 / 1000) + "s"],
        ["summaries", fmt.int(r.n)],
      ]), e))
      .on("mouseleave", tip.hide);
  });
}

// ── Outcome corroboration ────────────────────────────────────────────────────
function drawOutcomes(grid, outc) {
  const chart = card(grid, {
    title: "Outcome corroboration",
    span: 12,
    note: "Share of kept items whose signal was later corroborated (follow-on coverage, EDGAR filing, regime shift, or a Benjamini–Hochberg-significant benchmark-excess stock move). " +
      "These labels train the calibrator and the learned model. All-time, all cohorts (live + backfill).",
  });
  if (!outc.by_horizon.length) return empty(chart, "no outcome labels yet — they accrue as items mature");

  const wrap = el("div");
  wrap.style.cssText = "display:grid;grid-template-columns:170px 1fr 1fr;gap:18px;align-items:start";
  chart.appendChild(wrap);

  // headline rates
  const head = el("div");
  for (const h of outc.by_horizon) {
    const rate = h.n ? h.corroborated / h.n : 0;
    head.appendChild(el("div", { style: "margin-bottom:12px" },
      `<div class="kpi-label">${h.horizon_days}-day</div>
       <div class="kpi-value">${fmt.pct0(rate)}</div>
       <div class="kpi-sub">${fmt.int(h.corroborated)} of ${fmt.int(h.n)}</div>`));
  }
  wrap.appendChild(head);

  rateBars(wrap, "by source (7d)", outc.by_source.filter((r) => r.horizon_days === 7),
    (r) => sourceLabel(r.source), () => "var(--accent)");
  rateBars(wrap, "by topic (7d)", outc.by_topic.filter((r) => r.horizon_days === 7),
    (r) => topicLabel(r.topic), (r) => topicColor(r.topic));
}

function rateBars(wrap, title, rows, labelFn, colorFn) {
  const box = el("div");
  box.innerHTML = `<div class="kpi-label" style="margin-bottom:6px">${esc(title)} <span class="t3" style="text-transform:none;letter-spacing:0">· n≥10</span></div>`;
  wrap.appendChild(box);
  if (!rows.length) {
    box.appendChild(el("div", { class: "t3", style: "font-size:11.5px" }, "not enough labels"));
    return;
  }
  rows = rows.map((r) => ({ ...r, rate: r.corroborated / r.n }))
    .sort((a, b) => b.rate - a.rate).slice(0, 10);
  const holder = el("div");
  box.appendChild(holder);
  const H = rows.length * 23 + 6;
  responsiveSvg(holder, H, (svg, w) => {
    const m = { l: 118, r: 76 };
    const iw = Math.max(60, w - m.l - m.r);
    const x = d3.scaleLinear().domain([0, 1]).range([0, iw]);
    rows.forEach((r, i) => {
      const yc = i * 23 + 12;
      svg.append("text").attr("x", m.l - 8).attr("y", yc + 3.5).attr("text-anchor", "end")
        .attr("font-size", 10.5).attr("fill", "var(--text-2)").text(labelFn(r));
      svg.append("rect").attr("x", m.l).attr("y", yc - 5).attr("width", iw).attr("height", 10)
        .attr("rx", 5).attr("fill", "rgba(255,255,255,.05)");
      svg.append("rect").attr("x", m.l).attr("y", yc - 5)
        .attr("width", Math.max(2, x(r.rate))).attr("height", 10).attr("rx", 5)
        .attr("fill", colorFn(r)).attr("fill-opacity", .8)
        .on("mousemove", (e) => tip.show(ttRows(labelFn(r), [
          ["corroborated", `${fmt.int(r.corroborated)} of ${fmt.int(r.n)}`],
          ["rate", fmt.pct1(r.rate)],
        ]), e))
        .on("mouseleave", tip.hide);
      svg.append("text").attr("x", m.l + iw + 8).attr("y", yc + 3.5)
        .attr("font-size", 10).attr("fill", "var(--text-3)").attr("class", "mono")
        .text(`${fmt.pct0(r.rate)} · ${fmt.int(r.n)}`);
    });
  });
}

// ── Recent errors ────────────────────────────────────────────────────────────
function drawErrors(grid, runs) {
  const chart = card(grid, {
    title: "Recent failures",
    span: 12,
  });
  if (!runs.errors.length) {
    return empty(chart, "no failed runs in window — pipeline healthy", "✓");
  }
  setSub(chart, `${runs.errors.length} most recent`);
  const table = el("table", { class: "data" });
  table.innerHTML = `<thead><tr><th>When (UTC)</th><th>Source</th><th>Run</th><th>Status</th><th>Error</th></tr></thead>`;
  const tb = el("tbody");
  for (const r of runs.errors) {
    const tr = el("tr");
    tr.innerHTML = `<td class="mono t2">${esc(r.run_at)}</td>
      <td>${esc(sourceLabel(r.source))}</td>
      <td class="t3">${esc(r.run_type)}</td>
      <td class="neg">${esc(r.status)}</td>
      <td class="t2" style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(r.error || "")}">${esc((r.error || "—").slice(0, 160))}</td>`;
    tb.appendChild(tr);
  }
  table.appendChild(tb);
  chart.appendChild(table);
}
