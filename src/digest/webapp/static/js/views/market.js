// Market — insurer prices indexed to window start, EDGAR filing markers,
// excess-return small multiples, and the alpha engine's (honest) forecasts.

import { get } from "../api.js";
import { fmt, parseUtc, tip, ttRows, esc } from "../theme.js";
import { card, el, empty, responsiveSvg, axes, setSub } from "../components.js";

// Stable hand-assigned hues — the common default trios stay distinguishable.
const TICKER_COLORS = {
  PGR: "#4dabf7", ALL: "#ffd43b", TRV: "#ff8787", CB: "#69db7c", HIG: "#da77f2",
  AIG: "#3bc9db", MET: "#ffa94d", PRU: "#9775fa", BRK: "#38d9a9", RNR: "#f783ac",
  EG: "#a9e34b", AXS: "#748ffc", AON: "#e8935a", WTW: "#74c0fc", MMC: "#b08968",
};
const FALLBACK_COLORS = ["#4dabf7", "#ffd43b", "#ff8787", "#69db7c", "#da77f2"];
const BENCH_COLOR = "#97a1b2";
const FORM_GLYPH = { "8-K": "●", "10-Q": "◆", "10-K": "■", "13F-HR": "▲" };

const DEFAULT_TICKERS = ["PGR", "ALL", "TRV", "IAK"];

export async function render(root, store) {
  const days = store.range || 3650;
  const [px, evts, fc] = await Promise.all([
    get("prices", { days }),
    get("price-events", { days }),
    get("forecasts"),
  ]);

  const grid = el("div", { class: "grid" });
  root.appendChild(grid);

  if (!px.series.length) {
    card(grid, { title: "Prices", span: 12 });
    return empty(grid.querySelector(".chart"), "price store is empty — run `digest forecast prices`");
  }

  const state = { selected: new Set(DEFAULT_TICKERS.filter(
    (t) => px.series.some((s) => s.ticker === t))) };
  if (!state.selected.size) state.selected = new Set(px.series.slice(0, 3).map((s) => s.ticker));

  drawPriceChart(grid, px, evts, state);
  drawSmallMultiples(grid, px);
  drawForecasts(grid, fc);
}

