// Signals — leaderboard, multiplicative score anatomy, score distribution.

import { get } from "../api.js";
import { sourceLabel, fmt, agoUtc, tip, ttRows, esc } from "../theme.js";
import { card, el, empty, responsiveSvg, setSub, topicChip, tierBadge } from "../components.js";

const FACTORS = [
  ["source_mult", "source"],
  ["regime_mult", "regime"],
  ["topic_relevance", "topic relevance"],
  ["recency", "recency"],
  ["llm_judgment", "LLM judgment"],
  ["topic_boost", "topic priority"],
  ["burden_boost", "reg. burden"],
  ["insurer_boost", "insurer priority"],
  ["inflation_boost", "inflation kw"],
  ["regulatory_boost", "regulatory kw"],
  ["tplf_boost", "litigation/TPLF"],
  ["reserve_boost", "reserve deterioration"],
];

export async function render(root, store) {
  const days = store.range;
  const [lb, dist] = await Promise.all([
    get("leaderboard", { days, limit: 50 }),
    get("score-distribution", { days }),
  ]);

  const grid = el("div", { class: "grid" });
  root.appendChild(grid);

  if (!lb.rows.length) {
    card(grid, { title: "Leaderboard", span: 12 });
    return empty(grid.querySelector(".chart"), "no scored items in this window");
  }

  // anatomy panel re-renders when a row is selected
  let selected = lb.rows[0];
  let renderAnatomy = () => {};

  drawLeaderboard(grid, lb, days, (row) => { selected = row; renderAnatomy(); });
  renderAnatomy = drawAnatomy(grid, () => selected);
  drawDistribution(grid, dist);
}

