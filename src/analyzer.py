"""Stress-testing, sensitivity analysis and investment recommendation.

Builds on the Phase 2 engine without changing it: every number here comes from
re-running :mod:`src.modeling_engine` with different inputs.

- **Scenarios**: Base / Bull / Bear defined as overrides on the base assumptions
  (``config/scenarios.json``), each run through the full projection, DCF and LBO.
- **Sensitivity matrices**: DCF share price for WACC x terminal growth and
  exit multiple x WACC; LBO IRR and MOIC for entry multiple x leverage.
- **Risk metrics**: covenant headroom by year, break-even exit and entry multiples
  for the hurdle IRR, terminal value concentration, and scenario variance vs. base.
- **Recommendation**: a rule-based Proceed / Pass verdict against explicit investment
  criteria. The memo generator (:mod:`src.memo_generator`) must agree with it.

Usage::

    python -m src.analyzer outputs/extracted/apple_inc_fy2015.json
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import Field, ValidationError

from src.exceptions import AssumptionError, FinancialDataError
from src.modeling_engine import (
    LBOResult,
    ValuationModel,
    build_model,
    load_schema,
    repatriation_tax_rate,
    run_dcf,
    run_lbo,
)
from src.schemas import FinancialStatementSchema, ModelAssumptions, StrictModel

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Configuration models
# --------------------------------------------------------------------------------------
class InvestmentCriteria(StrictModel):
    """Hurdles a deal must clear to be recommended."""

    target_irr: float = Field(gt=-1.0, lt=1.0)
    minimum_moic: float = Field(gt=0.0)
    require_base_case_covenant_compliance: bool = True
    sources: dict[str, str] = Field(default_factory=dict)


class ScenarioSpec(StrictModel):
    """A named scenario.

    ``overrides`` replace values outright (deep merge). ``shifts`` add to the base
    value and are keyed by dotted path, e.g. ``{"operating.revenue_growth": 0.02}``;
    a shift on a per-year list applies to every year. Shifts make a scenario generic,
    so the same Bull / Bear definitions work for any company.
    """

    name: str = Field(min_length=1)
    description: str
    overrides: dict[str, Any] = Field(default_factory=dict)
    shifts: dict[str, float] = Field(default_factory=dict)


class SensitivityGrids(StrictModel):
    """Axis definitions for the sensitivity matrices.

    Axes are centred on the base-case value and step outwards, so the tables stay
    centred when assumptions change. The LBO axes may instead be given as explicit
    lists (the Apple sample does this).
    """

    wacc_step: float = Field(gt=0.0)
    growth_step: float = Field(gt=0.0)
    dcf_exit_multiple_step: float = Field(gt=0.0)
    lbo_entry_multiples: list[float] = Field(default_factory=list)
    lbo_debt_pcts: list[float] = Field(default_factory=list)
    lbo_entry_multiple_step: float = Field(default=1.0, gt=0.0)
    lbo_debt_pct_step: float = Field(default=0.10, gt=0.0)
    steps_each_side: int = Field(default=2, ge=1, le=5)


class AnalysisConfig(StrictModel):
    """Contents of ``config/scenarios.json``."""

    investment_criteria: InvestmentCriteria
    scenarios: list[ScenarioSpec] = Field(min_length=1)
    sensitivity_grids: SensitivityGrids

    @classmethod
    def from_json(cls, path: str | Path) -> AnalysisConfig:
        """Load and validate the analysis configuration."""
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------
def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` into a copy of ``base``.

    Nested dicts are merged key by key; any other value (including lists) replaces
    the base value outright, so a scenario can redefine a whole driver path.
    """
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def apply_overrides(base: ModelAssumptions, overrides: dict[str, Any]) -> ModelAssumptions:
    """Return a new, fully re-validated assumption set with ``overrides`` applied.

    Raises:
        ValidationError: If the overrides produce invalid assumptions.
    """
    return ModelAssumptions.model_validate(deep_merge(base.model_dump(mode="json"), overrides))


