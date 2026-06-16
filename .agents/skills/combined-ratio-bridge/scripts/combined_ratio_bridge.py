#!/usr/bin/env python3
"""Combined-ratio bridge — decompose a P&C combined ratio into loss / LAE / expense,
strip out catastrophes and prior-year reserve development, and expose the
**underlying** (current accident-year, ex-cat, ex-development) combined ratio that is
the real measure of underwriting margin trend. Pure stdlib.

The identity it enforces:

    reported combined = underlying + cat load + prior-year-development impact
    (underlying = combined − cat_ratio − pyd_ratio = current-AY ex-cat margin)

and, separately, the accident-year vs calendar-year split:

    accident-year combined = calendar-year (reported) combined − pyd_ratio

Bases matter and are a classic error source:
  • GAAP / "as reported": expense ratio = underwriting expense / EARNED premium.
  • Statutory / "trade":   expense ratio = underwriting expense / WRITTEN premium
                           (loss ratio still on earned). Pick with --basis.

Sign convention for prior_year_development ($):
  positive = ADVERSE (reserve strengthening, adds to losses)
  negative = FAVORABLE (reserve release, reduces losses)

Two ways to use it:

  # Single period from a filing / investor supplement (args or --stdin):
  python combined_ratio_bridge.py --earned-premium 1000 --incurred-loss 600 \
      --lae 50 --underwriting-expense 280 --prior-year-development 20 \
      --cat-losses 80 --written-premium 1040 --basis gaap

  # Period-over-period BRIDGE (decompose the CHANGE in the combined ratio):
  echo '{"basis":"gaap",
         "current":{"earned_premium":1050,"incurred_loss":...},
         "prior":{"earned_premium":1000,"incurred_loss":...}}' \
      | python combined_ratio_bridge.py --stdin

  # Self-checking worked example:
  python combined_ratio_bridge.py --demo
"""
from __future__ import annotations

import argparse
import json
import sys


# ── Core decomposition ───────────────────────────────────────────────────────


def decompose(p: dict, basis: str) -> dict:
    """Turn one period's $ figures into the ratio bridge. Ratios are decimals
    (0.93 = 93%). See module docstring for the sign convention + bases."""
    if "earned_premium" not in p:
        sys.exit("combined-ratio: 'earned_premium' is required.")
    nep = float(p["earned_premium"])
    if nep <= 0:
        sys.exit("combined-ratio: earned_premium must be > 0.")
    nwp = float(p["written_premium"]) if p.get("written_premium") is not None else None
    if nwp is not None and nwp <= 0:
        nwp = None   # non-positive written premium is unusable → treat like missing
    pyd = float(p.get("prior_year_development", 0.0))   # + adverse, − favorable
    cat = float(p.get("cat_losses", 0.0))
    uw_exp = float(p.get("underwriting_expense", 0.0))

    warnings: list[str] = []

    # Loss & LAE numerator: take a combined loss_lae if given, else losses + LAE.
    if p.get("loss_lae") is not None:
        loss_lae = float(p["loss_lae"])
        losses = lae = None
    elif p.get("incurred_loss") is not None:
        losses = float(p["incurred_loss"])
        lae = float(p.get("lae", 0.0))
        loss_lae = losses + lae
    else:
        sys.exit("combined-ratio: provide 'incurred_loss' (+ optional 'lae') or 'loss_lae'.")

    # Expense ratio basis.
    if basis in ("statutory", "trade"):
        if nwp is None:
            warnings.append("statutory/trade basis requested but written_premium is "
                            "missing or non-positive — expense ratio fell back to earned "
                            "premium (GAAP basis).")
            exp_denom, exp_basis = nep, "earned (fallback)"
        else:
            exp_denom, exp_basis = nwp, "written"
    else:
        exp_denom, exp_basis = nep, "earned"

    loss_ratio = (losses / nep) if losses is not None else None
    lae_ratio = (lae / nep) if lae is not None else None
    loss_lae_ratio = loss_lae / nep
    expense_ratio = (uw_exp / exp_denom) if exp_denom else 0.0
    combined = loss_lae_ratio + expense_ratio

    pyd_ratio = pyd / nep
    cat_ratio = cat / nep
    ay_combined = combined - pyd_ratio                 # remove development → accident-year
    ex_cat_combined = combined - cat_ratio             # remove cats
    underlying = combined - cat_ratio - pyd_ratio      # current-AY, ex-cat, ex-development
    underlying_loss_lae = loss_lae_ratio - cat_ratio - pyd_ratio   # the loss part of underlying

    if cat > loss_lae:
        warnings.append("cat_losses exceed total loss&LAE — check that cat is a subset "
                        "of incurred losses, not additive.")
    pyd_dir = "adverse" if pyd > 0 else "favorable" if pyd < 0 else "none"

    return {
        "basis": basis, "expense_basis": exp_basis,
        "earned_premium": nep, "written_premium": nwp,
        "loss_ratio": loss_ratio, "lae_ratio": lae_ratio,
        "loss_lae_ratio": loss_lae_ratio, "expense_ratio": expense_ratio,
        "combined_ratio": combined,
        "cat_ratio": cat_ratio, "pyd_ratio": pyd_ratio, "pyd_direction": pyd_dir,
        "accident_year_combined": ay_combined,
        "ex_cat_combined": ex_cat_combined,
        "underlying_combined": underlying,
        "underlying_loss_lae_ratio": underlying_loss_lae,
        "underwriting_margin": 1.0 - combined,
        "underwriting_result": round(nep * (1.0 - combined), 2),
        "warnings": warnings,
    }


