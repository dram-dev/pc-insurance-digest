"""Edge-case regression tests for the Analyst method-skill helper scripts under
`.claude/skills/**/scripts/`. These are standalone, stdlib-only CLI tools invoked via
Bash (not importable packages), so we load them by path and run the main()-level paths
as subprocesses with the bare interpreter. Added with the PR that fixed the code-review
findings on bornhuetter_ferguson.py and combined_ratio_bridge.py — each test names the
finding it pins.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".claude/skills"
BF_PATH = SKILLS / "bornhuetter-ferguson/scripts/bornhuetter_ferguson.py"
CR_PATH = SKILLS / "combined-ratio-bridge/scripts/combined_ratio_bridge.py"
CRED_PATH = SKILLS / "credibility-weighting/scripts/credibility.py"
RATE_PATH = SKILLS / "ratemaking-indication/scripts/ratemaking_indication.py"
GLM_PATH = SKILLS / "glm-pricing/scripts/glm_pricing.py"
SEV_PATH = SKILLS / "severity-trend-decomposition/scripts/severity_trend.py"


def _run(path: Path, *cli_args, stdin: str | None = None):
    return subprocess.run([sys.executable, str(path), *cli_args],
                          input=stdin, capture_output=True, text=True)


def _clean_exit(r) -> bool:
    """Non-zero return with a friendly message, not a Python traceback."""
    return r.returncode != 0 and "Traceback" not in (r.stderr + r.stdout)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load(BF_PATH, "bf_skill")
cr = _load(CR_PATH, "cr_skill")

# The reference triangle (AY 2019-2022) shared by the BF tests.
_TRI = [
    {"ay": 2019, "dev": 0, "value": 1000}, {"ay": 2019, "dev": 1, "value": 1500},
    {"ay": 2019, "dev": 2, "value": 1750}, {"ay": 2019, "dev": 3, "value": 1800},
    {"ay": 2020, "dev": 0, "value": 1200}, {"ay": 2020, "dev": 1, "value": 1800},
    {"ay": 2020, "dev": 2, "value": 2100},
    {"ay": 2021, "dev": 0, "value": 1100}, {"ay": 2021, "dev": 1, "value": 1650},
    {"ay": 2022, "dev": 0, "value": 1300},
]


# ── Bornhuetter-Ferguson ──────────────────────────────────────────────────────

def test_bf_partial_premiums_totals_stay_coherent():
    """#1: an AY without a premium falls back to CL, so the BF total is never an
    impossible ultimate below latest (paid-to-date)."""
    dev = bf.develop(_TRI)
    rows = bf.apply_bf(dev["per_ay"], {2019: 2400, 2020: 2640, 2021: 2200}, 0.75)  # 2022 omitted
    t = bf.totals(rows)
    assert t["bf_ult"] >= t["latest"]
    ay22 = next(r for r in rows if r["accident_year"] == 2022)
    assert ay22["bf_ult"] == ay22["cl_ult"] and "chain-ladder" in ay22["note"]


def test_bf_cape_cod_handles_partial_premiums():
    """#2: Cape Cod derives over the AYs that have premiums instead of returning None."""
    dev = bf.develop(_TRI)
    cc = bf.cape_cod_elr(dev["per_ay"], {2019: 2400, 2020: 2640})
    assert cc is not None and cc["elr"] > 0 and cc["n_ay"] == 2


def test_bf_no_crash_on_undefined_cdf():
    """#3: a 0 CDF (tail 0 / a zero column) must not crash apply_bf."""
    dev = bf.develop(_TRI, tail=0.0)  # forces CDF 0 → pct_unreported None
    rows = bf.apply_bf(dev["per_ay"], {2022: 2600}, 0.75)
    assert all(r["bf_ult"] is not None for r in rows)


