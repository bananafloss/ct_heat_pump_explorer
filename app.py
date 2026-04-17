"""
Heat Pump Incentive Scenario Explorer
=====================================
A companion tool to the white paper "Not All Heat Pump Conversions Are Equal:
Fuel-Differentiated Incentives and the Cost of Decarbonizing Connecticut Homes"
(Scruggs, Ouimet, and Bonitz, April 2026).

Streamlit entry point. Run locally with:
    streamlit run app.py
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

from engine import PRESETS, load_cama, load_constants, run_all_presets, run_scenario

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CT Heat Pump Incentive Explorer",
    page_icon=None,
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
# Cached data loaders
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def get_data():
    return load_cama(), load_constants()

@st.cache_data(show_spinner=False)
def get_preset_results():
    df, c = get_data()
    return run_all_presets(df, c)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: build a custom scenario
# ─────────────────────────────────────────────────────────────────────────────
def _rule_widget(fuel_label: str, default_type: str = "None",
                 default_rate: float = 0.0, default_cap: float = 6000.0,
                 key_prefix: str = "") -> Optional[Dict[str, Any]]:
    """Render a per-fuel rule block and return the rule dict (or None)."""
    rtype = st.radio(
        f"{fuel_label} rebate structure",
        ["None", "$/cap-ton (with cap)", "% of HP cost (with cap)"],
        index=["None", "$/cap-ton (with cap)", "% of HP cost (with cap)"].index(default_type),
        key=f"{key_prefix}_type",
        horizontal=True,
    )
    if rtype == "None":
        return None
    col1, col2 = st.columns(2)
    if rtype == "$/cap-ton (with cap)":
        rate = col1.number_input(
            "$ per cap-ton", min_value=0.0, max_value=10000.0, step=250.0,
            value=float(default_rate) if default_rate else 2000.0,
            key=f"{key_prefix}_rate_flat",
            help="$ per ton of HVAC heating capacity (≈12,000 BTU/hr).",
        )
        cap = col2.number_input(
            "Dollar cap", min_value=0.0, max_value=20000.0, step=500.0,
            value=float(default_cap),
            key=f"{key_prefix}_cap_flat",
        )
        return {"per_cap_ton": rate, "cap": cap if cap > 0 else None}
    # percent
    rate_pct = col1.number_input(
        "% of HP retail cost", min_value=0.0, max_value=100.0, step=5.0,
        value=float(default_rate) if default_rate else 40.0,
        key=f"{key_prefix}_rate_pct",
    )
    cap = col2.number_input(
        "Dollar cap", min_value=0.0, max_value=20000.0, step=500.0,
        value=float(default_cap),
        key=f"{key_prefix}_cap_pct",
    )
    return {"percent": rate_pct / 100.0, "cap": cap if cap > 0 else None}

def build_custom_scenario() -> Dict[str, Any]:
    st.sidebar.header("Build a scenario")
    st.sidebar.caption(
        "Define state rebate rules by fuel and an optional federal layer. "
        "Results appear in the right-most column of the table."
    )

    st.sidebar.subheader("Oil (and propane by default)")
    oil_rule = _rule_widget("Oil / propane", default_type="% of HP cost (with cap)",
                            default_rate=50.0, default_cap=8000.0,
                            key_prefix="oil")

    separate_propane = st.sidebar.checkbox(
        "Use a different rule for propane", value=False, key="sep_propane"
    )
    if separate_propane:
        st.sidebar.markdown("**Propane**")
        propane_rule = _rule_widget("Propane", default_type="% of HP cost (with cap)",
                                    default_rate=50.0, default_cap=8000.0,
                                    key_prefix="propane")
    else:
        propane_rule = oil_rule

    st.sidebar.subheader("Gas")
    gas_rule = _rule_widget("Gas", default_type="None",
                            default_rate=0.0, default_cap=0.0,
                            key_prefix="gas")

    st.sidebar.subheader("Federal layer")
    fed_mode = st.sidebar.radio(
        "Federal incentive",
        ["None", "Flat federal ($)", "HEAR stacking (income-tiered)"],
        index=0,
        key="fed_mode",
        help=("HEAR stacks on top of the state rebate with income-based caps "
              "(<80% AMI → up to $8K, 80–150% AMI → up to $4K, >150% AMI → $0). "
              "Per-fuel income-tier shares come from the survey."),
    )
    if fed_mode == "Flat federal ($)":
        fed_flat = st.sidebar.number_input(
            "Flat federal per home", min_value=0.0, max_value=10000.0, step=500.0,
            value=2000.0, key="fed_flat",
        )
        fed = {"mode": "flat", "flat": fed_flat}
    elif fed_mode == "HEAR stacking (income-tiered)":
        fed = {"mode": "hear"}
    else:
        fed = {"mode": "none"}

    name = st.sidebar.text_input("Scenario label", value="Your scenario", key="label")

    return {
        "label":   name,
        "oil":     oil_rule,
        "gas":     gas_rule,
        "propane": propane_rule,
        "federal": fed,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Results table formatting
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_int(x):   return f"{x:,.0f}" if x == x else "—"
def _fmt_money(x): return f"${x:,.0f}" if x == x else "—"
def _fmt_mil(x):   return f"${x/1e6:.1f}M" if x == x else "—"

ROW_DEFS = [
    ("5-year conversions (total)",
     lambda r: _fmt_int(r["total_adopt"])),
    ("  · oil + propane",
     lambda r: _fmt_int(r["by_fuel"]["Oil"]["adopt"] + r["by_fuel"]["Propane"]["adopt"])),
    ("  · gas",
     lambda r: _fmt_int(r["by_fuel"]["Gas"]["adopt"])),
    ("Annual CO₂ savings (tons/yr)",
     lambda r: _fmt_int(r["co2_ann"])),
    ("Total program cost (5yr)",
     lambda r: _fmt_mil(r["prog_cost"])),
    ("Mean rebate per conversion",
     lambda r: _fmt_money(r["mean_rebate"])),
    ("Program $/ton CO₂ (20yr life)",
     lambda r: _fmt_money(r["prog_per_ton"])),
    ("Total resource $/ton CO₂ (20yr)",
     lambda r: _fmt_money(r["res_per_ton"])),
]

def results_table(preset_res: Dict[str, Any], custom_res: Dict[str, Any],
                  custom_label: str) -> pd.DataFrame:
    col_ids    = list(PRESETS.keys()) + ["custom"]
    col_labels = [PRESETS[s]["label"] for s in PRESETS] + [custom_label]
    data = {
        col_labels[i]: [fn(preset_res[sid] if sid != "custom" else custom_res)
                        for _, fn in ROW_DEFS]
        for i, sid in enumerate(col_ids)
    }
    return pd.DataFrame(data, index=[label for label, _ in ROW_DEFS])

# ─────────────────────────────────────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────────────────────────────────────
st.title("CT Heat Pump Incentive Scenario Explorer")
st.markdown(
    "Companion tool to the draft white paper *Not All Heat Pump Conversions Are Equal: "
    "Fuel-Differentiated Incentives and the Cost of Decarbonizing Connecticut Homes* "
    "(Scruggs, Ouimet, and Bonitz, April 2026). "
    "Define a state rebate structure in the sidebar and compare it to the paper's preset "
    "scenarios on the same set of metrics as Table 6."
)

custom = build_custom_scenario()

df, constants = get_data()
preset_res  = get_preset_results()
custom_res  = run_scenario(df, constants, custom)

st.markdown("### Projected statewide 5-year outcomes")
table = results_table(preset_res, custom_res, custom["label"] or "Your scenario")
st.dataframe(table, width="stretch")

# ─────────────────────────────────────────────────────────────────────────────
# Detail / explanation panel
# ─────────────────────────────────────────────────────────────────────────────
with st.expander("Scenario summary"):
    col1, col2 = st.columns(2)
    col1.markdown(f"**Your scenario — {custom['label']}**")
    col1.write({
        "oil / propane": custom["oil"],
        "propane (if separate)": (
            custom["propane"] if custom["propane"] is not custom["oil"] else "same as oil"
        ),
        "gas": custom["gas"],
        "federal": custom["federal"],
    })
    col2.markdown("**Paper preset definitions**")
    col2.write({sid: PRESETS[sid]["desc"] for sid in PRESETS})

with st.expander("Methodology and caveats"):
    st.markdown(
        "**Engine.** The scenario engine reproduces the paper's Table 6 to rounding. "
        "For each home in a 16,300-home modelable subset of a 20,000 CT single-family "
        "CAMA sample, the engine computes the net 10-year cost gap between an air-source "
        "heat pump (after state + federal rebates) and a like-for-like fossil fuel "
        "replacement, then applies a logit estimated on survey respondents with "
        "systems aged 15+ years to obtain P(choose HP | replacing). Five-year adoption "
        "probability = P(replace in 5yr | fuel) × P(choose HP). Totals scale to 882,000 "
        "CT single-family homes."
    )
    st.markdown(
        "**Caveats.**\n"
        "- The logit was estimated at prevailing 2025 fuel and electricity prices. "
        "Extrapolating to very different price regimes (e.g., large oil price shocks) "
        "should be treated as indicative.\n"
        "- A cap of 85% is applied to the per-home choice probability to reflect "
        "residual non-adoption regardless of economics.\n"
        "- The HEAR stacking layer uses survey-derived income-tier shares by fuel "
        "and assumes the IRA HEAR program is implemented as written.\n"
        "- Equipment-cost and fuel-use formulas mirror the Qualtrics survey instrument; "
        "alternative assumptions will shift the levels but rarely the ordering."
    )

st.caption(
    "Source: subsidy_scenarios.py (paper engine). This app is a draft; please do not "
    "cite without permission. Questions → Lyle Scruggs, lyle.scruggs@uconn.edu."
)
