"""
Heat Pump Incentive Scenario Explorer
=====================================
Companion tool to the white paper
"Not All Heat Pump Conversions Are Equal: Fuel-Differentiated Incentives and
the Cost of Decarbonizing Connecticut Homes" (Scruggs, Ouimet, and Bonitz,
April 2026).

Streamlit entry point. Run locally with:
    streamlit run app.py
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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
# Sidebar CSS — italic section headers, indentation for fuel blocks
# ─────────────────────────────────────────────────────────────────────────────
SIDEBAR_CSS = """
<style>
.section-header {
    font-style: italic;
    font-weight: 600;
    font-size: 1.02rem;
    margin-top: 0.75rem;
    margin-bottom: 0.25rem;
    border-bottom: 1px solid rgba(128,128,128,0.25);
    padding-bottom: 2px;
}
.fuel-header {
    font-weight: 600;
    margin-left: 0.75rem;
    margin-top: 0.5rem;
    margin-bottom: 0.1rem;
}
.fuel-body { margin-left: 1.5rem; }
</style>
"""

# ─────────────────────────────────────────────────────────────────────────────
# Per-fuel rebate widget (must render inside a `with st.sidebar:` block)
# ─────────────────────────────────────────────────────────────────────────────
def _rule_widget(fuel_label: str, default_type: str,
                 default_rate: float, default_cap: float,
                 key_prefix: str) -> Optional[Dict[str, Any]]:
    rtype_options = ["None", "$/cap-ton (with cap)", "% of HP cost (with cap)"]
    rtype = st.radio(
        f"{fuel_label} rebate structure",
        rtype_options,
        index=rtype_options.index(default_type),
        key=f"{key_prefix}_type",
    )
    if rtype == "None":
        return None
    # Stacked inputs (not side-by-side)
    if rtype == "$/cap-ton (with cap)":
        rate = st.number_input(
            "$ per cap-ton", min_value=0.0, max_value=10000.0, step=250.0,
            value=float(default_rate) if default_rate else 2000.0,
            key=f"{key_prefix}_rate_flat",
            help="$ per ton of HVAC heating capacity (≈12,000 BTU/hr).",
        )
        cap = st.number_input(
            "Dollar cap", min_value=0.0, max_value=20000.0, step=500.0,
            value=float(default_cap),
            key=f"{key_prefix}_cap_flat",
        )
        return {"per_cap_ton": rate, "cap": cap if cap > 0 else None}
    # percent
    rate_pct = st.number_input(
        "% of HP retail cost", min_value=0.0, max_value=100.0, step=5.0,
        value=float(default_rate) if default_rate else 40.0,
        key=f"{key_prefix}_rate_pct",
    )
    cap = st.number_input(
        "Dollar cap", min_value=0.0, max_value=20000.0, step=500.0,
        value=float(default_cap),
        key=f"{key_prefix}_cap_pct",
    )
    return {"percent": rate_pct / 100.0, "cap": cap if cap > 0 else None}

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar composition
# ─────────────────────────────────────────────────────────────────────────────
def build_custom_scenario() -> Dict[str, Any]:
    with st.sidebar:
        st.markdown(SIDEBAR_CSS, unsafe_allow_html=True)
        st.header("Build a rebate scenario")
        st.caption(
            "Define state rebate rules by fuel and an optional federal layer. "
            "Your scenario appears as the first column of the results table."
        )

        name = st.text_input("Scenario label", value="Your scenario", key="label")

        # ── STATE REBATE LAYER ────────────────────────────────────────────────
        st.markdown('<div class="section-header">State rebate layer</div>',
                    unsafe_allow_html=True)

        st.markdown('<div class="fuel-header">Oil (and propane by default)</div>',
                    unsafe_allow_html=True)
        with st.container():
            oil_rule = _rule_widget(
                "Oil / propane",
                default_type="% of HP cost (with cap)",
                default_rate=50.0, default_cap=8000.0,
                key_prefix="oil",
            )
            separate_propane = st.checkbox(
                "Use a different rule for propane",
                value=False, key="sep_propane",
            )

        if separate_propane:
            st.markdown('<div class="fuel-header">Propane</div>',
                        unsafe_allow_html=True)
            propane_rule = _rule_widget(
                "Propane",
                default_type="% of HP cost (with cap)",
                default_rate=50.0, default_cap=8000.0,
                key_prefix="propane",
            )
        else:
            propane_rule = oil_rule

        st.markdown('<div class="fuel-header">Natural gas</div>',
                    unsafe_allow_html=True)
        gas_rule = _rule_widget(
            "Natural gas",
            default_type="None",
            default_rate=0.0, default_cap=0.0,
            key_prefix="gas",
        )

        # ── FEDERAL LAYER ─────────────────────────────────────────────────────
        st.markdown('<div class="section-header">Federal layer</div>',
                    unsafe_allow_html=True)
        fed_mode = st.radio(
            "Federal incentive",
            ["None", "Flat federal ($)", "HEAR stacking (income-tiered)"],
            index=0,
            key="fed_mode",
            help=("HEAR stacks on top of the state rebate with income-based "
                  "caps (<80% AMI → up to $8K, 80–150% AMI → up to $4K, "
                  ">150% AMI → $0). Per-fuel income-tier shares come from the "
                  "survey."),
        )
        if fed_mode == "Flat federal ($)":
            fed_flat = st.number_input(
                "Flat federal per home",
                min_value=0.0, max_value=10000.0, step=500.0,
                value=2000.0, key="fed_flat",
            )
            fed = {"mode": "flat", "flat": fed_flat}
        elif fed_mode == "HEAR stacking (income-tiered)":
            fed = {"mode": "hear"}
        else:
            fed = {"mode": "none"}

    return {
        "label":   name or "Your scenario",
        "oil":     oil_rule,
        "gas":     gas_rule,
        "propane": propane_rule,
        "federal": fed,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Value formatting + row definitions
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_int(x):   return f"{x:,.0f}" if x == x else "—"
def _fmt_money(x): return f"${x:,.0f}" if x == x else "—"
def _fmt_mil(x):   return f"${x/1e6:.1f}M" if x == x else "—"

# (row label, formatter, class)  class = "primary" or "secondary"
ROW_DEFS = [
    ("5-year conversions (total)",
     lambda r: _fmt_int(r["total_adopt"]), "primary"),
    ("oil + propane",
     lambda r: _fmt_int(r["by_fuel"]["Oil"]["adopt"] + r["by_fuel"]["Propane"]["adopt"]),
     "secondary"),
    ("natural gas",
     lambda r: _fmt_int(r["by_fuel"]["Gas"]["adopt"]), "secondary"),
    ("Annual CO₂ savings (tons/yr)",
     lambda r: _fmt_int(r["co2_ann"]), "primary"),
    ("Mean rebate per conversion",
     lambda r: _fmt_money(r["mean_rebate"]), "secondary"),
    ("Total program cost (5yr)",
     lambda r: _fmt_mil(r["prog_cost"]), "primary"),
    ("Program $/ton CO₂ (20yr life)",
     lambda r: _fmt_money(r["prog_per_ton"]), "primary"),
    ("Total resource $/ton CO₂ (20yr)",
     lambda r: _fmt_money(r["res_per_ton"]), "primary"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Table CSS — theme-agnostic via `opacity` and `currentColor`
# ─────────────────────────────────────────────────────────────────────────────
TABLE_CSS = """
<style>
.hp-table {
    border-collapse: collapse;
    width: 100%;
    font-family: inherit;
    margin-top: 0.5em;
    /* Inherit the page's base text color so we stay readable in any theme. */
    color: inherit;
}
.hp-table th, .hp-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(128,128,128,0.25);
    vertical-align: baseline;
    white-space: nowrap;
}
.hp-table thead th {
    font-weight: 600;
    font-size: 0.88rem;
    text-align: right;
    border-bottom: 2px solid rgba(128,128,128,0.5);
}
.hp-table thead th:first-child {
    text-align: left;
    min-width: 240px;
}
.hp-table td { text-align: right; }
.hp-table td:first-child { text-align: left; }