def apply_shifts(base: ModelAssumptions, shifts: dict[str, float]) -> ModelAssumptions:
    """Add ``shifts`` (dotted paths) to the base values and re-validate.

    Raises:
        KeyError: If a path does not exist.
        ValidationError: If a shifted value is invalid (e.g. margins below opex).
    """
    data = base.model_dump(mode="json")
    for path, delta in shifts.items():
        *parents, leaf = path.split(".")
        node = data
        for key in parents:
            node = node[key]
        value = node[leaf]
        node[leaf] = [v + delta for v in value] if isinstance(value, list) else value + delta
    return ModelAssumptions.model_validate(data)


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's assumptions, full model and headline metrics."""

    name: str
    description: str
    model: ValuationModel
    metrics: dict[str, float]


def scenario_metrics(model: ValuationModel) -> dict[str, float]:
    """Headline outputs used in scenario comparison and variance analysis."""
    p, d, lbo = model.projection, model.dcf, model.lbo
    last = p.years[-1]
    sched = lbo.schedule
    return {
        "revenue_cagr": (p.line("income", "net_sales", last)
                         / model.schema.income_statement(p.base_year).net_sales)
        ** (1 / len(p.years)) - 1,
        "final_year_revenue": p.line("income", "net_sales", last),
        "final_year_ebitda": p.line("income", "ebitda", last),
        "final_year_ebitda_margin": (p.line("income", "ebitda", last)
                                     / p.line("income", "net_sales", last)),
        "cumulative_ufcf": float(d.schedule.loc["unlevered_fcf"].sum()),
        "dcf_gordon_price": d.gordon.implied_share_price,
        "dcf_exit_price": d.exit.implied_share_price,
        "lbo_sponsor_equity": lbo.sources["Sponsor equity"],
        "lbo_exit_equity": lbo.exit["exit_equity"],
        "lbo_moic": lbo.moic,
        "lbo_irr": lbo.irr,
        "peak_leverage": float(sched.loc["leverage"].max()),
        "min_leverage_cushion": float(sched.loc["leverage_cushion"].min()),
        "min_coverage_cushion": float(sched.loc["coverage_cushion"].min()),
        "debt_repaid_pct": 1 - float(sched.loc["total_debt"].iloc[-1])
        / lbo.entry["total_new_debt"] if lbo.entry["total_new_debt"] else 0.0,
    }


def run_scenarios(schema: FinancialStatementSchema, base: ModelAssumptions,
                  specs: Sequence[ScenarioSpec]) -> list[ScenarioResult]:
    """Run each scenario through the full model.

    Args:
        schema: Historical data.
        base: Base assumptions (from ``config/assumptions.json``).
        specs: Scenario definitions.

    Returns:
        One result per scenario, in input order.
    """
    results = []
    for spec in specs:
        assumptions = apply_shifts(apply_overrides(base, spec.overrides), spec.shifts)
        model = build_model(schema, assumptions)
        results.append(ScenarioResult(spec.name, spec.description, model,
                                      scenario_metrics(model)))
        logger.info("Scenario %-5s IRR %6.1f%%  MOIC %.2fx  DCF $%.2f / $%.2f", spec.name,
                    100 * model.lbo.irr, model.lbo.moic, model.dcf.gordon.implied_share_price,
                    model.dcf.exit.implied_share_price)
    return results


def variance_table(results: Sequence[ScenarioResult], base_name: str = "Base") -> pd.DataFrame:
    """Scenario metrics side by side, with each scenario's change vs. the base case.

    Returns:
        DataFrame indexed by metric with columns ``<scenario>`` and
        ``<scenario> vs Base`` for every non-base scenario.
    """
    frame = pd.DataFrame({r.name: r.metrics for r in results})
    if base_name not in frame.columns:
        return frame
    for r in results:
        if r.name != base_name:
            frame[f"{r.name} vs {base_name}"] = frame[r.name] - frame[base_name]
    return frame