def bridge(curr: dict, prior: dict) -> dict:
    """Decompose the CHANGE in the combined ratio between two periods into the
    change in each component. Δcombined = Δunderlying + Δcat + Δpyd (identity)."""
    keys = ["loss_lae_ratio", "underlying_loss_lae_ratio", "expense_ratio",
            "cat_ratio", "pyd_ratio", "underlying_combined",
            "accident_year_combined", "combined_ratio"]
    delta = {k: curr[k] - prior[k] for k in keys}
    delta["check_identity"] = round(
        delta["underlying_combined"] + delta["cat_ratio"] + delta["pyd_ratio"]
        - delta["combined_ratio"], 10)
    return delta


# ── Rendering ────────────────────────────────────────────────────────────────


def _pct(x: float | None) -> str:
    # round-then-+0.0 normalizes -0.0 / tiny-negatives so they don't render as "-0.00".
    return "   n/a" if x is None else f"{round(x * 100, 2) + 0.0:6.2f}"


def _dpct(x: float, width: int = 6) -> str:
    """Signed percentage for the bridge, normalizing -0.0 / tiny-negative to +0.00."""
    return f"{round(x * 100, 2) + 0.0:+{width}.2f}"


def render_single(r: dict, label: str) -> str:
    out = [f"Combined-ratio bridge — {label}", "=" * 58,
           f"  basis: {r['basis']}  (expense ratio on {r['expense_basis']} premium)", ""]
    out.append("  Component ratios (% of earned premium):")
    if r["loss_ratio"] is not None:
        out.append(f"    loss ratio                 {_pct(r['loss_ratio'])}%")
        out.append(f"    LAE ratio                  {_pct(r['lae_ratio'])}%")
    out += [f"    loss & LAE ratio           {_pct(r['loss_lae_ratio'])}%",
            f"    expense ratio              {_pct(r['expense_ratio'])}%",
            f"  ┌─────────────────────────────────────",
            f"  │ COMBINED RATIO            {_pct(r['combined_ratio'])}%   "
            f"(UW {'profit' if r['underwriting_margin'] >= 0 else 'loss'} "
            f"{abs(r['underwriting_margin'])*100:.2f} pts, "
            f"{r['underwriting_result']:,.0f})",
            f"  └─────────────────────────────────────", "",
            "  Quality of that combined ratio:",
            f"    cat load                   {_pct(r['cat_ratio'])}%",
            f"    prior-yr development       {_pct(r['pyd_ratio'])}%   ({r['pyd_direction']})",
            "", "  Bridge (what the headline is made of):",
            f"    underlying (curr AY ex-cat){_pct(r['underlying_combined'])}%",
            f"      + cat load               {_pct(r['cat_ratio'])}%",
            f"      + prior-yr development   {_pct(r['pyd_ratio'])}%",
            f"      = reported combined      {_pct(r['combined_ratio'])}%", "",
            "  Also:",
            f"    accident-year combined     {_pct(r['accident_year_combined'])}%   "
            f"(= reported − development)",
            f"    ex-cat combined            {_pct(r['ex_cat_combined'])}%"]
    if r["warnings"]:
        out += [""] + ["  ⚠ " + w for w in r["warnings"]]
    out += ["", "  Read: 'underlying' is the cleanest read of current pricing/loss-cost",
            "  margin — it strips one-off cats and prior-year reserve moves. A combined",
            "  ratio that only looks good because of favorable development (negative PYD)",
            "  is borrowing from the past; watch underlying, not the headline."]
    return "\n".join(out)


