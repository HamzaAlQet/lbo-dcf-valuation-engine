"""Tests for scenarios, sensitivity matrices, risk metrics and the recommendation."""

from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from src.analyzer import (
    AnalysisConfig,
    AnalysisResult,
    InvestmentCriteria,
    analysis_context,
    apply_overrides,
    deep_merge,
    recommend,
    solve_monotonic,
)
from src.modeling_engine import run_lbo


def by_name(analysis: AnalysisResult, name: str):  # noqa: ANN201
    return next(r for r in analysis.scenarios if r.name == name)


# --------------------------------------------------------------------------------------
# Scenario mechanics
# --------------------------------------------------------------------------------------
def test_deep_merge_merges_dicts_and_replaces_lists() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 2}
    merged = deep_merge(base, {"a": {"y": [9]}, "c": 3})
    assert merged == {"a": {"x": 1, "y": [9]}, "b": 2, "c": 3}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 2}  # input untouched


def test_overrides_are_revalidated(analysis: AnalysisResult) -> None:
    base = analysis.base.model.assumptions
    with pytest.raises(ValidationError):
        apply_overrides(base, {"operating": {"revenue_growth": [0.1, 0.1]}})  # wrong length


def test_scenarios_are_ordered_bear_base_bull(analysis: AnalysisResult) -> None:
    bear, base, bull = (by_name(analysis, n).metrics for n in ("Bear", "Base", "Bull"))
    for key in ("final_year_ebitda", "dcf_gordon_price", "dcf_exit_price", "lbo_irr", "lbo_moic"):
        assert bear[key] < base[key] < bull[key], key


def test_base_scenario_matches_unmodified_model(analysis: AnalysisResult) -> None:
    base = analysis.base.metrics
    assert base["lbo_irr"] == pytest.approx(analysis.base.model.lbo.irr)
    assert base["dcf_gordon_price"] == pytest.approx(
        analysis.base.model.dcf.gordon.implied_share_price)


def test_variance_is_scenario_minus_base(analysis: AnalysisResult) -> None:
    v = analysis.variance
    for key in v.index:
        assert v.at[key, "Bull vs Base"] == pytest.approx(v.at[key, "Bull"] - v.at[key, "Base"])


def test_bull_case_repatriation_reduces_sponsor_equity(analysis: AnalysisResult) -> None:
    bull = by_name(analysis, "Bull").model.lbo
    assert bull.entry["repatriated_cash"] == pytest.approx(186_900)
    assert bull.entry["repatriation_tax_paid"] == pytest.approx(186_900 * 0.14)
    assert bull.sources["Sponsor equity"] < analysis.base.model.lbo.sources["Sponsor equity"]


# --------------------------------------------------------------------------------------
# Sensitivities
# --------------------------------------------------------------------------------------
def test_sensitivity_base_cells_equal_base_model(analysis: AnalysisResult) -> None:
    model, sens = analysis.base.model, analysis.sensitivities
    for table, expected in (
        (sens.dcf_wacc_growth, model.dcf.gordon.implied_share_price),
        (sens.dcf_multiple_wacc, model.dcf.exit.implied_share_price),
        (sens.lbo_irr, model.lbo.irr),
        (sens.lbo_moic, model.lbo.moic),
    ):
        r, c = table.base
        assert table.values.at[r, c] == pytest.approx(expected), table.key


def test_dcf_sensitivity_monotonicity(analysis: AnalysisResult) -> None:
    grid = analysis.sensitivities.dcf_wacc_growth.values
    assert all(grid[c].is_monotonic_decreasing for c in grid.columns)   # higher WACC -> lower
    assert all(grid.loc[r].is_monotonic_increasing for r in grid.index)  # higher g -> higher
    exit_grid = analysis.sensitivities.dcf_multiple_wacc.values
    assert all(exit_grid[c].is_monotonic_increasing for c in exit_grid.columns)


def test_lbo_irr_falls_as_entry_price_rises(analysis: AnalysisResult) -> None:
    grid = analysis.sensitivities.lbo_irr.values
    assert all(grid[c].is_monotonic_decreasing for c in grid.columns)


def test_grids_are_centred_on_base_case(analysis: AnalysisResult) -> None:
    table = analysis.sensitivities.dcf_wacc_growth
    mid_r, mid_c = len(table.rows) // 2, len(table.cols) // 2
    assert table.rows[mid_r] == pytest.approx(analysis.base.model.dcf.wacc.wacc)
    assert table.cols[mid_c] == pytest.approx(analysis.base.model.dcf.terminal_growth_rate)


# --------------------------------------------------------------------------------------
# Risk metrics & recommendation
# --------------------------------------------------------------------------------------
def test_breakeven_multiples_hit_the_hurdle(analysis: AnalysisResult) -> None:
    model, risk, hurdle = analysis.base.model, analysis.risk, analysis.criteria.target_irr
    p, s, a = model.projection, model.schema, model.assumptions
    assert risk.breakeven_exit_multiple is not None
    assert run_lbo(p, s, a, exit_multiple=risk.breakeven_exit_multiple).irr == pytest.approx(
        hurdle, abs=1e-4)
    assert risk.max_entry_multiple_for_hurdle is not None
    assert run_lbo(p, s, a, entry_multiple=risk.max_entry_multiple_for_hurdle).irr == (
        pytest.approx(hurdle, abs=1e-4))


def test_solve_monotonic_returns_none_when_not_bracketed() -> None:
    assert solve_monotonic(lambda x: x, 50.0, 0.0, 10.0, increasing=True) is None
    assert solve_monotonic(lambda x: x, 2.5, 0.0, 10.0, increasing=True) == pytest.approx(
        2.5, abs=1e-3)


def test_covenant_cushions_and_breach_flag(analysis: AnalysisResult) -> None:
    risk = analysis.risk
    sched = analysis.base.model.lbo.schedule
    assert risk.min_leverage_cushion == pytest.approx(sched.loc["leverage_cushion"].min())
    assert risk.covenant_breach is (min(risk.min_leverage_cushion, risk.min_coverage_cushion) < 0)


def test_base_case_recommendation_is_pass(analysis: AnalysisResult) -> None:
    rec = analysis.recommendation
    assert rec.verdict == "PASS"
    assert len(rec.failed) == 2  # IRR and MOIC; covenants are met


def test_recommendation_proceeds_when_criteria_are_met(analysis: AnalysisResult,
                                                      analysis_config: AnalysisConfig) -> None:
    lenient = InvestmentCriteria(target_irr=0.05, minimum_moic=1.2)
    assert recommend(analysis.base, analysis.risk, lenient).verdict == "PROCEED"
    assert analysis_config.investment_criteria.target_irr == 0.20


def test_analysis_context_is_strict_json(analysis: AnalysisResult) -> None:
    context = analysis_context(analysis)
    text = json.dumps(context, allow_nan=False)  # raises on NaN / inf
    assert context["recommendation"]["verdict"] == "PASS"
    assert len(context["scenarios"]) == 3
    assert math.isclose(json.loads(text)["lbo"]["irr"], analysis.base.model.lbo.irr,
                        abs_tol=1e-6)
