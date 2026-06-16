#!/usr/bin/env python3
"""P&C insurer valuation — the P/B↔ROE justified multiple, a residual-income (excess
return) intrinsic value, a reconciling DDM, an operating-ROE DuPont split, and a peer
P/B-on-ROE regression. Pure stdlib (no numpy/pandas).

Book value is meaningful for a financial, so insurers are valued off ROE vs cost of
equity, not off EBITDA multiples. For constant ROE & growth the justified P/B,
residual-income and DDM all collapse to the SAME number — the script shows all three
so the identity is visible, then triangulates against the market price and peers.

  python insurer_valuation.py --book-value 25000 --shares 250 --net-income 3500 \
      --cost-of-equity 0.09 --growth 0.04 --price 180 \
      --peers "A:1.5:0.11,B:2.2:0.15,C:1.8:0.13,D:2.6:0.17"

  # From the warehouse (book / shares / net income / price), supply r and g:
  python insurer_valuation.py --db data/state.db --insurer PGR -r 0.09 -g 0.05

  # --stdin for JSON, --demo for the verified worked example.

$ figures in USD millions; shares in millions; price and per-share in $. Rates are
decimals (0.09 = 9%). Outputs a human report; --format json for structured.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%".replace("-0.0%", "0.0%")


def justified_pb(roe: float, r: float, g: float) -> float:
    if r - g <= 0:
        sys.exit("insurer_valuation: cost of equity must exceed growth (r > g).")
    return (roe - g) / (r - g)


def residual_income(book: float, roe: float, r: float, g: float,
                    horizon: int, terminal_roe: float | None) -> dict:
    """Explicit excess-return forecast + Gordon terminal. With constant ROE this
    reproduces the justified-P/B closed form exactly (a verification)."""
    if r - g <= 0:
        sys.exit("insurer_valuation: cost of equity must exceed growth (r > g).")
    troe = terminal_roe if terminal_roe is not None else roe
    bv, pv_ri, yr = book, 0.0, []
    for t in range(1, horizon + 1):
        roe_t = roe + (troe - roe) * (t / horizon) if terminal_roe is not None else roe
        earnings = roe_t * bv
        ri = (roe_t - r) * bv                       # excess return over cost of equity
        disc = ri / (1 + r) ** t
        pv_ri += disc
        yr.append({"t": t, "begin_bv": bv, "roe": roe_t, "earnings": earnings,
                   "residual_income": ri, "pv": disc})
        bv *= (1 + g)
    # terminal: residual income continuing at the terminal spread, growing at g
    term_ri = (troe - r) * bv                        # next-year RI on end-of-horizon BV
    term_val = term_ri / (r - g) if r - g > 0 else 0.0
    pv_terminal = term_val / (1 + r) ** horizon
    value = book + pv_ri + pv_terminal
    return {"book_value": book, "pv_residual_income": pv_ri,
            "pv_terminal": pv_terminal, "intrinsic_equity_value": value,
            "implied_pb": value / book if book else None, "years": yr}


def ddm(book: float, roe: float, r: float, g: float) -> dict:
    """Gordon DDM with clean-surplus payout. D1 = book·(ROE−g); reconciles to P/B."""
    payout = (roe - g) / roe if roe else None
    d1 = book * (roe - g)
    value = d1 / (r - g) if r - g > 0 else None
    return {"implied_payout": payout, "dividend_next_year": d1,
            "intrinsic_equity_value": value,
            "implied_pb": (value / book) if (value and book) else None}


def dupont(p: dict) -> dict | None:
    """Operating ROE vs total ROE: strip realized gains / AOCI to expose the
    quality of the return. Runs only when operating components are supplied."""
    uw, nii, tax = p.get("uw_profit"), p.get("investment_income"), p.get("tax_rate")
    book, ni = p.get("book_value"), p.get("net_income")
    if uw is None or nii is None or tax is None or not book:
        return None
    pretax_op = uw + nii
    after_tax_op = pretax_op * (1 - tax)
    op_roe = after_tax_op / book
    total_roe = (ni / book) if ni is not None else None
    return {"pretax_operating_income": pretax_op, "after_tax_operating_income": after_tax_op,
            "operating_roe": op_roe, "total_roe": total_roe,
            "non_operating_roe": (total_roe - op_roe) if total_roe is not None else None}


def peer_regression(peers: list[dict], subject_roe: float) -> dict | None:
    """OLS P/B = a + b·ROE across the peer set; predicted fair P/B at subject ROE."""
    n = len(peers)
    if n < 2:
        return None
    mx = sum(q["roe"] for q in peers) / n
    my = sum(q["pb"] for q in peers) / n
    sxx = sum((q["roe"] - mx) ** 2 for q in peers)
    if sxx == 0:
        return None
    b = sum((q["roe"] - mx) * (q["pb"] - my) for q in peers) / sxx
    a = my - b * mx
    return {"intercept": a, "slope": b, "n_peers": n,
            "predicted_pb_at_subject_roe": a + b * subject_roe}


def value(p: dict) -> dict:
    for k in ("book_value", "cost_of_equity", "growth"):
        if p.get(k) is None:
            sys.exit(f"insurer_valuation: --{k.replace('_','-')} is required.")
    book, r, g = float(p["book_value"]), float(p["cost_of_equity"]), float(p["growth"])
    ni = p.get("net_income")
    roe = p.get("roe")
    if roe is None and ni is not None and book:
        roe = ni / book
    if roe is None:
        sys.exit("insurer_valuation: supply --roe or --net-income to imply ROE.")
    roe = float(roe)
    horizon = int(p.get("horizon", 10))
    out = {
        "book_value": book, "roe": roe, "cost_of_equity": r, "growth": g,
        "justified_pb": justified_pb(roe, r, g),
        "residual_income": residual_income(book, roe, r, g, horizon, p.get("terminal_roe")),
        "ddm": ddm(book, roe, r, g),
        "economic_profit_spread": roe - r,       # value created iff ROE > Ke
        "warnings": [],
    }
    du = dupont({**p, "roe": roe})
    if du:
        out["dupont"] = du
    shares, price = p.get("shares"), p.get("price")
    if shares:
        out["book_value_per_share"] = book / shares
        if ni is not None:
            out["eps"] = ni / shares
        if price is not None:
            bvps = book / shares
            out["price"] = price
            out["market_pb"] = price / bvps if bvps else None
            out["justified_price"] = out["justified_pb"] * bvps
            out["upside_to_justified"] = (out["justified_price"] / price - 1) if price else None
            if ni:
                out["pe"] = price / (ni / shares)
    if p.get("peers"):
        reg = peer_regression(p["peers"], roe)
        if reg:
            out["peer_regression"] = reg
            if shares and price is not None:
                bvps = book / shares
                reg["peer_fair_price"] = reg["predicted_pb_at_subject_roe"] * bvps
                reg["upside_to_peer_line"] = reg["peer_fair_price"] / price - 1
    if roe - r < 0:
        out["warnings"].append("ROE < cost of equity: the firm destroys value and "
                               "should trade BELOW book (P/B < 1).")
    return out


def parse_peers(s: str) -> list[dict]:
    out = []
    for item in s.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            name, pb, roe = item.split(":")
            out.append({"name": name, "pb": float(pb), "roe": float(roe)})
        except ValueError:
            sys.exit(f"insurer_valuation: malformed peer '{item}' (want name:pb:roe).")
    return out


def from_db(db_path: Path, insurer: str) -> dict:
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
        prow = con.execute(
            "SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (insurer,)).fetchone()
        payload = {
            "book_value": fact("equity", "common_equity"),
            "shares": fact("equity", "shares_outstanding"),
            "net_income": fact("segment_results", "net_income"),
            "price": float(prow[0]) if prow else None,
        }
        # shares often reported raw (not millions); normalize to millions if huge
        if payload.get("shares") and payload["shares"] > 100000:
            payload["shares"] = payload["shares"] / 1e6
    finally:
        con.close()
    return {k: v for k, v in payload.items() if v is not None}


_DEMO = {"book_value": 25000, "shares": 250, "net_income": 3500,
         "cost_of_equity": 0.09, "growth": 0.04, "price": 180,
         "uw_profit": 800, "investment_income": 3400, "tax_rate": 0.21,
         "peers": [{"name": "A", "pb": 1.5, "roe": 0.11}, {"name": "B", "pb": 2.2, "roe": 0.15},
                   {"name": "C", "pb": 1.8, "roe": 0.13}, {"name": "D", "pb": 2.6, "roe": 0.17}]}


def render(o: dict) -> str:
    L = ["INSURER VALUATION",
         f"  book {o['book_value']:,.0f}  ROE {_pct(o['roe'])}  Ke {_pct(o['cost_of_equity'])}"
         f"  g {_pct(o['growth'])}  economic-profit spread {_pct(o['economic_profit_spread'])}"]
    L.append(f"  justified P/B (ROE−g)/(Ke−g) = {o['justified_pb']:.2f}×")
    ri = o["residual_income"]
    L.append(f"  residual-income value {ri['intrinsic_equity_value']:,.0f} "
             f"(implied P/B {ri['implied_pb']:.2f}×; book {ri['book_value']:,.0f} + "
             f"PV excess returns {ri['pv_residual_income'] + ri['pv_terminal']:,.0f})")
    d = o["ddm"]
    L.append(f"  DDM value {d['intrinsic_equity_value']:,.0f} (implied P/B "
             f"{d['implied_pb']:.2f}×, payout {_pct(d['implied_payout'])})")
    if "dupont" in o:
        du = o["dupont"]
        L.append(f"  operating ROE {_pct(du['operating_roe'])} vs total ROE "
                 f"{_pct(du['total_roe'])} (non-operating {_pct(du['non_operating_roe'])})")
    if "market_pb" in o:
        L.append(f"  market P/B {o['market_pb']:.2f}×  justified price "
                 f"{o['justified_price']:.2f}  vs price {o['price']:.2f}  → upside "
                 f"{_pct(o['upside_to_justified'])}" + (f"  P/E {o['pe']:.1f}×" if "pe" in o else ""))
    if "peer_regression" in o:
        pr = o["peer_regression"]
        L.append(f"  peer line P/B = {pr['intercept']:.2f} + {pr['slope']:.1f}·ROE "
                 f"→ fair P/B {pr['predicted_pb_at_subject_roe']:.2f}× at this ROE"
                 + (f"  (upside {_pct(pr['upside_to_peer_line'])})" if "upside_to_peer_line" in pr else ""))
    for w in o["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="P&C insurer valuation (P/B-ROE, RI, DDM).")
    ap.add_argument("--db"); ap.add_argument("--insurer")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--book-value", type=float)
    ap.add_argument("--shares", type=float)
    ap.add_argument("--net-income", type=float)
    ap.add_argument("--roe", type=float)
    ap.add_argument("-r", "--cost-of-equity", type=float)
    ap.add_argument("-g", "--growth", type=float)
    ap.add_argument("--price", type=float)
    ap.add_argument("--horizon", type=int)
    ap.add_argument("--terminal-roe", type=float)
    ap.add_argument("--uw-profit", type=float)
    ap.add_argument("--investment-income", type=float)
    ap.add_argument("--tax-rate", type=float)
    ap.add_argument("--peers", help='"name:pb:roe,name:pb:roe,…"')
    args = ap.parse_args()

    skip = ("db", "insurer", "stdin", "demo", "format", "peers")
    if args.demo:
        p = dict(_DEMO)
    elif args.stdin:
        try:
            p = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            sys.exit(f"insurer_valuation: bad JSON on stdin ({e}).")
    elif args.db:
        if not args.insurer:
            sys.exit("insurer_valuation: --db requires --insurer.")
        p = from_db(Path(args.db), args.insurer)
        for d in vars(args):
            v = getattr(args, d)
            if v is not None and d not in skip:
                p[d] = v
    else:
        p = {d: getattr(args, d) for d in vars(args)
             if getattr(args, d) is not None and d not in skip}
        if not p:
            sys.exit("insurer_valuation: no inputs. Pass flags, --stdin, --db, or --demo.")
    if args.peers and "peers" not in p:
        p["peers"] = parse_peers(args.peers)

    out = value(p)
    if args.insurer:
        out["insurer"] = args.insurer
    print(json.dumps(out, indent=2) if args.format == "json" else render(out))


if __name__ == "__main__":
    main()