# --------------------------------------------------------------------------------------
# Sensitivity matrices
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SensitivityTable:
    """A two-way sensitivity grid.

    Attributes:
        key: Identifier (e.g. ``"dcf_wacc_growth"``).
        title: Display title.
        row_label / col_label: Axis names.
        rows / cols: Axis values.
        values: Grid of outputs (index = rows, columns = cols); NaN where infeasible.
        value_format: ``"price"``, ``"pct"`` or ``"multiple"``.
        axis_formats: Formats of the row and column axis values.
        base: The (row, col) of the base case.
    """

    key: str
    title: str
    row_label: str
    col_label: str
    rows: list[float]
    cols: list[float]
    values: pd.DataFrame
    value_format: Literal["price", "pct", "multiple"]
    axis_formats: tuple[str, str]
    base: tuple[float, float]


def centred_axis(centre: float, step: float, n_each_side: int) -> list[float]:
    """``centre`` plus/minus ``n_each_side`` steps, ascending."""
    return [centre + k * step for k in range(-n_each_side, n_each_side + 1)]


def _grid(rows: Sequence[float], cols: Sequence[float],
          fn: Callable[[float, float], float]) -> pd.DataFrame:
    """Evaluate ``fn(row, col)`` over a grid; infeasible points become NaN."""
    data: dict[float, list[float]] = {}
    for c in cols:
        column = []
        for r in rows:
            try:
                column.append(fn(r, c))
            except AssumptionError:
                column.append(float("nan"))
        data[c] = column
    return pd.DataFrame(data, index=list(rows))


@dataclass(frozen=True)
class SensitivitySet:
    """All sensitivity tables for one model."""

    dcf_wacc_growth: SensitivityTable
    dcf_multiple_wacc: SensitivityTable
    lbo_irr: SensitivityTable
    lbo_moic: SensitivityTable

    def tables(self) -> list[SensitivityTable]:
        """All tables in display order."""
        return [self.dcf_wacc_growth, self.dcf_multiple_wacc, self.lbo_irr, self.lbo_moic]


