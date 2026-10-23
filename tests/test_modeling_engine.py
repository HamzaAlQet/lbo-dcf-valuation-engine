"""Tests for the 3-statement projection, DCF and LBO engine.

Linkage tests recompute each tie independently from the projected line items rather
than trusting the engine's own subtotals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.exceptions import AssumptionError
from src.modeling_engine import (
    ValuationModel,
    base_working_capital_drivers,
    build_model,
    run_dcf,
    run_lbo,
)
from src.schemas import FinancialStatementSchema, ModelAssumptions

CONFIG = Path(__file__).resolve().parents[1] / "config" / "assumptions.json"


@pytest.fixture(scope="session")
def assumptions() -> ModelAssumptions:
    return ModelAssumptions.from_json(CONFIG)


@pytest.fixture(scope="session")
def model(apple: FinancialStatementSchema, assumptions: ModelAssumptions) -> ValuationModel:
    return build_model(apple, assumptions)


def years_with_prior(model: ValuationModel) -> list[tuple[int, int]]:
    """(year, prior year) pairs across the projection, starting from the base year."""
    years = [model.projection.base_year, *model.projection.years]
    return list(zip(years[1:], years[:-1], strict=True))


def balance(model: ValuationModel, key: str, year: int) -> float:
    """Balance sheet line for a historical or projected year."""
    if year == model.projection.base_year:
        return getattr(model.schema.balance_sheet(year), key)
    return model.projection.line("balance", key, year)


# --------------------------------------------------------------------------------------
# Three-statement linkage
# --------------------------------------------------------------------------------------
def test_projected_balance_sheets_balance(model: ValuationModel) -> None:
    for y in model.projection.years:
        b = model.projection.balance[y]
        assert b["total_assets"] == pytest.approx(
            b["total_liabilities"] + b["total_equity"], abs=1e-6), f"FY{y} does not balance"


def test_net_income_flows_to_retained_earnings(model: ValuationModel) -> None:
    p = model.projection
    for y, prior in years_with_prior(model):
        cf = p.cash_flow[y]
        expected = (balance(model, "retained_earnings", prior) + p.income.at["net_income", y]
                    + cf["dividends_paid"] + cf["share_repurchases"])
        assert balance(model, "retained_earnings", y) == pytest.approx(expected)


def test_cash_flow_statement_ties_to_balance_sheet_cash(model: ValuationModel) -> None:
    p = model.projection
    for y, prior in years_with_prior(model):
        assert p.cash_flow.at["beginning_cash", y] == pytest.approx(
            balance(model, "cash_and_equivalents", prior))
        assert p.cash_flow.at["ending_cash", y] == pytest.approx(
            balance(model, "cash_and_equivalents", y))


def test_ppe_roll_forward(model: ValuationModel) -> None:
    p = model.projection
    for y, prior in years_with_prior(model):
        expected = (balance(model, "ppe_net", prior) - p.cash_flow.at["capital_expenditures", y]
                    - p.income.at["depreciation_and_amortization", y])
        assert balance(model, "ppe_net", y) == pytest.approx(expected)


def test_working_capital_change_ties_balance_sheet_to_cash_flow(model: ValuationModel) -> None:
    for y, prior in years_with_prior(model):
        bs_prior = (model.schema.balance_sheet(prior) if prior == model.projection.base_year
                    else model.projection.statements[prior][1])
        bs_now = model.projection.statements[y][1]
        cf = model.projection.statements[y][2]
        assert cf.change_in_nwc == pytest.approx(
            bs_now.net_working_capital - bs_prior.net_working_capital)


def test_working_capital_defaults_to_base_year_drivers(model: ValuationModel) -> None:
    base = base_working_capital_drivers(model.schema, model.projection.base_year)
    first = model.projection.years[0]
    # FY2015 receivable days = 16,849 / 233,715 x 365
    assert base["receivable_days"] == pytest.approx(16_849 / 233_715 * 365)
    assert model.projection.drivers.at["receivable_days", first] == pytest.approx(
        base["receivable_days"])


def test_interest_uses_beginning_balances(model: ValuationModel) -> None:
    first = model.projection.years[0]
    base = model.schema.balance_sheet(model.projection.base_year)
    yield_ = model.schema.investment_portfolio_yield
    assert model.projection.income.at["interest_income", first] == pytest.approx(
        yield_ * base.cash_and_investments)


def test_revenue_follows_growth_drivers(model: ValuationModel,
                                        assumptions: ModelAssumptions) -> None:
    sales = model.schema.income_statement(model.projection.base_year).net_sales
    for y, g in zip(model.projection.years, assumptions.operating.revenue_growth, strict=True):
        sales *= 1 + g
        assert model.projection.income.at["net_sales", y] == pytest.approx(sales)


# --------------------------------------------------------------------------------------
# DCF
# --------------------------------------------------------------------------------------
def test_wacc_build_up(model: ValuationModel, assumptions: ModelAssumptions) -> None:
    w, m = model.dcf.wacc, assumptions.market
    ke = m.risk_free_rate + m.levered_beta * m.equity_risk_premium
    e = m.share_price * model.schema.diluted_shares_outstanding
    d = 8_499 + 2_500 + 53_463
    expected = e / (e + d) * ke + d / (e + d) * w.pre_tax_cost_of_debt * (1 - m.marginal_tax_rate)
    assert w.wacc == pytest.approx(expected)
    assert w.equity_weight + w.debt_weight == pytest.approx(1.0)


def test_gordon_growth_terminal_value(model: ValuationModel) -> None:
    d = model.dcf
    r, g = d.wacc.wacc, d.terminal_growth_rate
    final_fcf = d.schedule.loc["unlevered_fcf"].iloc[-1]
    assert d.gordon.terminal_value == pytest.approx(final_fcf * (1 + g) / (r - g))
    n = len(model.projection.years)
    assert d.gordon.pv_terminal_value == pytest.approx(d.gordon.terminal_value / (1 + r) ** n)


def test_exit_multiple_terminal_value(model: ValuationModel) -> None:
    d = model.dcf
    final_ebitda = model.projection.income.loc["ebitda"].iloc[-1]
    assert d.exit.terminal_value == pytest.approx(final_ebitda * d.exit_multiple)


def test_mid_year_discounting(model: ValuationModel) -> None:
    sched = model.dcf.schedule
    assert list(sched.loc["discount_period"]) == [0.5, 1.5, 2.5, 3.5, 4.5]
    assert model.dcf.sum_pv_fcf == pytest.approx(sched.loc["pv_fcf"].sum())


def test_equity_bridge_and_share_price(model: ValuationModel) -> None:
    d = model.dcf
    for tv in (d.gordon, d.exit):
        assert tv.enterprise_value == pytest.approx(d.sum_pv_fcf + tv.pv_terminal_value)
        assert tv.equity_value == pytest.approx(
            tv.enterprise_value + d.cash_and_investments - d.total_debt - d.repatriation_tax)
        assert tv.implied_share_price == pytest.approx(
            tv.equity_value / model.schema.diluted_shares_outstanding)
    assert d.repatriation_tax == pytest.approx(186_900 * 30.0 / 91.5)


def test_terminal_methods_are_internally_consistent(model: ValuationModel) -> None:
    """Exiting at Gordon's implied multiple must reproduce Gordon's EV."""
    d = model.dcf
    rerun = run_dcf(model.projection, model.schema, model.assumptions,
                    exit_multiple=d.gordon.implied_cross_check)
    assert rerun.exit.enterprise_value == pytest.approx(d.gordon.enterprise_value)


def test_terminal_growth_at_or_above_wacc_rejected(model: ValuationModel) -> None:
    with pytest.raises(AssumptionError, match="below WACC"):
        run_dcf(model.projection, model.schema, model.assumptions, wacc=0.03,
                terminal_growth=0.03)


# --------------------------------------------------------------------------------------
# LBO
# --------------------------------------------------------------------------------------
def test_sources_equal_uses(model: ValuationModel) -> None:
    lbo = model.lbo
    assert sum(lbo.sources.values()) == pytest.approx(sum(lbo.uses.values()))
    assert lbo.entry["equity_purchase_price"] == pytest.approx(
        lbo.entry["entry_ev"] - lbo.entry["existing_debt"] + lbo.entry["cash_and_investments"])


def test_debt_balances_decline_and_never_go_negative(model: ValuationModel) -> None:
    sched = model.lbo.schedule
    for i in range(len(model.lbo.tranche_names)):
        endings = list(sched.loc[f"tranche{i}_ending"])
        assert all(b >= -1e-6 for b in endings)
        assert endings == sorted(endings, reverse=True)


def test_cash_waterfall_conserves_cash(model: ValuationModel,
                                       assumptions: ModelAssumptions) -> None:
    for y, r in model.lbo.schedule.items():
        assert r["ending_cash"] == pytest.approx(
            r["beginning_cash"] + r["levered_fcf"] - r["total_mandatory"] - r["total_optional"])
        # With a 100% sweep and no shortfall, cash is swept down to the minimum.
        assert r["ending_cash"] == pytest.approx(assumptions.transaction.minimum_cash), y


def test_sweep_pays_senior_before_subordinated(apple: FinancialStatementSchema,
                                               model: ValuationModel) -> None:
    # Lower leverage so the term loan is retired inside the holding period.
    lbo = run_lbo(model.projection, apple, model.assumptions, debt_pct=0.20)
    sched = lbo.schedule
    assert sched.loc["tranche0_ending"].iloc[-1] == pytest.approx(0.0)
    assert sched.loc["tranche1_optional"].sum() > 0
    for y in sched.columns:
        if sched.at["tranche1_optional", y] > 0:
            assert sched.at["tranche0_ending", y] == pytest.approx(0.0), (
                f"FY{y}: subordinated notes prepaid while senior debt outstanding")


def test_returns_math(model: ValuationModel) -> None:
    lbo = model.lbo
    h = len(lbo.schedule.columns)
    assert lbo.moic == pytest.approx(lbo.exit["exit_equity"] / lbo.sources["Sponsor equity"])
    assert lbo.irr == pytest.approx(lbo.moic ** (1 / h) - 1, abs=1e-8)
    assert lbo.exit["exit_equity"] == pytest.approx(
        lbo.exit["exit_ev"] - lbo.exit["exit_debt"] + lbo.exit["exit_cash"]
        + lbo.exit["trapped_cash_after_tax"])


def test_higher_entry_multiple_lowers_returns(apple: FinancialStatementSchema,
                                              model: ValuationModel) -> None:
    irrs = [run_lbo(model.projection, apple, model.assumptions, entry_multiple=m,
                    exit_multiple=8.0).irr for m in (7.0, 8.0, 9.0)]
    assert irrs == sorted(irrs, reverse=True)


def test_covenant_cushions(model: ValuationModel, assumptions: ModelAssumptions) -> None:
    tx = assumptions.transaction
    for _, r in model.lbo.schedule.items():
        assert r["leverage_cushion"] == pytest.approx(
            1 - r["total_debt"] / tx.max_leverage_covenant / r["ebitda"])
        assert r["coverage_cushion"] == pytest.approx(
            1 - tx.min_interest_coverage_covenant * r["total_interest_expense"] / r["ebitda"])


def test_repatriating_offshore_cash_reduces_sponsor_equity(
        apple: FinancialStatementSchema, assumptions: ModelAssumptions) -> None:
    config = assumptions.model_dump()
    config["transaction"]["foreign_cash_usable_pct"] = 1.0
    repatriated = build_model(apple, ModelAssumptions.model_validate(config)).lbo
    base = build_model(apple, assumptions).lbo
    after_tax_cash = 186_900 * (1 - 30.0 / 91.5)
    assert base.sources["Sponsor equity"] - repatriated.sources["Sponsor equity"] == (
        pytest.approx(after_tax_cash))
