"""Pure helpers behind the Streamlit dashboard (``streamlit_app.py``).

Everything here is independent of Streamlit so it can be unit-tested: turning
sidebar controls into validated assumptions, building Altair charts, and rendering
the workbook to bytes for download.
"""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd

from src.analyzer import AnalysisResult, SensitivityTable, apply_overrides
from src.modeling_engine import LBOResult, ValuationModel
from src.schemas import ModelAssumptions

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_FILING = ROOT / "data" / "raw" / "apple_10k_fy2015.pdf"
SAMPLE_MEMO = ROOT / "outputs" / "memo.md"
ASSUMPTIONS_FILE = ROOT / "config" / "assumptions.json"
SCENARIOS_FILE = ROOT / "config" / "scenarios.json"

NAVY, BLUE, LIGHT = "#1F3864", "#2F5597", "#8EA9DB"


# --------------------------------------------------------------------------------------
# Sidebar controls -> assumptions
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Controls:
    """The assumptions a dashboard user can flex.

    Operating drivers are expressed as *shifts* applied to every projection year, so
    the base case's year-by-year shape is preserved.
    """

    entry_multiple: float
    exit_multiple: float
    debt_pct: float
    foreign_cash_usable_pct: float
    revenue_growth_shift: float
    gross_margin_shift: float
    risk_free_rate: float
    equity_risk_premium: float
    levered_beta: float
    terminal_growth_rate: float
    dcf_exit_multiple: float

    @classmethod
    def from_assumptions(cls, a: ModelAssumptions) -> Controls:
        """Controls matching an assumption set (zero shifts)."""
        return cls(
            entry_multiple=a.transaction.entry_ev_ebitda_multiple,
            exit_multiple=a.transaction.exit_ev_ebitda_multiple,
            debt_pct=a.transaction.debt_pct_of_entry_ev,
            foreign_cash_usable_pct=a.transaction.foreign_cash_usable_pct,
            revenue_growth_shift=0.0,
            gross_margin_shift=0.0,
            risk_free_rate=a.market.risk_free_rate,
            equity_risk_premium=a.market.equity_risk_premium,
            levered_beta=a.market.levered_beta,
            terminal_growth_rate=a.market.terminal_growth_rate,
            dcf_exit_multiple=a.market.exit_ev_ebitda_multiple,
        )

    def as_dict(self) -> dict[str, float]:
        """Plain dict (hashable via ``tuple(sorted(...))`` for caching)."""
        return asdict(self)


def apply_controls(base: ModelAssumptions, controls: Controls) -> ModelAssumptions:
    """Return ``base`` with the dashboard controls applied, fully re-validated.

    Raises:
        pydantic.ValidationError: If the combination is invalid (for example a margin
            shift that leaves gross margin below R&D + SG&A).
    """
    op = base.operating
    overrides: dict[str, Any] = {
        "operating": {
            "revenue_growth": [g + controls.revenue_growth_shift for g in op.revenue_growth],
            "gross_margin": [m + controls.gross_margin_shift for m in op.gross_margin],
        },
        "market": {
            "risk_free_rate": controls.risk_free_rate,
            "equity_risk_premium": controls.equity_risk_premium,
            "levered_beta": controls.levered_beta,
            "terminal_growth_rate": controls.terminal_growth_rate,
            "exit_ev_ebitda_multiple": controls.dcf_exit_multiple,
        },
        "transaction": {
            "entry_ev_ebitda_multiple": controls.entry_multiple,
            "exit_ev_ebitda_multiple": controls.exit_multiple,
            "debt_pct_of_entry_ev": controls.debt_pct,
            "foreign_cash_usable_pct": controls.foreign_cash_usable_pct,
        },
    }
    return apply_overrides(base, overrides)


