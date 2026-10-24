"""Accounting-integrity tests for the Pydantic schemas.

Each test either confirms an identity holds on real filed data (Apple FY2015) or
breaks exactly one link and asserts that the right identity is reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.exceptions import AccountingIdentityError
from src.schemas import (
    BalanceSheet,
    CashFlowStatement,
    DebtTranche,
    FinancialStatementSchema,
    IncomeStatement,
    InstrumentType,
    MacroAssumptions,
    ModelAssumptions,
    OperatingAssumptions,
    TransactionAssumptions,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "assumptions.json"


def find_year(items: list[dict[str, Any]], year: int) -> dict[str, Any]:
    """Return the dict for ``year`` from a list of per-year statement dicts."""
    return next(item for item in items if item["fiscal_year"] == year)


def assert_breach(exc: pytest.ExceptionInfo[AccountingIdentityError], identity: str) -> None:
    """Assert that ``identity`` is among the reported breaches."""
    reported = [b.identity for b in exc.value.breaches]
    assert any(identity in r for r in reported), f"{identity!r} not in {reported}"


# --------------------------------------------------------------------------------------
# Balance sheet: A = L + E
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("year", [2015, 2014])
def test_balance_sheet_balances_on_filed_data(apple: FinancialStatementSchema, year: int) -> None:
    bs = apple.balance_sheet(year)
    assert bs.total_assets == pytest.approx(bs.total_liabilities + bs.total_equity, abs=1.0)


def test_apple_fy2015_balance_sheet_totals(apple: FinancialStatementSchema) -> None:
    bs = apple.balance_sheet(2015)
    assert (bs.total_assets, bs.total_liabilities, bs.total_equity) == (290_479, 171_124, 119_355)


def test_unbalanced_balance_sheet_raises(apple_payload: dict[str, Any]) -> None:
    bs = find_year(apple_payload["balance_sheets"], 2015)
    bs["total_assets"] += 10
    with pytest.raises(AccountingIdentityError) as exc:
        BalanceSheet(**bs)
    assert_breach(exc, "Assets = Liabilities + Equity")
    assert "FY2015" in str(exc.value)


def test_all_breaches_reported_together(apple_payload: dict[str, Any]) -> None:
    bs = find_year(apple_payload["balance_sheets"], 2015)
    bs["inventories"] += 50          # breaks current-asset subtotal
    bs["retained_earnings"] += 50    # breaks equity subtotal
    with pytest.raises(AccountingIdentityError) as exc:
        BalanceSheet(**bs)
    assert_breach(exc, "Total current assets")
    assert_breach(exc, "Total equity")


def test_tolerance_absorbs_rounding_but_is_configurable(apple_payload: dict[str, Any]) -> None:
    bs = find_year(apple_payload["balance_sheets"], 2015)
    bs["cash_and_equivalents"] += 0.5  # half a million of rounding noise
    BalanceSheet.model_validate(bs)    # default $1M tolerance: passes
    with pytest.raises(AccountingIdentityError):
        BalanceSheet.model_validate(bs, context={"tolerance": 0.1})


def test_net_working_capital_definition(apple: FinancialStatementSchema) -> None:
    bs = apple.balance_sheet(2015)
    # (89,378 - 21,120 - 20,481 - 5,546) - (80,610 - 8,499 - 2,500)
    assert bs.net_working_capital == pytest.approx(42_231 - 69_611)


def test_net_debt_is_negative_for_net_cash_company(apple: FinancialStatementSchema) -> None:
    bs = apple.balance_sheet(2015)
    assert bs.total_debt == 8_499 + 2_500 + 53_463
    assert bs.cash_and_investments == 21_120 + 20_481 + 164_065
    assert bs.net_debt < 0


# --------------------------------------------------------------------------------------
# Income statement & cash flow statement
# --------------------------------------------------------------------------------------
def test_income_statement_net_income_build(apple_payload: dict[str, Any]) -> None:
    inc = find_year(apple_payload["income_statements"], 2015)
    inc["net_income"] += 100
    with pytest.raises(AccountingIdentityError) as exc:
        IncomeStatement(**inc)
    assert_breach(exc, "Net income = Pre-tax income - Tax")


def test_cash_roll_forward_break(apple_payload: dict[str, Any]) -> None:
    cf = find_year(apple_payload["cash_flow_statements"], 2015)
    cf["ending_cash"] += 25
    with pytest.raises(AccountingIdentityError) as exc:
        CashFlowStatement(**cf)
    assert_breach(exc, "Ending cash = Beginning cash + Net change")


def test_capex_sign_convention_enforced(apple_payload: dict[str, Any]) -> None:
    cf = find_year(apple_payload["cash_flow_statements"], 2015)
    cf["capital_expenditures"] = abs(cf["capital_expenditures"])  # wrong sign
    with pytest.raises(ValidationError, match="capital_expenditures"):
        CashFlowStatement(**cf)


def test_schemas_are_immutable(apple: FinancialStatementSchema) -> None:
    with pytest.raises(ValidationError):
        apple.balance_sheet(2015).total_assets = 0  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Cross-statement linkage (the 3-statement "ties")
# --------------------------------------------------------------------------------------
def test_net_income_flows_to_retained_earnings(apple_payload: dict[str, Any]) -> None:
    rf = find_year(apple_payload["equity_roll_forwards"], 2015)
    rf["net_income"] += 100                       # internally still balances...
    rf["other_retained_earnings_changes"] -= 100  # ...but no longer ties to the IS
    with pytest.raises(AccountingIdentityError) as exc:
        FinancialStatementSchema.model_validate(apple_payload)
    assert_breach(exc, "Net income: IS = Equity roll-forward")


def test_beginning_cash_ties_to_prior_balance_sheet(apple_payload: dict[str, Any]) -> None:
    cf = find_year(apple_payload["cash_flow_statements"], 2015)
    # Shift opening cash and offset in financing so the CFS is internally consistent.
    cf["beginning_cash"] += 100
    cf["other_financing"] -= 100
    cf["cash_from_financing"] -= 100
    cf["net_change_in_cash"] -= 100
    with pytest.raises(AccountingIdentityError) as exc:
        FinancialStatementSchema.model_validate(apple_payload)
    assert_breach(exc, "CFS beginning (ex. restricted) = prior-year BS")


def test_segments_must_sum_to_net_sales(apple_payload: dict[str, Any]) -> None:
    americas = next(s for s in apple_payload["revenue_segments"] if s["name"] == "Americas")
    americas["net_sales"][2015] += 500
    with pytest.raises(AccountingIdentityError) as exc:
        FinancialStatementSchema.model_validate(apple_payload)
    assert_breach(exc, "FY2015 Geographic segments sum = Net sales")


def test_reported_tax_rate_must_match_computed(apple_payload: dict[str, Any]) -> None:
    apple_payload["reported_effective_tax_rates"][2015] = 0.30
    with pytest.raises(AccountingIdentityError) as exc:
        FinancialStatementSchema.model_validate(apple_payload)
    assert_breach(exc, "FY2015 ETR")


def test_debt_schedule_ties_to_balance_sheet(apple_payload: dict[str, Any]) -> None:
    apple_payload["debt_schedule"]["commercial_paper"] += 1_000
    with pytest.raises(AccountingIdentityError) as exc:
        FinancialStatementSchema.model_validate(apple_payload)
    assert_breach(exc, "Commercial paper: schedule = BS")


# --------------------------------------------------------------------------------------
# Debt
# --------------------------------------------------------------------------------------
def test_debt_face_value_reconciles_to_carrying_value(apple: FinancialStatementSchema) -> None:
    ds = apple.debt_schedule
    assert ds is not None
    # 55,701 face - 114 discount + 376 hedge adj. = 2,500 current + 53,463 long-term
    assert ds.total_principal == pytest.approx(55_701)
    assert ds.carrying_value == pytest.approx(55_963)


def test_maturity_ladder_conserves_principal(apple: FinancialStatementSchema) -> None:
    ds = apple.debt_schedule
    assert ds is not None
    assert sum(ds.maturity_ladder().values()) == pytest.approx(ds.total_principal)
    assert sum(ds.principal_by_currency().values()) == pytest.approx(ds.total_principal)


def test_weighted_average_rate_within_tranche_bounds(apple: FinancialStatementSchema) -> None:
    ds = apple.debt_schedule
    assert ds is not None
    lo = min(t.effective_rate_low for t in ds.tranches)
    hi = max(t.effective_rate_high for t in ds.tranches)
    assert lo <= ds.weighted_average_effective_rate <= hi


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"maturity_year_start": 2030, "maturity_year_end": 2020}, "maturity range"),
        ({"effective_rate_low": 0.05, "effective_rate_high": 0.01}, "effective rate range"),
        ({"coupon_rate_low": None, "coupon_rate_high": None}, "requires a coupon"),
    ],
)
def test_invalid_tranche_rejected(overrides: dict[str, Any], message: str) -> None:
    base = {
        "name": "Test note", "instrument_type": InstrumentType.FIXED_RATE_NOTE,
        "principal": 100.0, "coupon_rate_low": 0.03, "coupon_rate_high": 0.03,
        "effective_rate_low": 0.031, "effective_rate_high": 0.031,
        "maturity_year_start": 2025, "maturity_year_end": 2025,
    }
    with pytest.raises(ValidationError, match=message):
        DebtTranche(**{**base, **overrides})


# --------------------------------------------------------------------------------------
# Macro assumptions
# --------------------------------------------------------------------------------------
def test_assumptions_config_loads() -> None:
    assumptions = ModelAssumptions.from_json(CONFIG)
    macro = assumptions.market
    assert macro.cost_of_equity == pytest.approx(
        macro.risk_free_rate + macro.levered_beta * macro.equity_risk_premium
    )
    assert set(macro.sources) >= {"risk_free_rate", "equity_risk_premium", "levered_beta"}
    assert len(assumptions.operating.revenue_growth) == assumptions.operating.projection_years


def test_terminal_growth_must_be_below_cost_of_equity() -> None:
    with pytest.raises(ValidationError, match="terminal_growth_rate"):
        MacroAssumptions(
            valuation_date="2015-09-26", risk_free_rate=0.01, equity_risk_premium=0.02,
            levered_beta=1.0, marginal_tax_rate=0.25, terminal_growth_rate=0.04,
            exit_ev_ebitda_multiple=10.0, share_price=100.0,
        )


def _operating(**overrides: Any) -> dict[str, Any]:
    base = {"revenue_growth": 0.03, "gross_margin": 0.40, "rd_pct_sales": 0.05,
            "sga_pct_sales": 0.10, "da_pct_sales": 0.04, "capex_pct_sales": 0.05,
            "tax_rate": 0.25}
    return {**base, **overrides}


def test_scalar_drivers_broadcast_to_projection_years() -> None:
    op = OperatingAssumptions(**_operating(projection_years=7))
    assert op.sga_pct_sales == [0.10] * 7


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"revenue_growth": [0.03, 0.04]}, "expected 5 yearly values"),
        ({"gross_margin": 0.12}, "gross margin"),  # R&D + SG&A exceed gross margin
    ],
)
def test_invalid_operating_assumptions(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        OperatingAssumptions(**_operating(**overrides))


def test_tranche_shares_must_sum_to_one() -> None:
    config = ModelAssumptions.from_json(CONFIG).transaction.model_dump()
    config["tranches"][0]["share_of_debt"] = 0.5
    with pytest.raises(ValidationError, match="sum to 100%"):
        TransactionAssumptions(**config)


def test_holding_period_cannot_exceed_projection() -> None:
    config = ModelAssumptions.from_json(CONFIG).model_dump()
    config["transaction"]["holding_period_years"] = 7
    with pytest.raises(ValidationError, match="holding_period_years"):
        ModelAssumptions.model_validate(config)


def test_repatriation_tax_rate_implied_by_filing(apple: FinancialStatementSchema) -> None:
    # $30.0B unrecognized deferred tax liability / $91.5B undistributed foreign earnings
    assert apple.implied_repatriation_tax_rate == pytest.approx(30.0 / 91.5)
    assert apple.diluted_shares_outstanding == pytest.approx(5_578.753 + 5_793.069 - 5_753.421)