def run_sensitivities(model: ValuationModel, grids: SensitivityGrids) -> SensitivitySet:
    """Build the DCF and LBO sensitivity matrices around the base case.

    Args:
        model: Base-case valuation model.
        grids: Axis definitions.

    Returns:
        The four sensitivity tables.
    """
    p, s, a = model.projection, model.schema, model.assumptions
    n = grids.steps_each_side
    wacc = model.dcf.wacc.wacc
    g = model.dcf.terminal_growth_rate
    mult = model.dcf.exit_multiple
    waccs = centred_axis(wacc, grids.wacc_step, n)
    growths = centred_axis(g, grids.growth_step, n)
    multiples = centred_axis(mult, grids.dcf_exit_multiple_step, n)

    dcf_wg = _grid(waccs, growths, lambda w, gr: run_dcf(
        p, s, a, wacc=w, terminal_growth=gr).gordon.implied_share_price)
    dcf_mw = _grid(multiples, waccs, lambda m, w: run_dcf(
        p, s, a, wacc=w, exit_multiple=m).exit.implied_share_price)

    tx0 = a.transaction
    entries = grids.lbo_entry_multiples or [
        round(m, 4) for m in centred_axis(tx0.entry_ev_ebitda_multiple,
                                          grids.lbo_entry_multiple_step, n) if m > 0]
    debts = grids.lbo_debt_pcts or [
        round(min(max(d, 0.05), 0.90), 4)
        for d in centred_axis(tx0.debt_pct_of_entry_ev, grids.lbo_debt_pct_step, n)]
    debts = sorted(set(debts))
    lbo_runs: dict[tuple[float, float], LBOResult | None] = {}
    for e in entries:
        for d in debts:
            try:
                lbo_runs[(e, d)] = run_lbo(p, s, a, entry_multiple=e, debt_pct=d)
            except AssumptionError:
                lbo_runs[(e, d)] = None

    def lbo_metric(attr: str) -> Callable[[float, float], float]:
        def fn(e: float, d: float) -> float:
            run = lbo_runs[(e, d)]
            if run is None:
                raise AssumptionError("infeasible structure")
            return float(getattr(run, attr))
        return fn

    tx = a.transaction
    exit_note = f"exit fixed at {tx.exit_ev_ebitda_multiple:.1f}x"
    return SensitivitySet(
        dcf_wacc_growth=SensitivityTable(
            "dcf_wacc_growth", "DCF (Gordon Growth): implied share price",
            "WACC", "Terminal growth", waccs, growths, dcf_wg, "price", ("pct", "pct"),
            (wacc, g)),
        dcf_multiple_wacc=SensitivityTable(
            "dcf_multiple_wacc", "DCF (Exit Multiple): implied share price",
            "Exit EV/EBITDA", "WACC", multiples, waccs, dcf_mw, "price", ("multiple", "pct"),
            (mult, wacc)),
        lbo_irr=SensitivityTable(
            "lbo_irr", f"LBO sponsor IRR ({exit_note})", "Entry EV/EBITDA",
            "Debt % of entry EV", entries, debts, _grid(entries, debts, lbo_metric("irr")),
            "pct", ("multiple", "pct"),
            (tx.entry_ev_ebitda_multiple, tx.debt_pct_of_entry_ev)),
        lbo_moic=SensitivityTable(
            "lbo_moic", f"LBO sponsor MOIC ({exit_note})", "Entry EV/EBITDA",
            "Debt % of entry EV", entries, debts, _grid(entries, debts, lbo_metric("moic")),
            "multiple", ("multiple", "pct"),
            (tx.entry_ev_ebitda_multiple, tx.debt_pct_of_entry_ev)),
    )


# --------------------------------------------------------------------------------------
# Risk metrics
# --------------------------------------------------------------------------------------
def solve_monotonic(fn: Callable[[float], float], target: float, low: float, high: float,
                    increasing: bool, tolerance: float = 1e-4) -> float | None:
    """Bisection for ``fn(x) = target`` on a monotonic function.

    Returns:
        The solution, or ``None`` if the target is not bracketed by ``[low, high]``.
    """
    f_low, f_high = fn(low), fn(high)
    lo_ok = f_low <= target if increasing else f_low >= target
    hi_ok = f_high >= target if increasing else f_high <= target
    if not (lo_ok and hi_ok):
        return None
    for _ in range(100):
        mid = (low + high) / 2
        above = fn(mid) >= target
        if above == increasing:
            high = mid
        else:
            low = mid
        if high - low < tolerance:
            break
    return (low + high) / 2


@dataclass(frozen=True)
class RiskMetrics:
    """Downside, covenant and break-even analysis for the base case."""

    covenant_table: pd.DataFrame
    min_leverage_cushion: float
    min_coverage_cushion: float
    covenant_breach: bool
    breakeven_exit_multiple: float | None
    max_entry_multiple_for_hurdle: float | None
    terminal_value_share_gordon: float
    terminal_value_share_exit: float
    sponsor_equity_pct_of_uses: float
    trapped_cash: float
    repatriation_tax_rate: float