# --------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------
def sources_uses_frame(lbo: LBOResult) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Uses and sources of funds in $B with % of total."""
    total = lbo.entry["total_uses"]

    def frame(items: dict[str, float], label: str) -> pd.DataFrame:
        # Percentages are in percent units (61.5, not 0.615) for fixed-decimal display.
        rows = [{label: k, "$B": v / 1_000, "% of total": 100 * v / total}
                for k, v in items.items()]
        rows.append({label: "Total", "$B": sum(items.values()) / 1_000,
                     "% of total": 100 * sum(items.values()) / total})
        return pd.DataFrame(rows)

    return frame(lbo.uses, "Uses"), frame(lbo.sources, "Sources")


def scenario_frame(analysis: AnalysisResult) -> pd.DataFrame:
    """Headline scenario outputs, one column per scenario, as display strings."""
    spec = [
        ("Revenue CAGR", "revenue_cagr", "{:.1%}"),
        ("Final-year EBITDA ($B)", "final_year_ebitda", "{:,.1f}", 1e-3),
        ("DCF price: Gordon Growth", "dcf_gordon_price", "${:,.2f}"),
        ("DCF price: Exit Multiple", "dcf_exit_price", "${:,.2f}"),
        ("Sponsor equity ($B)", "lbo_sponsor_equity", "{:,.1f}", 1e-3),
        ("Sponsor IRR", "lbo_irr", "{:.1%}"),
        ("Sponsor MOIC", "lbo_moic", "{:.2f}x"),
        ("Peak Debt / EBITDA", "peak_leverage", "{:.1f}x"),
        ("Min covenant EBITDA cushion", "min_coverage_cushion", "{:.0%}"),
    ]
    data = {}
    for r in analysis.scenarios:
        data[r.name] = [fmt.format(r.metrics[key] * (scale[0] if scale else 1))
                        for _, key, fmt, *scale in spec]
    return pd.DataFrame(data, index=[label for label, *_ in spec])


def covenant_frame(lbo: LBOResult) -> pd.DataFrame:
    """Credit statistics by year (cushions in percent units for fixed-decimal display)."""
    s = lbo.schedule
    return pd.DataFrame({
        "Total debt ($B)": s.loc["total_debt"] / 1_000,
        "Debt / EBITDA": s.loc["leverage"],
        "EBITDA / interest": s.loc["interest_coverage"],
        "Cushion: leverage covenant": 100 * s.loc["leverage_cushion"],
        "Cushion: coverage covenant": 100 * s.loc["coverage_cushion"],
    }).rename(index=lambda y: f"FY{y}")


# --------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------
_AXIS_FORMAT = {"pct": ".1%", "multiple": ".1f"}
_VALUE_FORMAT = {"price": "$,.2f", "pct": ".1%", "multiple": ".2f"}


def heatmap(table: SensitivityTable) -> alt.Chart:
    """Sensitivity grid as an annotated heat map; the base case is outlined."""
    rows = []
    for r in table.rows:
        for c in table.cols:
            v = float(table.values.at[r, c])
            rows.append({"row": r, "col": c, "value": v,
                         "base": bool(abs(r - table.base[0]) < 1e-9
                                      and abs(c - table.base[1]) < 1e-9)})
    df = pd.DataFrame(rows).dropna(subset=["value"])
    x = alt.X("col:O", title=table.col_label, sort="ascending",
              axis=alt.Axis(format=_AXIS_FORMAT[table.axis_formats[1]], labelAngle=0))
    y = alt.Y("row:O", title=table.row_label, sort="ascending",
              axis=alt.Axis(format=_AXIS_FORMAT[table.axis_formats[0]]))
    fmt = _VALUE_FORMAT[table.value_format]
    base = alt.Chart(df).encode(x=x, y=y)
    cells = base.mark_rect().encode(
        # Trim the scheme's darkest ends so black labels stay readable on every cell.
        color=alt.Color("value:Q", legend=None, scale=alt.Scale(
            scheme=alt.SchemeParams(name="redyellowgreen", extent=[0.15, 0.85]))),
        tooltip=[alt.Tooltip("row:O", title=table.row_label, format=_AXIS_FORMAT[table.axis_formats[0]]),
                 alt.Tooltip("col:O", title=table.col_label, format=_AXIS_FORMAT[table.axis_formats[1]]),
                 alt.Tooltip("value:Q", title="Value", format=fmt)])
    outline = base.transform_filter("datum.base").mark_rect(
        fill=None, stroke="black", strokeWidth=2.5)
    text = alt.Text("value:Q", format=fmt)
    labels = base.transform_filter("!datum.base").mark_text(fontSize=12).encode(text=text)
    base_label = base.transform_filter("datum.base").mark_text(
        fontSize=12, fontWeight="bold").encode(text=text)
    return (cells + outline + labels + base_label).properties(height=260, title=table.title)


def projection_chart(model: ValuationModel) -> alt.Chart:
    """Net sales and EBITDA, reported and projected, in $B."""
    s, p = model.schema, model.projection
    rows = []
    for y in s.years:
        rows += [{"year": f"FY{y}A", "metric": "Net sales", "value": s.income_statement(y).net_sales},
                 {"year": f"FY{y}A", "metric": "EBITDA", "value": s.ebitda(y)}]
    for y in p.years:
        rows += [{"year": f"FY{y}E", "metric": "Net sales", "value": p.line("income", "net_sales", y)},
                 {"year": f"FY{y}E", "metric": "EBITDA", "value": p.line("income", "ebitda", y)}]
    df = pd.DataFrame(rows).assign(value=lambda d: d["value"] / 1_000)
    return alt.Chart(df).mark_bar().encode(
        x=alt.X("year:N", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
        xOffset="metric:N",
        y=alt.Y("value:Q", title="$B"),
        color=alt.Color("metric:N", scale=alt.Scale(range=[NAVY, LIGHT]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["year", "metric", alt.Tooltip("value:Q", format=",.1f", title="$B")],
    ).properties(height=300)


def debt_paydown_chart(lbo: LBOResult) -> alt.Chart:
    """Debt outstanding by tranche (bars) with Debt / EBITDA (line)."""
    s = lbo.schedule
    rows = [{"year": f"FY{y}", "tranche": name,
             "balance": s.at[f"tranche{i}_ending", y] / 1_000}
            for y in s.columns for i, name in enumerate(lbo.tranche_names)]
    bars = alt.Chart(pd.DataFrame(rows)).mark_bar().encode(
        x=alt.X("year:N", title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("balance:Q", title="Debt outstanding ($B)"),
        color=alt.Color("tranche:N", scale=alt.Scale(range=[NAVY, LIGHT]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["year", "tranche", alt.Tooltip("balance:Q", format=",.1f", title="$B")])
    lev = pd.DataFrame({"year": [f"FY{y}" for y in s.columns],
                        "leverage": list(s.loc["leverage"])})
    line = alt.Chart(lev).mark_line(point=True, color="#C00000").encode(
        x="year:N",
        y=alt.Y("leverage:Q", title="Debt / EBITDA (x)", axis=alt.Axis(titleColor="#C00000")),
        tooltip=["year", alt.Tooltip("leverage:Q", format=".2f")])
    return alt.layer(bars, line).resolve_scale(y="independent").properties(height=300)


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------
def workbook_bytes(analysis: AnalysisResult) -> bytes:
    """Render the full workbook (with Phase 3 sheets) to ``.xlsx`` bytes."""
    from src.excel_export import write_workbook

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.xlsx"
        write_workbook(analysis.base.model, path, analysis=analysis)
        return path.read_bytes()
