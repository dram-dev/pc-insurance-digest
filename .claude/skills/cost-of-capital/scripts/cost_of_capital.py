#!/usr/bin/env python3
"""P&C insurer cost of capital — CAPM cost of equity, after-tax cost of debt, WACC,
capital structure / double leverage, RAROC, and the economic-profit (ROE−Ke) spread.
Pure stdlib (no numpy/pandas).

  python cost_of_capital.py --risk-free 0.04 --beta 1.0 --erp 0.05 \
      --pretax-cost-of-debt 0.05 --tax-rate 0.21 \
      --equity-mktval 45000 --debt 5000 \
      --roe 0.14 --book-value 25000 \
      --sub-equity 28000 --parent-equity 25000 \
      --risk-adjusted-earnings 3318 --economic-capital 20000

  # From the warehouse — market-cap from prices, debt/book from XBRL; β computed
  # from the price store vs SPY when --compute-beta is set:
  python cost_of_capital.py --db data/state.db --insurer ALL --compute-beta \
      --risk-free 0.04 --erp 0.05 --pretax-cost-of-debt 0.05

  # --stdin for JSON, --demo for the verified worked example.

$ figures in USD millions; rates/β as decimals (0.09 = 9%, β unitless). Outputs a
human report; --format json for structured.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.2f}%".replace("-0.00%", "0.00%")


def capm(rf: float, beta: float, erp: float) -> float:
    return rf + beta * erp


def cost_of_capital(p: dict) -> dict:
    out = {"warnings": []}
    rf, beta, erp = p.get("risk_free"), p.get("beta"), p.get("erp")
    ke = p.get("cost_of_equity")
    if ke is None and None not in (rf, beta, erp):
        ke = capm(rf, beta, erp)
        out["capm"] = {"risk_free": rf, "beta": beta, "erp": erp}
    if ke is None:
        sys.exit("cost_of_capital: supply --cost-of-equity, or --risk-free + --beta + "
                 "--erp for CAPM.")
    out["cost_of_equity"] = ke

    tax = p.get("tax_rate", 0.21)
    kd_pre = p.get("pretax_cost_of_debt")
    kd_post = kd_pre * (1 - tax) if kd_pre is not None else None
    if kd_post is not None:
        out["pretax_cost_of_debt"] = kd_pre
        out["after_tax_cost_of_debt"] = kd_post
        out["tax_rate"] = tax

    e, d = p.get("equity_mktval"), p.get("debt")
    if e is not None and d is not None and kd_post is not None:
        v = e + d
        out["capital_structure"] = {
            "equity_mktval": e, "debt": d, "total_capital": v,
            "equity_weight": e / v, "debt_weight": d / v,
            "debt_to_capital": d / v, "debt_to_equity": d / e}
        out["wacc"] = (e / v) * ke + (d / v) * kd_post

    se, pe = p.get("sub_equity"), p.get("parent_equity")
    if se is not None and pe:
        out["double_leverage"] = se / pe
        if se / pe > 1.0:
            out["warnings"].append(
                f"double leverage {se/pe:.2f}× > 1.0 — the parent funds subsidiary "
                "equity partly with holding-company debt; sub dividends must service it.")

    roe = p.get("roe")
    if roe is None and p.get("net_income") is not None and p.get("book_value"):
        roe = p["net_income"] / p["book_value"]
    if roe is not None:
        out["roe"] = roe
        out["economic_profit_spread"] = roe - ke           # ROE − Ke
        if p.get("book_value"):
            out["economic_profit_eva"] = (roe - ke) * p["book_value"]
        if roe < ke:
            out["warnings"].append("ROE below cost of equity — destroying value; should "
                                   "trade below book (see insurer-valuation).")

    rae, ec = p.get("risk_adjusted_earnings"), p.get("economic_capital")
    if rae is not None and ec:
        out["raroc"] = rae / ec
        out["raroc_hurdle"] = ke
        out["raroc_creates_value"] = (rae / ec) > ke

    out["hurdle_rate"] = ke   # equity projects discount at Ke; whole-firm at WACC
    if "wacc" in out:
        out["whole_firm_hurdle_rate"] = out["wacc"]
    return out


# ── beta from the price store (vs SPY) ───────────────────────────────────────

def _closes(con, ticker: str, lookback: int) -> dict[str, float]:
    rows = con.execute(
        "SELECT date, close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT ?",
        (ticker, lookback)).fetchall()
    return {d: float(c) for d, c in rows}


def beta_from_prices(con, ticker: str, benchmark: str, lookback: int) -> float | None:
    ti, bm = _closes(con, ticker, lookback), _closes(con, benchmark, lookback)
    dates = sorted(set(ti) & set(bm))
    if len(dates) < 30:
        return None
    ri = [ti[dates[k]] / ti[dates[k - 1]] - 1 for k in range(1, len(dates))]
    rm = [bm[dates[k]] / bm[dates[k - 1]] - 1 for k in range(1, len(dates))]
    var_m = statistics.pvariance(rm)
    if var_m == 0:
        return None
    mi, mm = statistics.mean(ri), statistics.mean(rm)
    cov = sum((a - mi) * (b - mm) for a, b in zip(ri, rm)) / len(ri)
    return cov / var_m


def from_db(db_path: Path, insurer: str, compute_beta: bool) -> dict:
    if not db_path.exists():
        sys.exit(f"No DB at {db_path}. Pass --db or run `digest init-db`.")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        def fact(dataset, field):
            row = con.execute(
                "SELECT value FROM insurer_xbrl_facts WHERE insurer=? AND dataset=? "
                "AND field=? AND segment IS NULL AND product IS NULL "
                "ORDER BY period_end DESC, as_of DESC LIMIT 1",
                (insurer, dataset, field)).fetchone()
            return float(row[0]) if row else None
        prow = con.execute("SELECT close FROM prices WHERE ticker=? ORDER BY date DESC "
                           "LIMIT 1", (insurer,)).fetchone()
        shares = fact("equity", "shares_outstanding")
        if shares and shares > 100000:
            shares = shares / 1e6
        price = float(prow[0]) if prow else None
        payload = {
            "equity_mktval": (price * shares) if (price and shares) else None,
            "debt": fact("capital_structure", "long_term_debt")
            or fact("capital_structure", "long_term_debt_total"),
            "net_income": fact("segment_results", "net_income"),
            "book_value": fact("equity", "common_equity"),
        }
        if compute_beta:
            payload["beta"] = beta_from_prices(con, insurer, "SPY", 252)
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO = {"risk_free": 0.04, "beta": 1.0, "erp": 0.05, "pretax_cost_of_debt": 0.05,
         "tax_rate": 0.21, "equity_mktval": 45000, "debt": 5000, "roe": 0.14,
         "book_value": 25000, "sub_equity": 28000, "parent_equity": 25000,
         "risk_adjusted_earnings": 3318, "economic_capital": 20000}


def render(o: dict) -> str:
    L = ["COST OF CAPITAL"]
    if "capm" in o:
        c = o["capm"]
        L.append(f"  CAPM Ke = {_pct(c['risk_free'])} + β {c['beta']:.2f} × ERP "
                 f"{_pct(c['erp'])} = {_pct(o['cost_of_equity'])}")
    else:
        L.append(f"  cost of equity Ke {_pct(o['cost_of_equity'])}")
    if "after_tax_cost_of_debt" in o:
        L.append(f"  after-tax cost of debt = {_pct(o['pretax_cost_of_debt'])} × "
                 f"(1−{o['tax_rate']:.0%}) = {_pct(o['after_tax_cost_of_debt'])}")
    if "wacc" in o:
        cs = o["capital_structure"]
        L.append(f"  WACC = {_pct(cs['equity_weight'])[:-1]}%·Ke + "
                 f"{_pct(cs['debt_weight'])[:-1]}%·Kd = {_pct(o['wacc'])}")
        L.append(f"  debt/capital {_pct(cs['debt_to_capital'])}  debt/equity {_pct(cs['debt_to_equity'])}")
    if "double_leverage" in o:
        L.append(f"  double leverage {o['double_leverage']:.2f}×")
    if "economic_profit_spread" in o:
        L.append(f"  ROE {_pct(o['roe'])} − Ke {_pct(o['cost_of_equity'])} = economic-profit "
                 f"spread {_pct(o['economic_profit_spread'])}"
                 + (f"  → EVA {o['economic_profit_eva']:,.0f}" if "economic_profit_eva" in o else ""))
    if "raroc" in o:
        L.append(f"  RAROC {_pct(o['raroc'])} vs hurdle {_pct(o['raroc_hurdle'])} → "
                 f"{'value-creating' if o['raroc_creates_value'] else 'value-destroying'}")
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="P&C insurer cost of capital (CAPM/WACC/RAROC).")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--compute-beta", action="store_true")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    for d in ("risk_free", "beta", "erp", "cost_of_equity", "pretax_cost_of_debt",
              "tax_rate", "equity_mktval", "debt", "roe", "net_income", "book_value",
              "sub_equity", "parent_equity", "risk_adjusted_earnings", "economic_capital"):
        ap.add_argument(f"--{d.replace('_', '-')}", type=float)
    args = ap.parse_args()

    skip = ("db", "insurer", "compute_beta", "stdin", "demo", "format")
    if args.demo:
        p = dict(_DEMO)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"cost_of_capital: bad JSON on stdin ({e}).")
    elif args.db:
        if not args.insurer:
            sys.exit("cost_of_capital: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer, args.compute_beta)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in skip:
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args)
             if getattr(args, d) is not None and d not in skip}
        if not p:
            sys.exit("cost_of_capital: no inputs. Pass flags, --stdin, --db, or --demo.")

    out = cost_of_capital(p)
    if args.insurer:
        out["insurer"] = args.insurer
    print(json.dumps(out, indent=2) if args.format == "json" else render(out))


if __name__ == "__main__":
    main()