def compute_risk_metrics(model: ValuationModel, criteria: InvestmentCriteria) -> RiskMetrics:
    """Covenant headroom, break-even multiples and concentration metrics.

    Break-evens hold all other assumptions at the base case:

    - *Break-even exit multiple*: the exit EV/EBITDA at which sponsor IRR equals the
      hurdle rate.
    - *Maximum entry multiple*: the highest entry EV/EBITDA that still earns the
      hurdle rate (at the base exit multiple).
    """
    p, s, a = model.projection, model.schema, model.assumptions
    lbo = model.lbo
    sched = lbo.schedule
    covenants = sched.loc[["ebitda", "total_debt", "total_interest_expense", "leverage",
                           "leverage_covenant", "leverage_cushion", "interest_coverage",
                           "coverage_covenant", "coverage_cushion"]]

    def irr_at(**kwargs: float) -> float:
        try:
            return run_lbo(p, s, a, **kwargs).irr
        except AssumptionError:
            return -1.0

    breakeven_exit = solve_monotonic(lambda m: irr_at(exit_multiple=m), criteria.target_irr,
                                     1.0, 40.0, increasing=True)
    max_entry = solve_monotonic(lambda m: irr_at(entry_multiple=m), criteria.target_irr,
                                1.0, 40.0, increasing=False)
    min_lev = float(sched.loc["leverage_cushion"].min())
    min_cov = float(sched.loc["coverage_cushion"].min())
    return RiskMetrics(
        covenant_table=covenants,
        min_leverage_cushion=min_lev,
        min_coverage_cushion=min_cov,
        covenant_breach=min(min_lev, min_cov) < 0,
        breakeven_exit_multiple=breakeven_exit,
        max_entry_multiple_for_hurdle=max_entry,
        terminal_value_share_gordon=model.dcf.gordon.pv_terminal_share_of_ev,
        terminal_value_share_exit=model.dcf.exit.pv_terminal_share_of_ev,
        sponsor_equity_pct_of_uses=lbo.sources["Sponsor equity"] / lbo.entry["total_uses"],
        trapped_cash=lbo.entry["trapped_cash"],
        repatriation_tax_rate=repatriation_tax_rate(s, a.market),
    )


# --------------------------------------------------------------------------------------
# Recommendation
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Recommendation:
    """Rule-based investment verdict.

    Attributes:
        verdict: ``"PROCEED"`` or ``"PASS"``.
        tests: Each criterion with its result: ``(description, passed)``.
    """

    verdict: Literal["PROCEED", "PASS"]
    tests: list[tuple[str, bool]]

    @property
    def failed(self) -> list[str]:
        """Descriptions of the criteria that were not met."""
        return [desc for desc, ok in self.tests if not ok]


def recommend(base: ScenarioResult, risk: RiskMetrics,
              criteria: InvestmentCriteria) -> Recommendation:
    """Apply the investment criteria to the base case.

    The deal is recommended only if every criterion passes.
    """
    m = base.metrics
    tests = [
        (f"Base-case IRR {m['lbo_irr']:.1%} vs. {criteria.target_irr:.0%} hurdle",
         m["lbo_irr"] >= criteria.target_irr),
        (f"Base-case MOIC {m['lbo_moic']:.2f}x vs. {criteria.minimum_moic:.1f}x minimum",
         m["lbo_moic"] >= criteria.minimum_moic),
    ]
    if criteria.require_base_case_covenant_compliance:
        tests.append(("Base-case maintenance covenants met in every year "
                      f"(minimum EBITDA cushion {min(risk.min_leverage_cushion, risk.min_coverage_cushion):.0%})",
                      not risk.covenant_breach))
    verdict: Literal["PROCEED", "PASS"] = "PROCEED" if all(ok for _, ok in tests) else "PASS"
    return Recommendation(verdict=verdict, tests=tests)


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AnalysisResult:
    """Everything Phase 3 produces for the workbook and the memo."""

    base: ScenarioResult
    scenarios: list[ScenarioResult]
    sensitivities: SensitivitySet
    risk: RiskMetrics
    recommendation: Recommendation
    criteria: InvestmentCriteria
    variance: pd.DataFrame = field(repr=False)