// ── Leaderboard ──────────────────────────────────────────────────────────────
function drawLeaderboard(grid, lb, days, onSelect) {
  const chart = card(grid, {
    title: "Leaderboard",
    span: 12,
    sub: `latest score per item · top ${lb.rows.length} in window`,
    note: "The persisted latest score per item. Select a row to inspect its multiplicative anatomy below.",
  });
  const maxScore = lb.rows[0].score || 1;

  const table = el("table", { class: "data" });
  table.innerHTML = `<thead><tr>
    <th>#</th><th>Score</th><th>Tier</th><th>Item</th><th>Topic</th><th>Source</th><th>Age</th>
  </tr></thead>`;
  const tbody = el("tbody");
  lb.rows.forEach((r, i) => {
    const tr = el("tr");
    const barW = Math.max(2, 56 * (r.score / maxScore));
    tr.innerHTML = `
      <td class="num t3">${i + 1}</td>
      <td class="num">${fmt.s2(r.score)}<span class="scorebar" style="width:${barW}px"></span></td>
      <td>${tierBadge(r.tier)}</td>
      <td class="title-cell">${r.url
        ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>`
        : esc(r.title)}${r.backfill ? ' <span class="t3" title="backfilled historical row">◌</span>' : ""}</td>
      <td>${topicChip(r.topic)}</td>
      <td class="t2">${esc(sourceLabel(r.source))}</td>
      <td class="t3" title="${esc(r.published_at || r.ingested_at)} UTC">${agoUtc(r.published_at || r.ingested_at)}</td>`;
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => {
      tbody.querySelectorAll("tr").forEach((x) => x.classList.remove("sel-row"));
      tr.classList.add("sel-row");
      onSelect(r);
    });
    if (i === 0) tr.classList.add("sel-row");
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  chart.style.maxHeight = "430px";
  chart.style.overflowY = "auto";
  chart.appendChild(table);
}

// ── Score anatomy: log-space contribution of each multiplicative factor ─────
function drawAnatomy(grid, getSelected) {
  const chart = card(grid, {
    title: "Score anatomy",
    span: 7,
    note: "Factors multiply: score = ∏ factor. Bars show log₂ contribution — right of the spine boosts, left dampens. Neutral (×1.00) factors are listed but unbarred.",
  });

  const render = () => {
    const r = getSelected();
    chart.innerHTML = "";
    if (!r) return empty(chart, "select a leaderboard row");
    setSub(chart, esc(r.title.length > 70 ? r.title.slice(0, 70) + "…" : r.title));

    const rows = FACTORS
      .map(([key, name]) => ({ key, name, v: r[key] }))
      .filter((f) => f.v != null);
    const product = rows.reduce((p, f) => p * f.v, 1);

    const H = rows.length * 27 + 58;
    responsiveSvg(chart, H, (svg, w) => {
      const m = { t: 6, r: 64, b: 40, l: 142 };
      const iw = w - m.l - m.r, ih = rows.length * 27;
      const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

      const maxAbs = Math.max(0.4, d3.max(rows, (f) => Math.abs(Math.log2(f.v))) || 0.4);
      const x = d3.scaleLinear().domain([-maxAbs, maxAbs]).range([0, iw]);
      const y = d3.scaleBand().domain(rows.map((f) => f.key)).range([0, ih]).padding(0.34);

      g.append("line").attr("x1", x(0)).attr("x2", x(0)).attr("y1", -2).attr("y2", ih + 2)
        .attr("stroke", "rgba(255,255,255,.22)");

      for (const f of rows) {
        const lg = Math.log2(f.v);
        const yc = y(f.key) + y.bandwidth() / 2;
        g.append("text").attr("x", -10).attr("y", yc + 3.5).attr("text-anchor", "end")
          .attr("font-size", 11).attr("fill", "var(--text-2)").text(f.name);
        if (Math.abs(lg) > 1e-9) {
          g.append("rect")
            .attr("x", Math.min(x(0), x(lg))).attr("y", y(f.key))
            .attr("width", Math.abs(x(lg) - x(0))).attr("height", y.bandwidth())
            .attr("rx", 3)
            .attr("fill", lg > 0 ? "#51cf66" : "#ff6b6b").attr("fill-opacity", 0.75)
            .on("mousemove", (e) => tip.show(ttRows(f.name, [
              ["factor", "×" + fmt.s3(f.v)], ["log₂", fmt.s3(lg)],
            ]), e))
            .on("mouseleave", tip.hide);
        }
        g.append("text")
          .attr("x", x(lg) + (lg >= 0 ? 6 : -6)).attr("y", yc + 3.5)
          .attr("text-anchor", lg >= 0 ? "start" : "end")
          .attr("font-size", 10.5).attr("fill", "var(--text-2)").attr("class", "mono")
          .text("×" + fmt.s2(f.v));
      }

      const foot = svg.append("text").attr("x", m.l).attr("y", H - 14)
        .attr("font-size", 11.5).attr("fill", "var(--text-2)");
      foot.append("tspan").text("∏ factors = ");
      foot.append("tspan").attr("class", "mono").attr("fill", "var(--text)")
        .attr("font-weight", 650).text(fmt.s3(product));
      foot.append("tspan").attr("fill", "var(--text-3)")
        .text(`   persisted score ${fmt.s3(r.score)}` +
          (r.learned_score != null ? `   learned (shadow) ${fmt.s3(r.learned_score)}` : ""));
    });
  };
  render();
  return render;
}

// ── Distribution ─────────────────────────────────────────────────────────────
function drawDistribution(grid, dist) {
  const chart = card(grid, {
    title: "Score distribution",
    span: 5,
    note: "All latest scores in window (log-spaced bins). Dashed rules mark the lowest score persisted in each tier — the live self-calibrated cutoffs.",
  });
  if (!dist.scores.length) return empty(chart, "no scores in window");
  setSub(chart, `n=${fmt.int(dist.n)}`);

  responsiveSvg(chart, 250, (svg, w) => {
    const m = { t: 12, r: 12, b: 28, l: 38 };
    const iw = w - m.l - m.r, ih = 250 - m.t - m.b;
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);

    const lo = Math.max(0.01, d3.min(dist.scores));
    const hi = d3.max(dist.scores) * 1.05;
    const x = d3.scaleLog().domain([lo, hi]).range([0, iw]);
    const thresholds = d3.range(0, 25).map((i) => lo * Math.pow(hi / lo, i / 24));
    const bins = d3.bin().domain([lo, hi]).thresholds(thresholds)(dist.scores);
    const y = d3.scaleLinear().domain([0, d3.max(bins, (b) => b.length)]).nice().range([ih, 0]);

    const ya = g.append("g").attr("class", "axis")
      .call(d3.axisLeft(y).ticks(4).tickSize(-iw));
    ya.select(".domain").remove();
    ya.selectAll("line").attr("stroke", "var(--grid-line)");
    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(6, "~g").tickSizeOuter(0));
    xa.select(".domain").attr("stroke", "var(--grid-line)");

    g.selectAll(".bar").data(bins).join("rect")
      .attr("x", (b) => x(b.x0) + 0.5)
      .attr("width", (b) => Math.max(0.5, x(b.x1) - x(b.x0) - 1))
      .attr("y", (b) => y(b.length))
      .attr("height", (b) => ih - y(b.length))
      .attr("rx", 2)
      .attr("fill", "var(--accent)").attr("fill-opacity", 0.65)
      .on("mousemove", (e, b) => tip.show(ttRows(null, [
        [`${fmt.s2(b.x0)} – ${fmt.s2(b.x1)}`, fmt.int(b.length) + " items"],
      ]), e))
      .on("mouseleave", tip.hide);

    for (const [tier, cut] of Object.entries(dist.tier_cuts || {})) {
      if (cut == null || cut < lo) continue;
      const c = tier === "high" ? "#ffd43b" : "#74c0fc";
      g.append("line").attr("x1", x(cut)).attr("x2", x(cut)).attr("y1", 0).attr("y2", ih)
        .attr("stroke", c).attr("stroke-opacity", .55).attr("stroke-dasharray", "4,4");
      g.append("text").attr("x", x(cut) + 4).attr("y", 10)
        .attr("font-size", 10).attr("fill", c).text(tier);
    }
  });
}
