"""
engine.py
=========
Heat pump subsidy scenario engine for the Streamlit app.
Mirrors the math in ../subsidy_scenarios.py so that the preset scenarios
reproduce paper Table 6 exactly.

Rule schema
-----------
A "rule" is a dict describing how much state (or federal) rebate a household
receives under a given scenario. Any of the components can be zero/None:

    {
      "per_cap_ton": float,   # $/cap-ton of HVAC capacity  (cap-ton ≈ 12k BTU/hr)
      "percent":     float,   # fraction of HP retail cost (0.0–1.0)
      "flat":        float,   # flat $ per home
      "cap":         float,   # total dollar cap on the sum of the above
    }

Rebate = min(cap, per_cap_ton*size_calc + percent*hp_retail + flat)
         (cap = +inf if None)

A "scenario" is a dict with per-fuel rules:

    {
      "oil":     Rule,
      "gas":     Rule,
      "propane": Rule,                 # defaults to oil if omitted
      "federal": {"mode": "none"|"flat"|"hear", "flat": 2000},
    }
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from scipy.special import expit

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "app_data"

# ─────────────────────────────────────────────────────────────────────────────
# Data loading (cache-friendly: call once)
# ─────────────────────────────────────────────────────────────────────────────
def load_constants(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or DATA_DIR / "constants.json"
    with open(path) as f:
        c = json.load(f)
    # Rehydrate the CO2 fuel-system dict (JSON keys are strings)
    c["co2"]["CO2_BY_FUEL_SYS"] = {
        tuple(k.split("|")): v for k, v in c["co2"]["CO2_BY_FUEL_SYS"].items()
    }
    return c

def load_cama(path: Optional[Path] = None) -> pd.DataFrame:
    path = path or DATA_DIR / "cama_minimal.csv"
    return pd.read_csv(path)

# ─────────────────────────────────────────────────────────────────────────────
# Rule evaluation (vectorized)
# ─────────────────────────────────────────────────────────────────────────────
def _rule_amount(rule: Optional[Dict[str, Any]], df: pd.DataFrame) -> np.ndarray:
    """Return the rebate amount per row for a single rule."""
    if rule is None:
        return np.zeros(len(df))
    pct  = float(rule.get("percent", 0.0) or 0.0)
    pct_ton = float(rule.get("per_cap_ton", 0.0) or 0.0)
    flat = float(rule.get("flat", 0.0) or 0.0)
    cap  = rule.get("cap", None)
    raw = pct * df["hp_retail"].to_numpy() + pct_ton * df["size_calc"].to_numpy() + flat
    if cap is not None:
        raw = np.minimum(raw, float(cap))
    return np.maximum(raw, 0.0)

def _state_rebate(scenario: Dict[str, Any], df: pd.DataFrame) -> np.ndarray:
    """Assemble per-home state rebate by fuel."""
    rebate = np.zeros(len(df))
    oil_rule = scenario.get("oil")
    gas_rule = scenario.get("gas")
    prop_rule = scenario.get("propane", oil_rule)

    fuel = df["fuel"].to_numpy()
    is_oil     = fuel == "Oil"
    is_gas     = fuel == "Gas"
    is_propane = fuel == "Propane"

    # Evaluate each rule on the full frame, then mask.
    if oil_rule:
        rebate = np.where(is_oil, _rule_amount(oil_rule, df), rebate)
    if gas_rule:
        rebate = np.where(is_gas, _rule_amount(gas_rule, df), rebate)
    if prop_rule:
        rebate = np.where(is_propane, _rule_amount(prop_rule, df), rebate)
    return rebate

# HEAR federal stacking (matches subsidy_scenarios.py)
def _hear_supplement(df: pd.DataFrame, state_rebate: np.ndarray,
                     hear_shares: Dict[str, Dict[str, float]]) -> np.ndarray:
    hp  = df["hp_retail"].to_numpy()
    fuel = df["fuel"].to_numpy()
    low_frac = np.array([hear_shares.get(f, hear_shares["Gas"])["low"] for f in fuel])
    mod_frac = np.array([hear_shares.get(f, hear_shares["Gas"])["mod"] for f in fuel])
    hear_low = np.minimum(8000.0, np.maximum(0.0, hp * 1.00 - state_rebate))
    hear_mod = np.minimum(4000.0, np.maximum(0.0, hp * 0.50 - state_rebate))
    return low_frac * hear_low + mod_frac * hear_mod

def _federal_rebate(scenario: Dict[str, Any], df: pd.DataFrame,
                    state_rebate: np.ndarray,
                    hear_shares: Dict[str, Dict[str, float]]) -> np.ndarray:
    fed = scenario.get("federal", {"mode": "none"})
    mode = fed.get("mode", "none")
    if mode == "flat":
        return np.full(len(df), float(fed.get("flat", 0.0)))
    if mode == "hear":
        return _hear_supplement(df, state_rebate, hear_shares)
    return np.zeros(len(df))

# ─────────────────────────────────────────────────────────────────────────────
# Core computation: one scenario → metrics dict
# ─────────────────────────────────────────────────────────────────────────────
def run_scenario(
    df: pd.DataFrame,
    constants: Dict[str, Any],
    scenario: Dict[str, Any],
) -> Dict[str, Any]:
    """Run one scenario over the CAMA sample. Returns a metrics dict."""
    logit   = constants["logit"]
    totals  = constants["totals"]
    model   = constants["model"]
    hear    = constants["hear"]

    B_CONST   = logit["B_CONST"]
    B_COSTGAP = logit["B_COSTGAP"]
    B_PRIOR   = logit["B_PRIOR"]
    PRIOR_HP  = logit["PRIOR_HP_RATE"]
    SCALE     = totals["SCALE"]
    HP_LIFETIME = model["HP_LIFETIME"]
    P_CHOOSE_MAX = model["P_CHOOSE_MAX"]

    state = _state_rebate(scenario, df)
    fed   = _federal_rebate(scenario, df, state, hear)
    total_reb = state + fed

    # 10-year cost gap (in $K)
    nc_hp = df["hp_retail"].to_numpy() - total_reb - df["save10y_hp"].to_numpy()
    nc_ff = df["ff_retail"].to_numpy() - df["save10y_curr"].to_numpy()
    gap_k = (nc_hp - nc_ff) / 1000.0

    # P(choose HP | replacement) — capped
    eta = B_CONST + B_COSTGAP * gap_k + B_PRIOR * PRIOR_HP
    p_choose = np.minimum(expit(eta), P_CHOOSE_MAX)

    # P(adopt in 5yr) = P(replace) × P(choose)
    p_replace = df["p_replace"].to_numpy()
    p_adopt = p_replace * p_choose

    co2_yr_fuel = df["co2_yr_fuel"].to_numpy()
    co2_yr_fs   = df["co2_yr_fs"].to_numpy()
    res_20yr    = df["resource_20yr"].to_numpy()
    fuel = df["fuel"].to_numpy()

    total_adopt = p_adopt.sum() * SCALE
    prog_cost   = (p_adopt * total_reb).sum() * SCALE
    state_cost  = (p_adopt * state).sum() * SCALE
    fed_cost    = (p_adopt * fed).sum() * SCALE

    if p_adopt.sum() > 0:
        mean_rebate       = (p_adopt * total_reb).sum() / p_adopt.sum()
        mean_state_rebate = (p_adopt * state).sum()     / p_adopt.sum()
        mean_fed_rebate   = (p_adopt * fed).sum()       / p_adopt.sum()
    else:
        mean_rebate = mean_state_rebate = mean_fed_rebate = 0.0

    co2_ann      = (p_adopt * co2_yr_fuel).sum() * SCALE
    co2_lifetime = co2_ann * HP_LIFETIME
    prog_per_ton = prog_cost / co2_lifetime if co2_lifetime > 0 else float("nan")

    co2_lifetime_fs = (p_adopt * co2_yr_fs).sum() * SCALE * HP_LIFETIME
    total_resource  = (p_adopt * res_20yr).sum() * SCALE
    res_per_ton     = total_resource / co2_lifetime_fs if co2_lifetime_fs > 0 else float("nan")

    by_fuel = {}
    for f in ("Oil", "Gas", "Propane"):
        mask = fuel == f
        n_adopt = p_adopt[mask].sum() * SCALE
        n_cost  = (p_adopt[mask] * total_reb[mask]).sum() * SCALE
        n_co2   = (p_adopt[mask] * co2_yr_fuel[mask]).sum() * SCALE
        by_fuel[f] = dict(adopt=float(n_adopt), cost=float(n_cost), co2_ann=float(n_co2))

    return dict(
        total_adopt=float(total_adopt),
        prog_cost=float(prog_cost),
        state_cost=float(state_cost),
        fed_cost=float(fed_cost),
        mean_rebate=float(mean_rebate),
        mean_state_rebate=float(mean_state_rebate),
        mean_fed_rebate=float(mean_fed_rebate),
        co2_ann=float(co2_ann),
        co2_lifetime=float(co2_lifetime),
        prog_per_ton=float(prog_per_ton),
        res_per_ton=float(res_per_ton),
        total_resource=float(total_resource),
        by_fuel=by_fuel,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Paper preset scenarios (Table 5) — reproduced exactly
# ─────────────────────────────────────────────────────────────────────────────
PRESETS: Dict[str, Dict[str, Any]] = {
    "a": {
        "label": "(a) IRA era",
        "desc":  "Prior regime. State $750/cap-ton (no cap) + $2K federal 25C.",
        "oil":     {"per_cap_ton": 750, "cap": None},
        "gas":     {"per_cap_ton": 750, "cap": None},
        "propane": {"per_cap_ton": 750, "cap": None},
        "federal": {"mode": "flat", "flat": 2000},
    },
    "b": {
        "label": "(b) 2026 SQ",
        "desc":  "Current status quo. State $1K/cap-ton up to $10K, no federal.",
        "oil":     {"per_cap_ton": 1000, "cap": 10000},
        "gas":     {"per_cap_ton": 1000, "cap": 10000},
        "propane": {"per_cap_ton": 1000, "cap": 10000},
        "federal": {"mode": "none"},
    },
    "c": {
        "label": "(c) 40/30%",
        "desc":  "Fuel-differentiated %. Oil 40% up to $6K; gas 30% up to $3K.",
        "oil":     {"percent": 0.40, "cap": 6000},
        "gas":     {"percent": 0.30, "cap": 3000},
        "propane": {"percent": 0.40, "cap": 6000},
        "federal": {"mode": "none"},
    },
    "d": {
        "label": "(d) 50/40%",
        "desc":  "Higher fuel-differentiated %. Oil 50%/$8K; gas 40%/$4K.",
        "oil":     {"percent": 0.50, "cap": 8000},
        "gas":     {"percent": 0.40, "cap": 4000},
        "propane": {"percent": 0.50, "cap": 8000},
        "federal": {"mode": "none"},
    },
    "e": {
        "label": "(e) 40%/$250",
        "desc":  "% for oil, token flat for gas. Oil 40%/$6K; gas $250/cap-ton ≤$1K.",
        "oil":     {"percent": 0.40, "cap": 6000},
        "gas":     {"per_cap_ton": 250, "cap": 1000},
        "propane": {"percent": 0.40, "cap": 6000},
        "federal": {"mode": "none"},
    },
    "f": {
        "label": "(f) 50%/$250",
        "desc":  "Higher oil %. Oil 50%/$8K; gas $250/cap-ton ≤$1K.",
        "oil":     {"percent": 0.50, "cap": 8000},
        "gas":     {"per_cap_ton": 250, "cap": 1000},
        "propane": {"percent": 0.50, "cap": 8000},
        "federal": {"mode": "none"},
    },
    "g": {
        "label": "(g) $2K/$1K",
        "desc":  "Flat per cap-ton, fuel-differentiated. Oil $2K/cap-ton ≤$6K; gas $1K/cap-ton ≤$3K.",
        "oil":     {"per_cap_ton": 2000, "cap": 6000},
        "gas":     {"per_cap_ton": 1000, "cap": 3000},
        "propane": {"per_cap_ton": 2000, "cap": 6000},
        "federal": {"mode": "none"},
    },
    "h": {
        "label": "(h) $2K/$0",
        "desc":  "Oil-only. Oil $2K/cap-ton ≤$6K; no gas rebate.",
        "oil":     {"per_cap_ton": 2000, "cap": 6000},
        "gas":     None,
        "propane": {"per_cap_ton": 2000, "cap": 6000},
        "federal": {"mode": "none"},
    },
}

def run_all_presets(df: pd.DataFrame, constants: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Run all paper presets and return a dict keyed by preset id."""
    return {sid: run_scenario(df, constants, preset) for sid, preset in PRESETS.items()}