def render_bridge(curr: dict, prior: dict, d: dict, label: str) -> str:
    out = [f"Combined-ratio change bridge — {label}", "=" * 58,
           f"  prior combined    {_pct(prior['combined_ratio'])}%",
           f"  current combined  {_pct(curr['combined_ratio'])}%",
           f"  change            {_dpct(d['combined_ratio'])} pts", "",
           "  Attribution of the change (pts of combined ratio):",
           f"    Δ underlying (curr-AY ex-cat margin)  {_dpct(d['underlying_combined'])}",
           f"      of which Δ underlying loss & LAE    {_dpct(d['underlying_loss_lae_ratio'])}",
           f"               Δ expense                  {_dpct(d['expense_ratio'])}",
           f"    Δ cat load                            {_dpct(d['cat_ratio'])}",
           f"    Δ prior-year development              {_dpct(d['pyd_ratio'])}",
           f"    ────────────────────────────────────────────",
           f"    Δ combined (check)                    {_dpct(d['combined_ratio'])}",
           "",
           f"  Memo — calendar-year Δ loss & LAE       {_dpct(d['loss_lae_ratio'])}  "
           f"(underlying {_dpct(d['underlying_loss_lae_ratio'], 0)} + cat "
           f"{_dpct(d['cat_ratio'], 0)} + dev {_dpct(d['pyd_ratio'], 0)})"]
    if abs(d["check_identity"]) > 1e-6:
        out.append(f"    ⚠ identity off by {d['check_identity']:.2e} — check inputs")
    out += ["", "  Note: the two 'of which' lines sum to Δunderlying; the calendar-year",
            "  loss&LAE move (memo) instead mixes underlying loss-cost trend with the cat",
            "  and reserve-development swings. Lead with Δunderlying — it isolates whether",
            "  *current* underwriting got better or worse, independent of cat luck and",
            "  reserve releases."]
    return "\n".join(out)


# ── Demo (self-check) ────────────────────────────────────────────────────────

_DEMO = {"earned_premium": 1000, "incurred_loss": 600, "lae": 50,
         "underwriting_expense": 280, "prior_year_development": 20,
         "cat_losses": 80, "written_premium": 1040}


def main() -> None:
    ap = argparse.ArgumentParser(description="Combined-ratio decomposition / bridge.")
    ap.add_argument("--basis", choices=["gaap", "statutory", "trade"], default="gaap")
    ap.add_argument("--earned-premium", type=float)
    ap.add_argument("--written-premium", type=float)
    ap.add_argument("--incurred-loss", type=float)
    ap.add_argument("--loss-lae", type=float, help="combined loss+LAE (instead of losses+lae)")
    ap.add_argument("--lae", type=float)
    ap.add_argument("--underwriting-expense", type=float)
    ap.add_argument("--prior-year-development", type=float,
                    help="$; + adverse (strengthening), − favorable (release)")
    ap.add_argument("--cat-losses", type=float)
    ap.add_argument("--stdin", action="store_true",
                    help="JSON: a flat period, or {basis, current, prior} for a bridge")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    if args.demo:
        r = decompose(_DEMO, args.basis)
        print(json.dumps(r, indent=2) if args.format == "json"
              else render_single(r, "DEMO"))
        return

    if args.stdin:
        payload = json.load(sys.stdin)
        basis = payload.get("basis", args.basis)
        if "current" in payload:                      # bridge mode
            curr = decompose(payload["current"], basis)
            prior = decompose(payload["prior"], basis)
            d = bridge(curr, prior)
            if args.format == "json":
                print(json.dumps({"current": curr, "prior": prior, "delta": d}, indent=2))
            else:
                print(render_bridge(curr, prior, d, "period-over-period"))
            return
        period = payload
    else:
        basis = args.basis
        # argparse already yields single-underscore dest names (earned_premium, …).
        period = {k: v for k, v in vars(args).items()
                  if v is not None and k not in ("basis", "stdin", "demo", "format")}
        if "earned_premium" not in period:
            ap.error("provide --earned-premium and the loss/expense figures "
                     "(or --stdin / --demo).")

    r = decompose(period, basis)
    print(json.dumps(r, indent=2) if args.format == "json" else render_single(r, "single period"))


if __name__ == "__main__":
    main()