def run_analysis(schema: FinancialStatementSchema, assumptions: ModelAssumptions,
                 config: AnalysisConfig) -> AnalysisResult:
    """Scenarios, sensitivities, risk metrics and the recommendation.

    The first scenario named ``Base`` (or, failing that, the first scenario) is the
    reference case for sensitivities, risk metrics and the verdict.
    """
    scenarios = run_scenarios(schema, assumptions, config.scenarios)
    base = next((r for r in scenarios if r.name.lower() == "base"), scenarios[0])
    risk = compute_risk_metrics(base.model, config.investment_criteria)
    return AnalysisResult(
        base=base,
        scenarios=scenarios,
        sensitivities=run_sensitivities(base.model, config.sensitivity_grids),
        risk=risk,
        recommendation=recommend(base, risk, config.investment_criteria),
        criteria=config.investment_criteria,
        variance=variance_table(scenarios, base.name),
    )


def _clean(value: Any) -> Any:
    """Make a value JSON-safe: round floats, map NaN/inf to None, recurse."""
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 6)
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


LARGEST_LBO_EV = 100_000.0
"""USD millions. No leveraged buyout has come close to $100B of enterprise value."""


def deal_framing(entry_ev: float) -> str:
    """One-line framing of the LBO for the memo, scaled to the deal size."""
    if entry_ev > LARGEST_LBO_EV:
        return ("Hypothetical take-private stress test. At this size the company is far "
                "larger than any leveraged buyout ever completed; the analysis tests whether "
                "a sponsor could earn its hurdle rate, not whether financing exists.")
    return ("Illustrative take-private analysis: could a financial sponsor buying the "
            "company at a control premium earn its hurdle rate?")


def analysis_context(analysis: AnalysisResult) -> dict[str, Any]:
    """Structured, JSON-serializable summary of the analysis (the memo's data pack).

    Amounts are USD millions; rates and returns are decimals.
    """
    model = analysis.base.model
    s, a, lbo, d = model.schema, model.assumptions, model.lbo, model.dcf
    y0 = model.projection.base_year
    inc = s.income_statement(y0)
    return _clean({
        "company": {"name": s.metadata.name, "ticker": s.metadata.ticker,
                    "fiscal_year_end": s.metadata.fiscal_year_end.isoformat(),
                    "source": f"{s.metadata.filing_type} for fiscal year {y0}"},
        "units": "USD millions unless stated; rates and returns as decimals",
        "framing": deal_framing(lbo.entry["entry_ev"]),
        "data_source": s.data_source,
        "data_notes": s.derivation_notes,
        "historicals": {
            "net_sales": {y: s.income_statement(y).net_sales for y in s.years},
            "ebitda": {y: s.ebitda(y) for y in s.years},
            "ebitda_margin": {y: s.ebitda(y) / s.income_statement(y).net_sales for y in s.years},
            "unlevered_fcf": {y: s.unlevered_fcf(y) for y in s.years},
            "segments": {seg.name: seg.net_sales for seg in s.revenue_segments},
            "ltm_net_income": inc.net_income,
        },
        "balance_sheet_facts": {
            "cash_and_investments": s.balance_sheet(y0).cash_and_investments,
            "total_debt": s.balance_sheet(y0).total_debt,
            "offshore_cash": s.foreign_cash_and_investments,
            "repatriation_tax_rate_implied_by_10k": s.implied_repatriation_tax_rate,
            "existing_term_debt_tranches": len(s.debt_schedule.tranches) if s.debt_schedule else 0,
        },
        "dcf": {
            "wacc": d.wacc.wacc, "cost_of_equity": d.wacc.cost_of_equity,
            "after_tax_cost_of_debt": d.wacc.after_tax_cost_of_debt,
            "terminal_growth": d.terminal_growth_rate, "exit_multiple": d.exit_multiple,
            "current_share_price": a.market.share_price,
            "gordon": {"enterprise_value": d.gordon.enterprise_value,
                       "share_price": d.gordon.implied_share_price,
                       "premium": d.gordon.premium_to_current,
                       "terminal_share_of_ev": d.gordon.pv_terminal_share_of_ev,
                       "implied_exit_multiple": d.gordon.implied_cross_check},
            "exit_multiple_method": {"enterprise_value": d.exit.enterprise_value,
                                     "share_price": d.exit.implied_share_price,
                                     "premium": d.exit.premium_to_current,
                                     "terminal_share_of_ev": d.exit.pv_terminal_share_of_ev,
                                     "implied_perpetual_growth": d.exit.implied_cross_check},
        },
        "lbo": {
            "entry": lbo.entry, "sources": lbo.sources, "uses": lbo.uses, "exit": lbo.exit,
            "moic": lbo.moic, "irr": lbo.irr,
            "tranches": [t.model_dump(mode="json") for t in a.transaction.tranches],
            "debt_schedule": {str(y): {k: lbo.schedule.at[k, y] for k in (
                "ebitda", "levered_fcf", "total_debt", "ending_cash", "leverage",
                "interest_coverage", "leverage_cushion", "coverage_cushion")}
                for y in lbo.schedule.columns},
        },
        "scenarios": [{"name": r.name, "description": r.description, **r.metrics}
                      for r in analysis.scenarios],
        "sensitivities": {
            t.key: {"title": t.title, "rows": {"label": t.row_label, "values": t.rows},
                    "cols": {"label": t.col_label, "values": t.cols},
                    "grid": t.values.values.tolist()}
            for t in analysis.sensitivities.tables()
        },
        "risk_metrics": {
            "min_leverage_cushion": analysis.risk.min_leverage_cushion,
            "min_coverage_cushion": analysis.risk.min_coverage_cushion,
            "covenant_breach_base_case": analysis.risk.covenant_breach,
            "covenants": {"max_leverage": a.transaction.max_leverage_covenant,
                          "min_interest_coverage": a.transaction.min_interest_coverage_covenant},
            "breakeven_exit_multiple_for_hurdle": analysis.risk.breakeven_exit_multiple,
            "max_entry_multiple_for_hurdle": analysis.risk.max_entry_multiple_for_hurdle,
            "sponsor_equity_pct_of_uses": analysis.risk.sponsor_equity_pct_of_uses,
            "trapped_offshore_cash": analysis.risk.trapped_cash,
        },
        "investment_criteria": analysis.criteria.model_dump(exclude={"sources"}),
        "recommendation": {"verdict": analysis.recommendation.verdict,
                           "tests": [{"criterion": c, "passed": ok}
                                     for c, ok in analysis.recommendation.tests]},
        "assumption_sources": {**a.market.sources, **a.operating.sources,
                               **a.transaction.sources},
    })