/* Custom column: warm highlight, full opacity, slightly bolder */
.hp-table .custom-col {
    background: rgba(251, 191, 36, 0.14);
}
.hp-table thead th.custom-col {
    background: rgba(251, 191, 36, 0.28);
    font-weight: 700;
}

/* Preset columns: same typography, slightly recessed via opacity
   (works in both light and dark themes) */
.hp-table tbody td:not(.custom-col) {
    opacity: 0.72;
}
.hp-table thead th:not(.custom-col):not(:first-child) {
    opacity: 0.82;
}

/* Primary rows: bolder, slightly larger, more vertical space */
.hp-table tr.primary td {
    font-size: 1.00rem;
    font-weight: 600;
    padding-top: 11px;
    padding-bottom: 11px;
}
.hp-table tr.primary td:first-child {
    font-weight: 700;
}

/* Secondary rows: smaller, italic, indented, slightly lighter */
.hp-table tr.secondary td {
    font-size: 0.86rem;
    font-weight: 400;
    padding-top: 4px;
    padding-bottom: 4px;
}
.hp-table tr.secondary td:first-child {
    padding-left: 32px;
    font-style: italic;
}

/* Preserve custom-column emphasis even on secondary rows */
.hp-table tbody tr.primary td.custom-col {
    font-weight: 700;
    opacity: 1;
}
.hp-table tbody tr.secondary td.custom-col {
    opacity: 1;
}
</style>
"""

def _render_table(preset_res: Dict[str, Any], custom_res: Dict[str, Any],
                  custom_label: str) -> str:
    col_ids     = ["custom"] + list(PRESETS.keys())
    col_headers = [custom_label] + [PRESETS[s]["label"] for s in PRESETS]
    col_classes = ["custom-col"] + [""] * len(PRESETS)

    head_cells = "".join(
        f'<th class="{cls}">{hdr}</th>'
        for cls, hdr in zip(col_classes, col_headers)
    )
    thead = f"<thead><tr><th></th>{head_cells}</tr></thead>"

    rows_html = []
    for label, fn, row_cls in ROW_DEFS:
        cells = []
        for sid, col_cls in zip(col_ids, col_classes):
            r = custom_res if sid == "custom" else preset_res[sid]
            cells.append(f'<td class="{col_cls}">{fn(r)}</td>')
        rows_html.append(
            f'<tr class="{row_cls}"><td>{label}</td>{"".join(cells)}</tr>'
        )

    tbody = f"<tbody>{''.join(rows_html)}</tbody>"
    return f"{TABLE_CSS}<table class='hp-table'>{thead}{tbody}</table>"

# ─────────────────────────────────────────────────────────────────────────────
# Extrapolation warning
# ─────────────────────────────────────────────────────────────────────────────
def _extrapolation_warnings(scenario: Dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    def check(rule: Optional[Dict[str, Any]], fuel: str) -> None:
        if rule is None:
            return
        pct = rule.get("percent", 0.0) or 0.0
        pct_ton = rule.get("per_cap_ton", 0.0) or 0.0
        if pct > 0.75:
            warnings.append(
                f"{fuel}: a {pct:.0%} rebate is well outside the range tested "
                "in the paper — treat as extrapolation."
            )
        if pct_ton > 3000:
            warnings.append(
                f"{fuel}: ${pct_ton:,.0f}/cap-ton exceeds the paper's highest "
                "scenario ($2,000/cap-ton) — treat as extrapolation."
            )
    check(scenario.get("oil"), "Oil/propane")
    if scenario.get("propane") is not scenario.get("oil"):
        check(scenario.get("propane"), "Propane")
    check(scenario.get("gas"), "Natural gas")
    return warnings

# ─────────────────────────────────────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────────────────────────────────────
st.title("CT Heat Pump Incentive Scenario Explorer")
st.markdown(
    "Companion tool to the draft white paper *Not All Heat Pump Conversions "
    "Are Equal: Fuel-Differentiated Incentives and the Cost of Decarbonizing "
    "Connecticut Homes* (Scruggs, Ouimet, and Bonitz, April 2026). "
    "Define a state rebate structure in the sidebar and compare it to the "
    "paper's preset scenarios on the same set of metrics as Table 6."
)

custom = build_custom_scenario()

df, constants = get_data()
preset_res  = get_preset_results()
custom_res  = run_scenario(df, constants, custom)

for w in _extrapolation_warnings(custom):
    st.warning(w)

st.markdown("### Projected statewide 5-year outcomes")
st.markdown(
    _render_table(preset_res, custom_res, custom["label"] or "Your scenario"),
    unsafe_allow_html=True,
)

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
        "natural gas": custom["gas"],
        "federal": custom["federal"],
    })
    col2.markdown("**Paper preset definitions**")
    col2.write({sid: PRESETS[sid]["desc"] for sid in PRESETS})

with st.expander("Methodology and caveats"):
    st.markdown(
        "**Engine.** The scenario engine reproduces the paper's Table 6 to "
        "rounding. For each home in a 16,300-home modelable subset of a "
        "20,000 CT single-family CAMA sample, the engine computes the net "
        "10-year cost gap between an air-source heat pump (after state + "
        "federal rebates) and a like-for-like fossil fuel replacement, then "
        "applies a logit estimated on survey respondents with systems aged "
        "15+ years to obtain P(choose HP | replacing). Five-year adoption "
        "probability = P(replace in 5yr | fuel) × P(choose HP). Totals scale "
        "to 882,000 CT single-family homes."
    )
    st.markdown(
        "**Caveats.**\n"
        "- The logit was estimated at prevailing 2025 fuel and electricity "
        "prices. Extrapolating to very different price regimes (e.g., large "
        "oil price shocks) should be treated as indicative.\n"
        "- A cap of 85% is applied to the per-home choice probability to "
        "reflect residual non-adoption regardless of economics.\n"
        "- The HEAR stacking layer uses survey-derived income-tier shares by "
        "fuel and assumes the IRA HEAR program is implemented as written.\n"
        "- Equipment-cost and fuel-use formulas mirror the Qualtrics survey "
        "instrument; alternative assumptions will shift the levels but "
        "rarely the ordering."
    )

st.caption(
    "Source: subsidy_scenarios.py (paper engine). This app is a draft; "
    "please do not cite without permission. Questions → Lyle Scruggs, "
    "lyle.scruggs@uconn.edu."
)