// ── Main price chart ─────────────────────────────────────────────────────────
function drawPriceChart(grid, px, evts, state) {
  const chart = card(grid, {
    title: "Indexed prices × filings",
    span: 12,
    note: "Daily closes indexed to 100 at window start. Benchmarks dashed. Markers are EDGAR filings by the plotted insurers " +
      "(● 8-K  ◆ 10-Q  ■ 10-K  ▲ 13F) placed at filing time on that insurer's line — click to open. Hollow markers are backfilled.",
  });

  const byTicker = new Map(px.series.map((s) => [s.ticker, s]));
  const insurers = px.series.filter((s) => s.kind === "insurer").map((s) => s.ticker);
  const benches = px.series.filter((s) => s.kind === "benchmark").map((s) => s.ticker);

  // ticker picker
  const picker = el("div", { class: "controls" });
  const pick = el("div", { class: "tickpick" });
  for (const t of [...insurers, ...benches]) {
    const chip = el("span", { class: "chip" + (state.selected.has(t) ? " on" : "") }, esc(t));
    chip.addEventListener("click", () => {
      state.selected.has(t) ? state.selected.delete(t) : state.selected.add(t);
      chip.classList.toggle("on");
      redraw();
    });
    pick.appendChild(chip);
  }
  picker.appendChild(pick);
  chart.closest(".card").insertBefore(picker, chart);

  const draw = (svg, w) => {
    const sel = [...state.selected].filter((t) => byTicker.has(t));
    if (!sel.length) { setSub(chart, "select a ticker"); return; }

    const m = { t: 14, r: 52, b: 26, l: 46 };
    const H = 380, iw = w - m.l - m.r, ih = H - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    // indexed series
    const lines = sel.map((t, i) => {
      const s = byTicker.get(t);
      const base = s.closes.find((c) => c != null) || 1;
      const pts = s.dates.map((d, j) => ({ date: parseUtc(d), v: (s.closes[j] / base) * 100 }));
      return {
        ticker: t, kind: s.kind, pts,
        color: s.kind === "benchmark" ? BENCH_COLOR
          : TICKER_COLORS[t] || FALLBACK_COLORS[insurers.indexOf(t) % FALLBACK_COLORS.length],
        index: new Map(s.dates.map((d, j) => [d, (s.closes[j] / base) * 100])),
      };
    });

    const allPts = lines.flatMap((l) => l.pts);
    const x = d3.scaleUtc().domain(d3.extent(allPts, (p) => p.date)).range([0, iw]);
    const y = d3.scaleLinear().domain(d3.extent(allPts, (p) => p.v)).nice().range([ih, 0]);

    axes(g, { x, y, w: iw, h: ih, xFmt: d3.utcFormat("%b %y"), yTicks: 6 });
    g.append("line").attr("x1", 0).attr("x2", iw).attr("y1", y(100)).attr("y2", y(100))
      .attr("stroke", "rgba(255,255,255,.18)").attr("stroke-dasharray", "1,3");

    const lineGen = d3.line().x((p) => x(p.date)).y((p) => y(p.v)).curve(d3.curveMonotoneX);
    for (const l of lines) {
      g.append("path").attr("d", lineGen(l.pts))
        .attr("fill", "none").attr("stroke", l.color)
        .attr("stroke-width", l.kind === "benchmark" ? 1.3 : 1.7)
        .attr("stroke-dasharray", l.kind === "benchmark" ? "5,4" : null)
        .attr("stroke-opacity", 0.92);
      const last = l.pts[l.pts.length - 1];
      g.append("text").attr("x", iw + 6).attr("y", y(last.v) + 3.5)
        .attr("font-size", 10.5).attr("fill", l.color).attr("class", "mono")
        .text(l.ticker);
    }

    // filing markers on their ticker's line
    const evRows = evts.rows.filter((r) => state.selected.has(r.ticker));
    for (const r of evRows) {
      const line = lines.find((l) => l.ticker === r.ticker);
      if (!line) continue;
      const t = parseUtc(r.published_at || r.ingested_at);
      if (!t || t < x.domain()[0] || t > x.domain()[1]) continue;
      const day = fmt.dateUtc(t);
      // nearest trading day at-or-before the filing
      let v = line.index.get(day);
      if (v == null) {
        const prior = line.pts.filter((p) => p.date <= t);
        v = prior.length ? prior[prior.length - 1].v : null;
      }
      if (v == null) continue;
      g.append("text")
        .attr("x", x(t)).attr("y", y(v) + 4)
        .attr("text-anchor", "middle").attr("font-size", 9.5)
        .attr("fill", r.backfill ? "none" : line.color)
        .attr("stroke", line.color).attr("stroke-width", r.backfill ? 0.8 : 0)
        .style("cursor", "pointer").style("paint-order", "stroke")
        .text(FORM_GLYPH[r.form] || "○")
        .on("mousemove", (e) => tip.show(ttRows(`${r.ticker} ${r.form}`, [
          ["filed", fmt.dtUtc(t) + " UTC"],
          ["indexed px", fmt.s1(v)],
          r.backfill ? ["provenance", "backfill"] : null,
        ]) + `<div class="t3" style="margin-top:3px;max-width:300px">${esc(r.title)}</div>`, e))
        .on("mouseleave", tip.hide)
        .on("click", () => r.url && window.open(r.url, "_blank", "noopener"));
    }

    // crosshair
    const rule = g.append("line").attr("y1", 0).attr("y2", ih)
      .attr("stroke", "rgba(255,255,255,.22)").attr("stroke-dasharray", "2,3")
      .style("display", "none");
    svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", iw).attr("height", ih)
      .attr("fill", "transparent")
      .on("mousemove", (e) => {
        const [mx] = d3.pointer(e, g.node());
        const t = x.invert(mx);
        rule.style("display", null).attr("x1", mx).attr("x2", mx);
        const rows = lines.map((l) => {
          const prior = l.pts.filter((p) => p.date <= t);
          const p = prior[prior.length - 1];
          return p ? [
            `<span style="color:${l.color}">●</span> ${l.ticker}`,
            fmt.s1(p.v) + ` (${fmt.signedPct1(p.v / 100 - 1)})`,
          ] : null;
        });
        tip.show(ttRows(fmt.dateUtc(t), rows), e);
      })
      .on("mouseleave", () => { rule.style("display", "none"); tip.hide(); });

    setSub(chart, `${sel.join(" · ")} · ${fmt.int(evRows.length)} filings in window`);
  };

  const redraw = responsiveSvg(chart, 380, draw);
}