GENERIC_SCENARIOS = Path(__file__).resolve().parents[1] / "config" / "scenarios_generic.json"


def load_company(source: str, *, ticker: str | None = None,
                 fiscal_year: int | None = None) -> FinancialStatementSchema:
    """Load historicals from a ticker, an EDGAR ``.htm`` 10-K, a 10-K PDF/text, or an
    extracted JSON file.

    Args:
        source: Ticker / CIK (looked up on SEC EDGAR) or a file path.
        ticker: Ticker to record for file inputs.
        fiscal_year: For ticker lookups, the 10-K's fiscal year (default: latest).
    """
    path = Path(source)
    if path.is_file():
        if path.suffix.lower() in (".htm", ".html", ".xhtml"):
            from src.xbrl_mapping import schema_from_htm

            return schema_from_htm(str(path), ticker=ticker)
        return load_schema(path, ticker)
    from src.xbrl_mapping import schema_from_ticker

    return schema_from_ticker(source, fiscal_year)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: any 10-K -> 3-statement model, DCF, LBO, scenarios -> workbook, data pack, memo.

    Examples::

        python -m src.analyzer MSFT --share-price 420
        python -m src.analyzer path/to/10-k.htm --share-price 55.10
        python -m src.analyzer data/raw/apple_10k_fy2015.pdf          # Apple sample

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Value any company from its 10-K.")
    parser.add_argument("source", help="Ticker or CIK (SEC lookup), an EDGAR 10-K .htm, a "
                                       "10-K PDF/text, or extracted JSON")
    parser.add_argument("--share-price", type=float,
                        help="Current share price (USD). Required except for the Apple "
                             "sample, which uses config/assumptions.json")
    parser.add_argument("--fiscal-year", type=int, help="Fiscal year of the 10-K (ticker lookup)")
    parser.add_argument("--ticker", help="Ticker to record for file inputs")
    parser.add_argument("--overrides", type=Path,
                        help="JSON file deep-merged over the derived assumptions")
    parser.add_argument("--assumptions", type=Path, default=Path("config/assumptions.json"))
    parser.add_argument("--scenarios", type=Path, default=Path("config/scenarios.json"))
    parser.add_argument("--out-dir", type=Path,
                        help="Output folder (default: outputs/ for the sample, "
                             "outputs/<TICKER>/ otherwise)")
    parser.add_argument("--offline", action="store_true",
                        help="Write the memo from the deterministic template (no API call)")
    parser.add_argument("--model", default=None, help="Claude model for the memo narrative")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-8s %(name)s: %(message)s")

    from src.auto_assumptions import derive_assumptions
    from src.excel_export import write_workbook
    from src.memo_generator import MemoGenerationError, generate_memo

    try:
        schema = load_company(args.source, ticker=args.ticker, fiscal_year=args.fiscal_year)
        if args.share_price is None:
            if schema.data_source != "pdf":
                parser.error("--share-price is required to value this company")
            assumptions = ModelAssumptions.from_json(args.assumptions)
            config = AnalysisConfig.from_json(args.scenarios)
            out_dir = args.out_dir or Path("outputs")
        else:
            overrides = (json.loads(args.overrides.read_text(encoding="utf-8"))
                         if args.overrides else None)
            assumptions = derive_assumptions(schema, args.share_price, overrides)
            config = AnalysisConfig.from_json(GENERIC_SCENARIOS)
            slug = schema.metadata.ticker or schema.metadata.name.split()[0]
            out_dir = args.out_dir or Path("outputs") / re.sub(r"[^A-Za-z0-9_-]", "", slug)
        analysis = run_analysis(schema, assumptions, config)
    except (FinancialDataError, ValidationError) as exc:
        logger.error("%s", exc)
        return 1

    args.out_dir = out_dir
    args.out_dir.mkdir(parents=True, exist_ok=True)
    context = analysis_context(analysis)
    try:
        memo = generate_memo(analysis, offline=args.offline, model=args.model)
    except MemoGenerationError as exc:
        logger.error("%s", exc)
        return 1
    try:
        (args.out_dir / "analysis.json").write_text(json.dumps(context, indent=2),
                                                    encoding="utf-8")
        (args.out_dir / "memo.md").write_text(memo.markdown, encoding="utf-8")
        write_workbook(analysis.base.model, args.out_dir / "model.xlsx", analysis=analysis)
    except PermissionError as exc:
        logger.error("Cannot write %s: close it in Excel (or any other program) and rerun.",
                     exc.filename)
        return 1

    print(f"\n{'Scenario':<8}{'IRR':>9}{'MOIC':>8}{'DCF Gordon':>13}{'DCF Exit':>11}")
    for r in analysis.scenarios:
        m = r.metrics
        print(f"{r.name:<8}{m['lbo_irr']:>9.1%}{m['lbo_moic']:>7.2f}x"
              f"{m['dcf_gordon_price']:>13,.2f}{m['dcf_exit_price']:>11,.2f}")
    print(f"\nRecommendation: {analysis.recommendation.verdict}")
    for desc, ok in analysis.recommendation.tests:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print(f"\nMemo ({memo.source}) -> {args.out_dir / 'memo.md'}")
    print(f"Workbook -> {args.out_dir / 'model.xlsx'}   Data pack -> {args.out_dir / 'analysis.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
