#!/usr/bin/env python3
"""
prepare_data.py
===============
One-time data preparation for the Streamlit heat pump scenario app.

Reads the raw files used by the EEC white paper engine
(subsidy_scenarios.py / eec25_white_paper_integrated.py):

  - ../eec25_analysis_ready.csv   (survey, used to fit the logit)
  - ../new_20k_address.dta        (CAMA sample, used for population projection)

Writes two artifacts committed with the app repo:

  - app_data/cama_minimal.csv     (one row per home, no addresses/PII)
  - app_data/constants.json       (logit coefs, CO2 rates, scaling factors)

Run whenever the paper engine is updated so the app stays in sync:

  python prepare_data.py
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
HERE      = Path(__file__).resolve().parent
EEC_ROOT  = HERE.parent
SURVEY_FP = EEC_ROOT / "eec25_analysis_ready.csv"
CAMA_FP   = EEC_ROOT / "new_20k_address.dta"
OUT_DIR   = HERE / "app_data"
OUT_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Constants (mirrored from subsidy_scenarios.py — keep in sync)
# ─────────────────────────────────────────────────────────────────────────────
P_REPLACE = {"Oil": 0.464, "Gas": 0.293, "Propane": 0.180}

CO2_BY_FUEL = {"Oil": 4.59, "Gas": 1.43, "Propane": 3.73}

CO2_BY_FUEL_SYS = {
    ("Oil",     "Boiler"):  5.25,
    ("Oil",     "Furnace"): 4.05,
    ("Gas",     "Boiler"):  1.79,
    ("Gas",     "Furnace"): 1.10,
    ("Propane", "Boiler"):  4.27,
    ("Propane", "Furnace"): 3.29,
}

# 10-year fuel cost multipliers (from Qualtrics instrument)
# (fuel, system) -> (replacement_10y_mult, hp_10y_mult)
COST_MULTS = {
    ("Oil",     "Boiler"):  (2.80,  4.90),
    ("Oil",     "Furnace"): (3.33,  3.20),
    ("Gas",     "Boiler"):  (3.00, -3.20),
    ("Gas",     "Furnace"): (3.60, -4.60),
    ("Propane", "Boiler"):  (2.80,  4.90),
    ("Propane", "Furnace"): (3.33,  3.20),
}

TOTAL_CT_SF = 882_000
CAMA_N      = 20_000
SCALE       = TOTAL_CT_SF / CAMA_N        # 44.1
HP_LIFETIME = 20                           # years
P_CHOOSE_MAX = 0.85

# HEAR income-tier shares by fuel (geography-adjusted AMI)
HEAR_SHARES = {
    "Oil":     {"low": 0.306, "mod": 0.343, "inel": 0.351},
    "Propane": {"low": 0.306, "mod": 0.343, "inel": 0.351},
    "Gas":     {"low": 0.210, "mod": 0.222, "inel": 0.568},
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Fit the logit from the survey
# ─────────────────────────────────────────────────────────────────────────────
def fit_logit():
    df = pd.read_csv(SURVEY_FP)

    nonadopt = df[df["would_switch"].notna()].copy()
    nonadopt["saw_error"] = (
        nonadopt["netcost_curr"].astype(str).str.contains("Invalid", na=False)
    )
    treated = nonadopt[
        (nonadopt["got_treatment"] == 1) & (~nonadopt["saw_error"])
    ].copy()

    for col in ["netcost_curr", "netcost_hp"]:
        treated[col] = pd.to_numeric(treated[col], errors="coerce")
    treated["cost_gap_k"] = (treated["netcost_hp"] - treated["netcost_curr"]) / 1000
    treated["prior_hp"]   = treated["would_switch"]

    age_map = {
        "<2 years":   1.0, "2-4 years":   3.0, "5-9 years":   7.0,
        "10-14 years": 12.0, "15-19 years": 17.0, "20+ years":  22.0,
    }
    treated["age_mid"] = treated["heat1_age"].map(age_map).fillna(
        pd.to_numeric(treated.get("heat1_age", pd.Series(dtype=float)), errors="coerce")
    )

    mod_df = treated[
        ["choice_info", "cost_gap_k", "prior_hp", "clean_weights", "age_mid"]
    ].dropna(subset=["choice_info", "cost_gap_k", "prior_hp", "clean_weights"])

    old_df = mod_df[mod_df["age_mid"] >= 15].copy()

    X = sm.add_constant(old_df[["cost_gap_k", "prior_hp"]])
    mod = sm.GLM(
        old_df["choice_info"], X,
        family=sm.families.Binomial(),
        freq_weights=old_df["clean_weights"],
    ).fit()

    coefs = {
        "B_CONST":   float(mod.params["const"]),
        "B_COSTGAP": float(mod.params["cost_gap_k"]),
        "B_PRIOR":   float(mod.params["prior_hp"]),
        "PRIOR_HP_RATE": float(old_df["prior_hp"].mean()),
        "n_obs":      int(len(old_df)),
        "n_events":   int(old_df["choice_info"].sum()),
    }
    print(
        f"Logit fit: n={coefs['n_obs']} events={coefs['n_events']} "
        f"const={coefs['B_CONST']:.4f} cost_gap_k={coefs['B_COSTGAP']:.4f} "
        f"prior={coefs['B_PRIOR']:.4f} prior_rate={coefs['PRIOR_HP_RATE']:.4f}"
    )
    return coefs

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Build the minimal CAMA table (no addresses/PII)
# ─────────────────────────────────────────────────────────────────────────────
def classify_fuel(v):
    v = str(v).lower()
    if "oil" in v:                   return "Oil"
    if "gas" in v or "natural" in v: return "Gas"
    if "propane" in v or "lp" in v:  return "Propane"
    if "elec" in v:                  return "Electric"
    return "Other"

def classify_system(v):
    v = str(v).lower()
    if "steam" in v or "hot water" in v: return "Boiler"
    if "furnace" in v:                   return "Furnace"
    if "heat pump" in v:                 return "Heat Pump"
    return "Other"

def sqft_to_sc(sqft):
    if pd.isna(sqft) or sqft <= 0: return np.nan
    if sqft < 1000:   return 1
    elif sqft <= 1500: return 2
    elif sqft <= 2000: return 3
    elif sqft <= 2500: return 4
    elif sqft <= 3000: return 5
    elif sqft <= 3500: return 6
    else:             return 7

def build_cama_minimal():
    cama = pd.read_stata(CAMA_FP, convert_categoricals=False)
    print(f"CAMA raw: {len(cama)} homes")

    cama["fuel"]   = cama["heatfueldescription"].str.strip().str.lower().map(classify_fuel)
    cama["system"] = cama["heatcode"].str.strip().str.lower().map(classify_system)

    m = cama[
        cama["fuel"].isin(["Oil", "Gas", "Propane"]) &
        cama["system"].isin(["Boiler", "Furnace"])
    ].copy()
    print(f"Modelable (Oil/Gas/Propane × Boiler/Furnace): {len(m)}")

    m["sqft"]      = m["livingarea"]
    m["size_calc"] = m["sqft"].apply(sqft_to_sc)
    m = m.dropna(subset=["size_calc"]).copy()
    m["size_calc"] = m["size_calc"].astype(float)

    # Retail cost formulas (from Qualtrics embedded data)
    m["hp_retail"]   = 7500 + (7500 - 250 * m["size_calc"]) * (m["size_calc"] - 1)
    m["boil_retail"] = 5000 + 1000 * m["size_calc"]
    m["furn_retail"] = 2500 + 1000 * m["size_calc"]
    m["ff_retail"]   = np.where(m["system"] == "Boiler", m["boil_retail"], m["furn_retail"])

    # Fuel use imputed from sqft
    m["fuel_use"] = 330 + 0.18 * m["sqft"] + np.where(m["system"] == "Boiler", 140, 0)

    def get_costs(row):
        rm, hm = COST_MULTS.get((row["fuel"], row["system"]), (np.nan, np.nan))
        fu = row["fuel_use"]
        return pd.Series({"save10y_curr": rm * fu, "save10y_hp": hm * fu})

    costs = m.apply(get_costs, axis=1)
    m["save10y_curr"] = costs["save10y_curr"]
    m["save10y_hp"]   = costs["save10y_hp"]

    # 20-year resource cost per home (no rebates, 0% discount)
    m["resource_20yr"] = (m["hp_retail"] - m["ff_retail"]) - 2 * (m["save10y_hp"] - m["save10y_curr"])

    m["co2_yr_fuel"] = m["fuel"].map(CO2_BY_FUEL)
    m["co2_yr_fs"]   = m.apply(
        lambda r: CO2_BY_FUEL_SYS.get(
            (r["fuel"], r["system"]),
            CO2_BY_FUEL.get(r["fuel"], np.nan),
        ),
        axis=1,
    )
    m["p_replace"] = m["fuel"].map(P_REPLACE)

    keep = [
        "fuel", "system", "size_calc", "sqft",
        "hp_retail", "ff_retail",
        "save10y_curr", "save10y_hp",
        "resource_20yr",
        "co2_yr_fuel", "co2_yr_fs",
        "p_replace",
    ]
    minimal = m[keep].reset_index(drop=True)

    # Round to keep the file small
    for col in ["hp_retail", "ff_retail", "save10y_curr", "save10y_hp", "resource_20yr"]:
        minimal[col] = minimal[col].round(0)
    for col in ["co2_yr_fuel", "co2_yr_fs"]:
        minimal[col] = minimal[col].round(3)
    minimal["size_calc"] = minimal["size_calc"].astype(int)
    minimal["sqft"]      = minimal["sqft"].astype(int)

    return minimal

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Write outputs
# ─────────────────────────────────────────────────────────────────────────────
def main():
    if not SURVEY_FP.exists():
        print(f"ERROR: survey file not found at {SURVEY_FP}", file=sys.stderr)
        sys.exit(1)
    if not CAMA_FP.exists():
        print(f"ERROR: CAMA file not found at {CAMA_FP}", file=sys.stderr)
        sys.exit(1)

    coefs = fit_logit()
    minimal = build_cama_minimal()

    csv_out = OUT_DIR / "cama_minimal.csv"
    minimal.to_csv(csv_out, index=False)
    print(f"Wrote {csv_out}  ({len(minimal)} rows, {csv_out.stat().st_size/1024:.0f} KB)")

    constants = {
        "logit":  coefs,
        "totals": {
            "TOTAL_CT_SF":   TOTAL_CT_SF,
            "CAMA_N":        CAMA_N,
            "SCALE":         SCALE,
        },
        "model": {
            "HP_LIFETIME":   HP_LIFETIME,
            "P_CHOOSE_MAX":  P_CHOOSE_MAX,
            "P_REPLACE":     P_REPLACE,
        },
        "co2":  {
            "CO2_BY_FUEL":     CO2_BY_FUEL,
            "CO2_BY_FUEL_SYS": {f"{f}|{s}": v for (f, s), v in CO2_BY_FUEL_SYS.items()},
        },
        "hear": HEAR_SHARES,
    }
    json_out = OUT_DIR / "constants.json"
    with open(json_out, "w") as f:
        json.dump(constants, f, indent=2)
    print(f"Wrote {json_out}")

if __name__ == "__main__":
    main()