// ── Small multiples: excess vs IAK ───────────────────────────────────────────
function drawSmallMultiples(grid, px) {
  const chart = card(grid, {
    title: "Excess vs IAK",
    span: 12,
    note: "Each insurer's indexed price relative to the IAK insurance ETF over the window (geometric excess). Green above benchmark, red below. Sorted by ending excess.",
  });
  const iak = px.series.find((s) => s.ticker === "IAK");
  if (!iak) return empty(chart, "IAK benchmark not in price store");
  const iakIdx = new Map(iak.dates.map((d, i) => [d, iak.closes[i]]));
  const iakBase = iak.closes[0];

  const cells = px.series
    .filter((s) => s.kind === "insurer")
    .map((s) => {
      const base = s.closes[0];
      const pts = s.dates
        .filter((d) => iakIdx.has(d))
        .map((d, _, arr) => {
          const i = s.dates.indexOf(d);
          return { date: parseUtc(d), v: (s.closes[i] / base) / (iakIdx.get(d) / iakBase) - 1 };
        });
      return { ticker: s.ticker, pts, last: pts.length ? pts[pts.length - 1].v : 0 };
    })
    .sort((a, b) => b.last - a.last);

  const wrap = el("div");
  wrap.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px";
  chart.appendChild(wrap);

  for (const c of cells) {
    const cell = el("div");
    cell.style.cssText = "border:1px solid var(--hairline-2);border-radius:8px;padding:8px 10px 4px";
    cell.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:baseline">
      <span class="mono" style="font-weight:650;font-size:12px">${esc(c.ticker)}</span>
      <span class="mono ${c.last >= 0 ? "pos" : "neg"}" style="font-size:11.5px">${fmt.signedPct1(c.last)}</span></div>`;
    const spark = el("div");
    cell.appendChild(spark);
    wrap.appendChild(cell);

    responsiveSvg(spark, 44, (svg, w) => {
      if (!c.pts.length) return;
      const x = d3.scaleUtc().domain(d3.extent(c.pts, (p) => p.date)).range([2, w - 2]);
      const ext = d3.max(c.pts, (p) => Math.abs(p.v)) || 0.01;
      const y = d3.scaleLinear().domain([-ext, ext]).range([40, 4]);
      svg.append("line").attr("x1", 2).attr("x2", w - 2).attr("y1", y(0)).attr("y2", y(0))
        .attr("stroke", "var(--grid-line)");
      const area = d3.area().x((p) => x(p.date)).y0(y(0)).y1((p) => y(p.v)).curve(d3.curveMonotoneX);
      svg.append("path").attr("d", area(c.pts))
        .attr("fill", c.last >= 0 ? "rgba(81,207,102,.25)" : "rgba(255,107,107,.22)");
      svg.append("path")
        .attr("d", d3.line().x((p) => x(p.date)).y((p) => y(p.v)).curve(d3.curveMonotoneX)(c.pts))
        .attr("fill", "none").attr("stroke", c.last >= 0 ? "#51cf66" : "#ff6b6b")
        .attr("stroke-width", 1.2).attr("stroke-opacity", .9);
    });
  }
}

// ── Forecast panel ───────────────────────────────────────────────────────────
function drawForecasts(grid, fc) {
  const chart = card(grid, {
    title: "Return forecasts (alpha engine — shadow)",
    span: 12,
    note: "Local ML predictions of forward benchmark-excess returns. These never feed the leaderboard. " +
      "IC is out-of-sample information coefficient from a purged walk-forward; the baseline is the best of momentum / signal-only.",
  });
  if (!fc.rows.length) return empty(chart, "no forecasts yet — run `digest forecast predict`");

  const m = fc.rows.find((r) => r.ic != null);
  const noEdge = m && (m.ic <= 0 || (m.baseline_ic != null && m.ic <= m.baseline_ic));
  if (noEdge) {
    chart.appendChild(el("div", { class: "notice" },
      `Model shows <b>no predictive edge</b>: out-of-sample IC ${fmt.s3(m.ic)}` +
      (m.baseline_ic != null ? ` (baseline ${fmt.s3(m.baseline_ic)})` : "") +
      ` on n=${fmt.int(m.n_samples ?? 0)}. Forecasts shown for transparency, not for action.`));
  }
  setSub(chart, m ? `model: ${esc(m.algo)} · trained ${esc((m.trained_at || "").slice(0, 10))}` : "");

  const horizons = Array.from(new Set(fc.rows.map((r) => r.horizon_days))).sort((a, b) => a - b);
  const wrap = el("div");
  wrap.style.cssText = "display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px";
  chart.appendChild(wrap);

  for (const h of horizons) {
    const rows = fc.rows.filter((r) => r.horizon_days === h);
    const box = el("div");
    box.innerHTML = `<div class="kpi-label" style="margin-bottom:6px">${h}-day horizon · as of ${esc(rows[0].as_of)}</div>`;
    const table = el("table", { class: "data" });
    table.innerHTML = `<thead><tr><th>Ticker</th><th class="num">Pred. excess</th><th class="num">P(beat ≥1σ)</th></tr></thead>`;
    const tb = el("tbody");
    for (const r of rows) {
      const tr = el("tr");
      tr.innerHTML = `<td class="mono">${esc(r.ticker)}</td>
        <td class="num ${r.pred_excess >= 0 ? "pos" : "neg"}">${r.pred_excess != null ? fmt.signedPct1(r.pred_excess) : "—"}</td>
        <td class="num t2">${r.pred_prob != null ? fmt.pct0(r.pred_prob) : "—"}</td>`;
      tb.appendChild(tr);
    }
    table.appendChild(tb);
    box.appendChild(table);
    wrap.appendChild(box);
  }
}