def test_bf_tail_must_be_positive():
    """#3: the CLI rejects a non-positive tail rather than producing the 0-CDF crash."""
    r = subprocess.run([sys.executable, str(BF_PATH), "--demo", "--tail", "0"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "tail must be positive" in (r.stderr + r.stdout)


def test_bf_parse_premiums_malformed_exits():
    """#7: malformed --premiums exits cleanly (no raw traceback)."""
    with pytest.raises(SystemExit):
        bf.parse_premiums("2019:2400,bad")


def test_bf_parse_premiums_duplicate_warns_keeps_last(capsys):
    out = bf.parse_premiums("2019:1,2019:9")
    assert out == {2019: 9.0} and "duplicate" in capsys.readouterr().err


def test_bf_apriori_override_applies():
    """#5: directly-supplied a-priori ultimates are honored by apply_bf."""
    dev = bf.develop(_TRI)
    rows = bf.apply_bf(dev["per_ay"], {}, 0.0, apriori_override={2022: 2000})
    ay22 = next(r for r in rows if r["accident_year"] == 2022)
    assert ay22["bf_ibnr"] == pytest.approx(2000 * (1 - 1 / 1.8), abs=0.5)


def test_bf_demo_still_reconciles():
    """The verified worked example is unchanged after the edits."""
    r = subprocess.run([sys.executable, str(BF_PATH), "--demo", "--format", "json"],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    assert d["totals"]["cl_ibnr"] == 1430.0 and d["totals"]["bf_ibnr"] == 1196.67
    assert round(d["elr"], 4) == 0.75


# ── combined-ratio bridge ─────────────────────────────────────────────────────

def test_cr_statutory_basis_honored_via_stdin():
    """#2 (combined): flat --stdin honors the payload 'basis' (was using args.basis)."""
    payload = json.dumps({"basis": "statutory", "earned_premium": 1000,
                          "incurred_loss": 600, "underwriting_expense": 280,
                          "written_premium": 1040})
    r = subprocess.run([sys.executable, str(CR_PATH), "--stdin", "--format", "json"],
                       input=payload, capture_output=True, text=True)
    d = json.loads(r.stdout)
    assert d["basis"] == "statutory" and d["expense_basis"] == "written"
    assert round(d["expense_ratio"], 6) == round(280 / 1040, 6)


def test_cr_missing_loss_exits_cleanly():
    """#5 (combined): a missing loss figure → clean SystemExit, not a raw KeyError."""
    with pytest.raises(SystemExit):
        cr.decompose({"earned_premium": 1000, "underwriting_expense": 280}, "gaap")


def test_cr_nonpositive_written_falls_back():
    """#6 (combined): written_premium<=0 on statutory → earned fallback + warning."""
    r = cr.decompose({"earned_premium": 1000, "incurred_loss": 600,
                      "underwriting_expense": 280, "written_premium": 0}, "statutory")
    assert r["expense_basis"] == "earned (fallback)"
    assert round(r["expense_ratio"], 6) == round(280 / 1000, 6)
    assert any("non-positive" in w for w in r["warnings"])


def test_cr_no_negative_zero_formatting():
    """#14/#15 (combined): zero / tiny-negative ratios never render as '-0.00'."""
    assert "-0.00" not in cr._pct(-1e-9)
    assert "-0.00" not in cr._dpct(-0.0)
    assert "-0.00" not in cr._dpct(-1e-9)


def test_cr_bridge_identity_holds():
    curr = cr.decompose({"earned_premium": 1100, "incurred_loss": 627, "lae": 55,
                         "underwriting_expense": 297, "prior_year_development": -22,
                         "cat_losses": 99}, "gaap")
    prior = cr.decompose({"earned_premium": 1000, "incurred_loss": 600, "lae": 50,
                          "underwriting_expense": 280, "prior_year_development": 20,
                          "cat_losses": 80}, "gaap")
    d = cr.bridge(curr, prior)
    assert abs(d["check_identity"]) < 1e-9
    assert d["underlying_combined"] == pytest.approx(
        d["underlying_loss_lae_ratio"] + d["expense_ratio"])


def test_cr_demo_still_reconciles():
    r = subprocess.run([sys.executable, str(CR_PATH), "--demo", "--format", "json"],
                       capture_output=True, text=True)
    d = json.loads(r.stdout)
    assert round(d["combined_ratio"] * 100, 2) == 93.0
    assert round(d["underlying_combined"] * 100, 2) == 83.0


# ── credibility-weighting (round-2 audit) ─────────────────────────────────────

def test_credibility_stdin_buhlmann_nk_works():
    """The documented '--n & --k' Bühlmann path now works via stdin (the dispatch
    gated on args.mode instead of the resolved mode)."""
    r = _run(CRED_PATH, "--stdin",
             stdin=json.dumps({"mode": "buhlmann", "n": 300, "k": 40,
                               "observed": 0.8, "complement": 0.65}))
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["mode"] == "buhlmann" and round(d["Z"], 4) == round(300 / 340, 4)


def test_credibility_classical_missing_n_exits_cleanly():
    assert _clean_exit(_run(CRED_PATH, "--mode", "classical", "--observed", "0.8"))


def test_credibility_buhlmann_zero_vhm_exits_cleanly():
    r = _run(CRED_PATH, "--stdin",
             stdin=json.dumps({"mode": "buhlmann", "n": 10, "epv": 5, "vhm": 0}))
    assert _clean_exit(r) and "vhm" in (r.stderr + r.stdout).lower()


# ── ratemaking-indication (round-2 audit) ─────────────────────────────────────

def test_ratemaking_missing_key_exits_cleanly():
    r = _run(RATE_PATH, "--method", "loss_ratio", "--loss-lae-ratio", "0.65",
             "--fixed-expense-ratio", "0.06", "--variable-expense-ratio", "0.25")
    assert _clean_exit(r) and "target_profit" in (r.stderr + r.stdout)


def test_ratemaking_zero_current_rate_exits_cleanly():
    r = _run(RATE_PATH, "--stdin",
             stdin=json.dumps({"method": "pure_premium", "pure_premium": 300,
                               "fixed_expense_per_exposure": 20,
                               "variable_expense_ratio": 0.25, "target_profit": 0.05,
                               "current_avg_rate": 0}))
    assert _clean_exit(r)


# ── glm-pricing (round-2 audit) ───────────────────────────────────────────────

def test_glm_zero_exposure_exits_cleanly():
    r = _run(GLM_PATH, stdin=json.dumps({"family": "poisson", "rows": [
        {"exposure": 0, "count": 0, "factors": {"region": "A"}},
        {"exposure": 100, "count": 10, "factors": {"region": "B"}}]}))
    assert _clean_exit(r) and "exposure" in (r.stderr + r.stdout)


def test_glm_singular_design_exits_cleanly():
    # region and plan are perfectly collinear → singular design matrix.
    r = _run(GLM_PATH, stdin=json.dumps({"family": "poisson", "rows": [
        {"exposure": 100, "count": 10, "factors": {"region": "A", "plan": "P"}},
        {"exposure": 100, "count": 12, "factors": {"region": "A", "plan": "P"}},
        {"exposure": 100, "count": 20, "factors": {"region": "B", "plan": "Q"}},
        {"exposure": 100, "count": 22, "factors": {"region": "B", "plan": "Q"}}]}))
    assert _clean_exit(r) and "glm:" in (r.stderr + r.stdout)


# ── severity-trend-decomposition (round-2 audit) ──────────────────────────────

def test_severity_no_negative_zero_formatting():
    r = _run(SEV_PATH, "--stdin", "--format", "text",
             stdin=json.dumps({"series": [{"date": "2020-01-01", "value": 100},
                                          {"date": "2021-01-01", "value": 100},
                                          {"date": "2022-01-01", "value": 100}]}))
    assert r.returncode == 0 and "-0.00%" not in r.stdout


def test_severity_stdin_missing_series_exits_cleanly():
    assert _clean_exit(_run(SEV_PATH, "--stdin", stdin="{}"))
