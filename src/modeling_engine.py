"""Dynamic 3-statement projection, DCF valuation and LBO returns engine.

Three layers, each a pure function of the layers below it:

1. :func:`project_statements`: drives a linked Income Statement, Balance Sheet and
   Cash Flow Statement forward from the last reported year. Every projected
   statement is instantiated as the same Pydantic schema used for the historicals,
   so ``Assets = Liabilities + Equity`` and all cash ties are re-verified each year.
2. :func:`run_dcf`: unlevered FCF, CAPM-based WACC, terminal value by both the
   Gordon Growth and Exit Multiple methods, and the bridge to implied share price.
3. :func:`run_lbo`: Sources & Uses, a multi-tranche debt schedule with a sequential
   cash sweep, credit statistics with covenant headroom, and sponsor MOIC / IRR.

Modelling conventions (also stated on the Excel cover sheet)
------------------------------------------------------------
- **No circularity.** Interest expense and interest income are computed on
  *beginning-of-year* balances, so the model needs no iterative calculation.
- **Funded debt is refinanced** in the standalone 3-statement projection (held flat);
  in the LBO it is fully refinanced at close.
- **D&A** is applied against PP&E; goodwill and intangibles are held flat.
- **Working capital** items are driven by days (receivables on sales, inventory and
  payables on COGS) or % of sales, defaulting to base-year actuals.
- **Diluted shares** are held flat at period-end shares plus base-year dilution.

Usage::

    python -m src.modeling_engine data/raw/apple_10k_fy2015.pdf --ticker AAPL
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from src import metrics
from src.exceptions import AssumptionError, FinancialDataError
from src.schemas import (
    BalanceSheet,
    CashFlowStatement,
    FinancialStatementSchema,
    IncomeStatement,
    MacroAssumptions,
    ModelAssumptions,
)

logger = logging.getLogger(__name__)

DAYS_IN_YEAR: float = 365.0

# --------------------------------------------------------------------------------------
# Driver definitions (shared with the Excel export so both use one vocabulary)
# --------------------------------------------------------------------------------------
WORKING_CAPITAL_DRIVERS: dict[str, tuple[str, str, str]] = {
    # driver key: (balance sheet field, income statement base, "days" | "pct")
    "receivable_days": ("accounts_receivable", "net_sales", "days"),
    "inventory_days": ("inventories", "cost_of_sales", "days"),
    "payable_days": ("accounts_payable", "cost_of_sales", "days"),
    "vendor_receivables_pct_sales": ("vendor_non_trade_receivables", "net_sales", "pct"),
    "other_current_assets_pct_sales": ("other_current_assets", "net_sales", "pct"),
    "accrued_expenses_pct_sales": ("accrued_expenses", "net_sales", "pct"),
    "deferred_revenue_pct_sales": ("deferred_revenue_current", "net_sales", "pct"),
}

OPERATING_DRIVERS: tuple[str, ...] = (
    "revenue_growth", "gross_margin", "rd_pct_sales", "sga_pct_sales",
    "other_opex_pct_sales", "da_pct_sales", "capex_pct_sales", "tax_rate",
)

HELD_FLAT_BALANCES: tuple[str, ...] = (
    "short_term_investments", "deferred_tax_assets_current", "long_term_investments",
    "goodwill", "commercial_paper", "current_portion_long_term_debt",
    "deferred_revenue_non_current", "long_term_debt", "other_non_current_liabilities",
    "common_stock_and_apic", "accumulated_other_comprehensive_income", "other_equity",
    "noncontrolling_interest",
)
"""Balance sheet lines with no operating driver; held at base-year values.

Also driven, but not by an operating ratio: net PP&E, intangibles and other
non-current assets absorb capex less D&A (in that order, see
:func:`depreciate_fixed_assets`), and short-term debt carries revolver draws."""

FUNDED_DEBT: tuple[str, ...] = (
    "commercial_paper", "short_term_debt", "current_portion_long_term_debt", "long_term_debt",
)
"""Balance sheet lines that bear interest."""

MAX_IMPLIED_COST_OF_DEBT: float = 0.25
"""Cap on the cost of debt implied from interest expense (guards against tiny debt)."""


def working_capital_ratio(balance: float, base: float, kind: str) -> float:
    """Convert a working-capital balance into its driver (days or % of base).

    Args:
        balance: Balance sheet amount.
        base: Income statement base (sales or COGS) for the same year.
        kind: ``"days"`` or ``"pct"``.

    Returns:
        Days outstanding or the balance as a share of the base.
    """
    ratio = metrics.safe_divide(balance, base)
    return ratio * DAYS_IN_YEAR if kind == "days" else ratio


def working_capital_balance(driver: float, base: float, kind: str) -> float:
    """Inverse of :func:`working_capital_ratio`: driver -> balance."""
    return base * driver / DAYS_IN_YEAR if kind == "days" else base * driver


def base_working_capital_drivers(schema: FinancialStatementSchema, year: int) -> dict[str, float]:
    """Actual working-capital drivers for a historical year.

    Args:
        schema: Validated historical data.
        year: Fiscal year with both an income statement and a balance sheet.

    Returns:
        Mapping of driver key to its actual value.
    """
    inc, bs = schema.income_statement(year), schema.balance_sheet(year)
    return {
        key: working_capital_ratio(getattr(bs, field), getattr(inc, base), kind)
        for key, (field, base, kind) in WORKING_CAPITAL_DRIVERS.items()
    }


def depreciate_fixed_assets(ppe: float, intangibles: float, other: float, capex: float,
                            da: float) -> tuple[float, float, float]:
    """Roll forward net PP&E, intangibles and other non-current assets.

    Capex adds to PP&E and D&A reduces it. D&A beyond the available PP&E (common
    where D&A is mostly amortization of acquired intangibles) reduces intangibles,
    then other non-current assets, so no asset balance turns negative.

    Returns:
        ``(ppe, intangibles, other_non_current_assets)`` at year end.
    """
    excess = max(da - capex - ppe, 0.0)            # D&A not absorbed by PP&E
    new_ppe = max(ppe + capex - da, 0.0)
    from_intangibles = min(excess, intangibles)
    return new_ppe, intangibles - from_intangibles, other - (excess - from_intangibles)


def implied_cost_of_debt(schema: FinancialStatementSchema) -> float | None:
    """Latest-year interest expense / average funded debt, capped at
    :data:`MAX_IMPLIED_COST_OF_DEBT`. ``None`` if interest expense is not reported
    or the average debt cannot be computed."""
    y0 = schema.latest_year
    interest = schema.reported_interest_expense.get(y0)
    years = sorted(b.fiscal_year for b in schema.balance_sheets)
    if interest is None or y0 - 1 not in years:
        return None
    avg_debt = (schema.balance_sheet(y0).total_debt + schema.balance_sheet(y0 - 1).total_debt) / 2
    if avg_debt <= 0:
        return 0.0
    return min(max(interest / avg_debt, 0.0), MAX_IMPLIED_COST_OF_DEBT)


def blended_cost_of_debt(schema: FinancialStatementSchema, market: MacroAssumptions) -> float:
    """Pre-tax cost of debt, in order of preference:

    1. the override in ``market.pre_tax_cost_of_debt``;
    2. the principal-weighted effective rate from the extracted debt note;
    3. the rate implied by interest expense over average funded debt.

    Raises:
        AssumptionError: If the company has debt and none of the above is available.
    """
    if market.pre_tax_cost_of_debt is not None:
        return market.pre_tax_cost_of_debt
    ds = schema.debt_schedule
    if ds is None:
        implied = implied_cost_of_debt(schema)
        if implied is not None:
            return implied
        if schema.balance_sheet(schema.latest_year).total_debt > 0:
            raise AssumptionError(
                "Company has debt but neither a debt note nor interest expense was "
                "extracted; set market.pre_tax_cost_of_debt"
            )
        return 0.0
    cp_rate = ds.commercial_paper_rate or 0.0
    weighted = sum(t.principal * t.effective_rate for t in ds.tranches)
    return metrics.safe_divide(weighted + ds.commercial_paper * cp_rate,
                               ds.total_principal + ds.commercial_paper)


def repatriation_tax_rate(schema: FinancialStatementSchema, market: MacroAssumptions) -> float:
    """Repatriation tax rate: the override, else the rate implied by the filing."""
    if market.repatriation_tax_rate is not None:
        return market.repatriation_tax_rate
    return schema.implied_repatriation_tax_rate or 0.0


# --------------------------------------------------------------------------------------
# 1. Three-statement projection
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Projection:
    """Projected, validated financial statements.

    Attributes:
        base_year: Last historical fiscal year.
        years: Projected fiscal years.
        drivers: Resolved drivers (index = driver key, columns = years).
        income: Income statement lines (index = schema field / line key).
        balance: Balance sheet lines (index = ``BalanceSheet`` field).
        cash_flow: Cash flow lines (index = ``CashFlowStatement`` field).
        statements: The validated schema objects per year.
    """

    base_year: int
    years: list[int]
    drivers: pd.DataFrame
    income: pd.DataFrame
    balance: pd.DataFrame
    cash_flow: pd.DataFrame
    statements: dict[int, tuple[IncomeStatement, BalanceSheet, CashFlowStatement]]

    def line(self, statement: str, key: str, year: int) -> float:
        """Convenience accessor, e.g. ``line("income", "net_sales", 2016)``."""
        return float(getattr(self, statement).at[key, year])


def resolve_drivers(schema: FinancialStatementSchema, assumptions: ModelAssumptions,
                    years: Sequence[int]) -> pd.DataFrame:
    """Combine configured drivers with base-year defaults into one table.

    Args:
        schema: Historical data (for base-year working capital and portfolio yield).
        assumptions: Full assumption set.
        years: Projection years.

    Returns:
        DataFrame indexed by driver key with one column per projection year.

    Raises:
        AssumptionError: If no interest income yield is configured or disclosed.
    """
    op = assumptions.operating
    base_wc = base_working_capital_drivers(schema, schema.latest_year)

    yield_ = op.interest_income_yield
    if yield_ is None:
        yield_ = schema.investment_portfolio_yield
    if yield_ is None:
        raise AssumptionError("Set operating.interest_income_yield: not disclosed in the filing")

    rows: dict[str, list[float]] = {name: list(getattr(op, name)) for name in OPERATING_DRIVERS}
    for key in WORKING_CAPITAL_DRIVERS:
        override = getattr(op, key)
        rows[key] = [base_wc[key] if override is None else override] * len(years)
    rows["interest_income_yield"] = [yield_] * len(years)
    rows["cost_of_debt"] = [blended_cost_of_debt(schema, assumptions.market)] * len(years)
    rows["dividend_payout_ratio"] = [op.dividend_payout_ratio] * len(years)
    rows["buyback_pct_net_income"] = [op.buyback_pct_net_income] * len(years)
    return pd.DataFrame(rows, index=list(years)).T


def project_statements(schema: FinancialStatementSchema,
                       assumptions: ModelAssumptions) -> Projection:
    """Project linked financial statements over the projection period.

    Linkages (per year *t*):

    - Revenue grows at the driver rate; COGS, R&D, SG&A, D&A and capex follow sales.
    - Interest income = yield x opening cash & investments; interest expense =
      cost of debt x opening funded debt (beginning balances: no circularity).
    - Net income -> retained earnings (less dividends and buybacks).
    - PP&E roll-forward: opening + capex - D&A.
    - Working capital balances from days / % drivers; their changes flow to CFO.
    - Cash on the balance sheet is the ending cash of the cash flow statement; the
      balance sheet must then balance with no plug.

    Args:
        schema: Validated historical data.
        assumptions: Full assumption set.

    Returns:
        The projection, with each year's statements validated by the Phase 1 schemas.

    Raises:
        AccountingIdentityError: If any projected statement fails an identity check.
    """
    y0 = schema.latest_year
    years = [y0 + i for i in range(1, assumptions.operating.projection_years + 1)]
    drivers = resolve_drivers(schema, assumptions, years)
    shares = schema.diluted_shares_outstanding

    prev_sales = schema.income_statement(y0).net_sales
    prev = schema.balance_sheet(y0).model_dump()
    income_rows: dict[int, dict[str, float]] = {}
    balance_rows: dict[int, dict[str, float]] = {}
    cash_rows: dict[int, dict[str, float]] = {}
    statements: dict[int, tuple[IncomeStatement, BalanceSheet, CashFlowStatement]] = {}

    for y in years:
        d = drivers[y]

        # ---- Income statement
        sales = prev_sales * (1 + d["revenue_growth"])
        cogs = sales * (1 - d["gross_margin"])
        rd = sales * d["rd_pct_sales"]
        sga = sales * d["sga_pct_sales"]
        other_opex = sales * d["other_opex_pct_sales"]
        opex = rd + sga + other_opex
        ebit = (sales - cogs) - opex
        da = sales * d["da_pct_sales"]
        interest_income = d["interest_income_yield"] * (
            prev["cash_and_equivalents"] + prev["short_term_investments"]
            + prev["long_term_investments"]
        )
        interest_expense = d["cost_of_debt"] * sum(prev[k] for k in FUNDED_DEBT)
        pretax = ebit + interest_income - interest_expense
        tax = pretax * d["tax_rate"]
        net_income = pretax - tax
        capex = sales * d["capex_pct_sales"]

        inc = {
            "net_sales": sales, "cost_of_sales": cogs, "gross_margin": sales - cogs,
            "research_and_development": rd, "selling_general_administrative": sga,
            "other_operating_expenses": other_opex,
            "total_operating_expenses": opex, "operating_income": ebit,
            "depreciation_and_amortization": da, "ebitda": ebit + da,
            "interest_income": interest_income, "interest_expense": interest_expense,
            "other_income_expense": interest_income - interest_expense,
            "pretax_income": pretax, "income_tax_expense": tax, "net_income": net_income,
            "diluted_shares": shares, "eps_diluted": net_income / shares,
        }

        # ---- Balance sheet (everything except cash)
        bal = {k: prev[k] for k in HELD_FLAT_BALANCES}
        for key, (field, base, kind) in WORKING_CAPITAL_DRIVERS.items():
            bal[field] = working_capital_balance(d[key], inc[base], kind)
        bal["ppe_net"], bal["intangible_assets"], bal["other_non_current_assets"] = (
            depreciate_fixed_assets(prev["ppe_net"], prev["intangible_assets"],
                                    prev["other_non_current_assets"], capex, da))

        # ---- Cash flow statement
        def delta(field: str, _bal: dict[str, float] = bal) -> float:
            return _bal[field] - prev[field]

        cf = {
            "fiscal_year": y, "beginning_cash": prev["cash_and_equivalents"],
            "net_income": net_income, "depreciation_and_amortization": da,
            "change_accounts_receivable": -delta("accounts_receivable"),
            "change_inventories": -delta("inventories"),
            "change_vendor_non_trade_receivables": -delta("vendor_non_trade_receivables"),
            "change_other_assets": -delta("other_current_assets"),
            "change_accounts_payable": delta("accounts_payable"),
            "change_deferred_revenue": delta("deferred_revenue_current"),
            "change_other_liabilities": delta("accrued_expenses"),
            "capital_expenditures": -capex,
        }
        cf["cash_from_operations"] = sum(cf.get(f, 0.0) for f in CashFlowStatement.OPERATING)
        cf["cash_from_investing"] = sum(cf.get(f, 0.0) for f in CashFlowStatement.INVESTING)

        # Term debt is held flat. A cash shortfall before distributions is drawn on a
        # revolver (short-term debt, interest from next year); distributions apply to
        # positive earnings and are funded from the cash left, dividends first.
        pre_financing = (cf["beginning_cash"] + cf["cash_from_operations"]
                         + cf["cash_from_investing"])
        revolver_draw = max(-pre_financing, 0.0)
        available = max(pre_financing, 0.0)
        dividends = min(max(net_income, 0.0) * d["dividend_payout_ratio"], available)
        buybacks = min(max(net_income, 0.0) * d["buyback_pct_net_income"],
                       max(available - dividends, 0.0))
        cf["proceeds_from_debt_net"] = revolver_draw
        cf["dividends_paid"], cf["share_repurchases"] = -dividends, -buybacks
        cf["cash_from_financing"] = sum(cf.get(f, 0.0) for f in CashFlowStatement.FINANCING)
        bal["short_term_debt"] = prev["short_term_debt"] + revolver_draw
        bal["retained_earnings"] = prev["retained_earnings"] + net_income - dividends - buybacks
        cf["net_change_in_cash"] = (cf["cash_from_operations"] + cf["cash_from_investing"]
                                    + cf["cash_from_financing"])
        cf["ending_cash"] = cf["beginning_cash"] + cf["net_change_in_cash"]
        if -1e-6 < cf["ending_cash"] < 0:  # floating-point residue when cash is fully used
            cf["ending_cash"] = 0.0

        # ---- Close the balance sheet with CFS cash, then compute subtotals
        bal["cash_and_equivalents"] = cf["ending_cash"]
        bal["total_current_assets"] = sum(bal[f] for f in BalanceSheet.CURRENT_ASSETS)
        bal["total_assets"] = bal["total_current_assets"] + sum(
            bal[f] for f in BalanceSheet.NON_CURRENT_ASSETS)
        bal["total_current_liabilities"] = sum(bal[f] for f in BalanceSheet.CURRENT_LIABILITIES)
        bal["total_liabilities"] = bal["total_current_liabilities"] + sum(
            bal[f] for f in BalanceSheet.NON_CURRENT_LIABILITIES)
        bal["total_equity"] = sum(bal[f] for f in BalanceSheet.EQUITY)
        bal["total_liabilities_and_equity"] = bal["total_liabilities"] + bal["total_equity"]

        # ---- Validate with the same schemas used for reported financials
        is_fields = set(IncomeStatement.model_fields)
        statements[y] = (
            IncomeStatement(fiscal_year=y, **{k: v for k, v in inc.items() if k in is_fields}),
            BalanceSheet(fiscal_year=y, **bal),
            CashFlowStatement(**cf),
        )
        income_rows[y], balance_rows[y] = inc, bal
        cash_rows[y] = {k: v for k, v in cf.items() if k != "fiscal_year"}
        prev, prev_sales = {**bal, "fiscal_year": y}, sales

    logger.info("Projected FY%d-FY%d: all balance sheets balance", years[0], years[-1])
    cash_flow = pd.DataFrame(cash_rows).reindex(
        [f for f in CashFlowStatement.model_fields if f != "fiscal_year"]).fillna(0.0)
    return Projection(
        base_year=y0, years=years, drivers=drivers,
        income=pd.DataFrame(income_rows),
        balance=pd.DataFrame(balance_rows).reindex(
            [f for f in BalanceSheet.model_fields if f != "fiscal_year"]),
        cash_flow=cash_flow.drop(["cash_taxes_paid", "cash_interest_paid"]),
        statements=statements,
    )


# --------------------------------------------------------------------------------------
# 2. DCF
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WACCResult:
    """Weighted average cost of capital and its build-up."""

    share_price: float
    diluted_shares: float
    market_value_of_equity: float
    total_debt: float
    equity_weight: float
    debt_weight: float
    cost_of_equity: float
    pre_tax_cost_of_debt: float
    tax_rate: float
    after_tax_cost_of_debt: float
    wacc: float


@dataclass(frozen=True)
class TerminalValueResult:
    """Valuation under one terminal value method."""

    method: str
    terminal_value: float
    pv_terminal_value: float
    enterprise_value: float
    equity_value: float
    implied_share_price: float
    premium_to_current: float
    pv_terminal_share_of_ev: float
    implied_cross_check: float
    """Gordon: implied exit EV/EBITDA multiple. Exit multiple: implied perpetual growth."""


@dataclass(frozen=True)
class DCFResult:
    """Full DCF output."""

    wacc: WACCResult
    terminal_growth_rate: float
    exit_multiple: float
    schedule: pd.DataFrame
    sum_pv_fcf: float
    cash_and_investments: float
    total_debt: float
    noncontrolling_interest: float
    repatriation_tax: float
    gordon: TerminalValueResult
    exit: TerminalValueResult


def compute_wacc(schema: FinancialStatementSchema, market: MacroAssumptions,
                 cost_of_debt: float) -> WACCResult:
    """WACC = E/V x Ke + D/V x Kd x (1 - t), with Ke from CAPM.

    Equity is valued at market (share price x diluted shares); debt at book value,
    a standard proxy for investment-grade debt trading near par.

    Args:
        schema: Historical data (shares and debt).
        market: Market assumptions.
        cost_of_debt: Pre-tax cost of debt.

    Returns:
        The WACC build-up.
    """
    shares = schema.diluted_shares_outstanding
    equity = market.share_price * shares
    debt = schema.balance_sheet(schema.latest_year).total_debt
    total = equity + debt
    e_w, d_w = equity / total, debt / total
    kd_after_tax = cost_of_debt * (1 - market.marginal_tax_rate)
    return WACCResult(
        share_price=market.share_price, diluted_shares=shares,
        market_value_of_equity=equity, total_debt=debt,
        equity_weight=e_w, debt_weight=d_w,
        cost_of_equity=market.cost_of_equity, pre_tax_cost_of_debt=cost_of_debt,
        tax_rate=market.marginal_tax_rate, after_tax_cost_of_debt=kd_after_tax,
        wacc=e_w * market.cost_of_equity + d_w * kd_after_tax,
    )


def free_cash_flow_schedule(projection: Projection) -> pd.DataFrame:
    """Unlevered free cash flow build for each projection year.

    Returns:
        DataFrame with rows ``operating_income, tax_rate, nopat,
        depreciation_and_amortization, capex, change_in_nwc, unlevered_fcf``.
    """
    rows: dict[int, dict[str, float]] = {}
    for y in projection.years:
        inc, _, cf = projection.statements[y]
        tax_rate = float(projection.drivers.at["tax_rate", y])
        rows[y] = {
            "operating_income": inc.operating_income,
            "tax_rate": tax_rate,
            "nopat": metrics.nopat(inc.operating_income, tax_rate),
            "depreciation_and_amortization": cf.depreciation_and_amortization,
            "capex": cf.capex,
            "change_in_nwc": cf.change_in_nwc,
            "unlevered_fcf": metrics.unlevered_free_cash_flow(
                inc.operating_income, tax_rate, cf.depreciation_and_amortization,
                cf.capex, cf.change_in_nwc),
        }
    return pd.DataFrame(rows)


def run_dcf(projection: Projection, schema: FinancialStatementSchema,
            assumptions: ModelAssumptions, *, wacc: float | None = None,
            terminal_growth: float | None = None,
            exit_multiple: float | None = None) -> DCFResult:
    """Discounted cash flow valuation with both terminal value methods.

    Keyword overrides exist so sensitivity tables can re-run the DCF without
    rebuilding the projection.

    Args:
        projection: Output of :func:`project_statements`.
        schema: Historical data (equity bridge items).
        assumptions: Full assumption set.
        wacc: Override the computed WACC.
        terminal_growth: Override the perpetual growth rate.
        exit_multiple: Override the terminal EV/EBITDA multiple.

    Returns:
        The DCF result.

    Raises:
        AssumptionError: If the perpetual growth rate is not below WACC.
    """
    market = assumptions.market
    wacc_result = compute_wacc(schema, market, blended_cost_of_debt(schema, market))
    r = wacc if wacc is not None else wacc_result.wacc
    g = terminal_growth if terminal_growth is not None else market.terminal_growth_rate
    multiple = exit_multiple if exit_multiple is not None else market.exit_ev_ebitda_multiple
    if g >= r:
        raise AssumptionError(f"Terminal growth ({g:.2%}) must be below WACC ({r:.2%})")

    schedule = free_cash_flow_schedule(projection)
    n = len(projection.years)
    periods = [i - 0.5 if market.mid_year_convention else float(i) for i in range(1, n + 1)]
    schedule.loc["discount_period"] = periods
    schedule.loc["discount_factor"] = [1 / (1 + r) ** t for t in periods]
    schedule.loc["pv_fcf"] = schedule.loc["unlevered_fcf"] * schedule.loc["discount_factor"]
    sum_pv = float(schedule.loc["pv_fcf"].sum())

    last = projection.years[-1]
    final_fcf = float(schedule.at["unlevered_fcf", last])
    final_ebitda = projection.line("income", "ebitda", last)
    terminal_discount = 1 / (1 + r) ** n  # terminal value arises at the end of year N

    bs = schema.balance_sheet(schema.latest_year)
    foreign = schema.foreign_cash_and_investments or 0.0
    repat_tax = (foreign * repatriation_tax_rate(schema, market)
                 if market.deduct_repatriation_tax_in_equity_bridge else 0.0)
    shares = wacc_result.diluted_shares

    def value(method: str, tv: float, cross_check: float) -> TerminalValueResult:
        pv_tv = tv * terminal_discount
        ev = sum_pv + pv_tv
        equity = (ev + bs.cash_and_investments - bs.total_debt - bs.noncontrolling_interest
                  - repat_tax)
        price = equity / shares
        return TerminalValueResult(
            method=method, terminal_value=tv, pv_terminal_value=pv_tv,
            enterprise_value=ev, equity_value=equity, implied_share_price=price,
            premium_to_current=price / market.share_price - 1,
            pv_terminal_share_of_ev=pv_tv / ev, implied_cross_check=cross_check,
        )

    tv_gordon = final_fcf * (1 + g) / (r - g)
    tv_exit = final_ebitda * multiple
    return DCFResult(
        wacc=wacc_result, terminal_growth_rate=g, exit_multiple=multiple,
        schedule=schedule, sum_pv_fcf=sum_pv,
        cash_and_investments=bs.cash_and_investments, total_debt=bs.total_debt,
        noncontrolling_interest=bs.noncontrolling_interest, repatriation_tax=repat_tax,
        gordon=value("Gordon Growth", tv_gordon, tv_gordon / final_ebitda),
        exit=value("Exit Multiple", tv_exit,
                   (tv_exit * r - final_fcf) / (tv_exit + final_fcf)),
    )


# --------------------------------------------------------------------------------------
# 3. LBO
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LBOResult:
    """Leveraged buyout output.

    Attributes:
        entry: Entry valuation items (EV, purchase equity, offer price, ...).
        sources: Sources of funds, in display order.
        uses: Uses of funds, in display order.
        schedule: Annual operating, debt and credit-stat rows (columns = years).
        exit: Exit valuation items.
        sponsor_cash_flows: Sponsor equity flows from close (t = 0) to exit.
        moic: Multiple on invested capital.
        irr: Internal rate of return.
        tranche_names: Display names, index-aligned with ``tranche{i}`` row keys.
    """

    entry: dict[str, float]
    sources: dict[str, float]
    uses: dict[str, float]
    schedule: pd.DataFrame
    exit: dict[str, float]
    sponsor_cash_flows: list[float]
    moic: float
    irr: float
    tranche_names: list[str]


def run_lbo(projection: Projection, schema: FinancialStatementSchema,
            assumptions: ModelAssumptions, *, entry_multiple: float | None = None,
            debt_pct: float | None = None, exit_multiple: float | None = None) -> LBOResult:
    """Leveraged buyout: Sources & Uses, debt waterfall and sponsor returns.

    Cash treatment: the target's cash & securities are split into domestic and
    offshore (as disclosed). Domestic cash above the minimum operating balance funds
    the deal; a configurable share of offshore cash is repatriated net of tax; the
    rest stays trapped and is credited at exit net of repatriation tax.

    Waterfall each year: levered FCF -> mandatory amortization -> cash sweep of
    cash above the minimum balance, applied to prepayable tranches in priority order.

    Args:
        projection: Operating projection (EBITDA, D&A, capex, NWC, tax rate).
        schema: Historical data (LTM EBITDA, debt, cash).
        assumptions: Full assumption set.
        entry_multiple: Override entry EV/EBITDA.
        debt_pct: Override total debt as % of entry EV.
        exit_multiple: Override exit EV/EBITDA.

    Returns:
        The LBO result.

    Raises:
        AssumptionError: If sponsor equity would be non-positive.
    """
    tx = assumptions.transaction
    market = assumptions.market
    entry_mult = entry_multiple if entry_multiple is not None else tx.entry_ev_ebitda_multiple
    exit_mult = exit_multiple if exit_multiple is not None else tx.exit_ev_ebitda_multiple
    leverage = debt_pct if debt_pct is not None else tx.debt_pct_of_entry_ev
    y0 = schema.latest_year
    bs = schema.balance_sheet(y0)
    repat_rate = repatriation_tax_rate(schema, market)

    # ---- Entry valuation
    ltm_ebitda = schema.ebitda(y0)
    if ltm_ebitda <= 0:
        raise AssumptionError(
            f"LTM EBITDA is {ltm_ebitda:,.0f}: an LBO needs positive EBITDA to size debt")
    entry_ev = entry_mult * ltm_ebitda
    # Minority interests stay outstanding: the sponsor buys the parent's equity only.
    equity_purchase = (entry_ev - bs.total_debt - bs.noncontrolling_interest
                       + bs.cash_and_investments)
    shares = schema.diluted_shares_outstanding
    offer_price = equity_purchase / shares

    # ---- Cash available at close
    foreign = schema.foreign_cash_and_investments or 0.0
    domestic = bs.cash_and_investments - foreign
    domestic_used = max(domestic - tx.minimum_cash, 0.0)
    opening_cash = domestic - domestic_used
    repatriated = foreign * tx.foreign_cash_usable_pct
    repatriation_tax_paid = repatriated * repat_rate
    trapped_cash = foreign - repatriated

    # ---- Sources & Uses
    new_debt = leverage * entry_ev
    principals = [new_debt * t.share_of_debt for t in tx.tranches]
    advisory = entry_ev * tx.advisory_fee_pct_ev
    financing = new_debt * tx.financing_fee_pct_debt
    uses = {
        "Purchase of equity": equity_purchase,
        "Refinance existing debt": bs.total_debt,
        "Advisory & transaction fees": advisory,
        "Financing fees": financing,
    }
    total_uses = sum(uses.values())
    cash_source = domestic_used + repatriated - repatriation_tax_paid
    sponsor_equity = total_uses - new_debt - cash_source
    if sponsor_equity <= 0:
        raise AssumptionError(
            f"Sponsor equity is {sponsor_equity:,.0f}: debt ({new_debt:,.0f}) plus "
            f"cash used ({cash_source:,.0f}) exceeds total uses ({total_uses:,.0f})"
        )
    sources = {t.name: p for t, p in zip(tx.tranches, principals, strict=True)}
    sources["Existing cash & securities used"] = cash_source
    sources["Sponsor equity"] = sponsor_equity

    entry = {
        "ltm_ebitda": ltm_ebitda, "entry_multiple": entry_mult, "entry_ev": entry_ev,
        "existing_debt": bs.total_debt, "cash_and_investments": bs.cash_and_investments,
        "noncontrolling_interest": bs.noncontrolling_interest,
        "equity_purchase_price": equity_purchase, "diluted_shares": shares,
        "offer_price_per_share": offer_price,
        "offer_premium": offer_price / market.share_price - 1,
        "foreign_cash": foreign, "domestic_cash": domestic,
        "domestic_cash_used": domestic_used, "repatriated_cash": repatriated,
        "repatriation_tax_paid": repatriation_tax_paid, "trapped_cash": trapped_cash,
        "opening_cash": opening_cash, "total_new_debt": new_debt,
        "total_uses": total_uses, "total_sources": sum(sources.values()),
        "debt_to_ebitda": metrics.safe_divide(new_debt, ltm_ebitda),
    }

    # ---- Annual debt schedule
    years = projection.years[: tx.holding_period_years]
    yield_ = float(projection.drivers.at["interest_income_yield", years[0]])
    balances = list(principals)
    cash = opening_cash
    rows: dict[int, dict[str, float]] = {}
    for y in years:
        inc = projection.income[y]
        cf = projection.statements[y][2]
        r: dict[str, float] = {
            "ebitda": inc["ebitda"],
            "depreciation_and_amortization": inc["depreciation_and_amortization"],
            "ebit": inc["operating_income"],
            "interest_income": yield_ * cash,
            "beginning_cash": cash,
        }
        for i, (t, bal) in enumerate(zip(tx.tranches, balances, strict=True)):
            r[f"tranche{i}_beginning"] = bal
            r[f"tranche{i}_interest"] = t.interest_rate * bal
        r["total_interest_expense"] = sum(r[f"tranche{i}_interest"] for i in range(len(balances)))
        r["pretax_income"] = r["ebit"] + r["interest_income"] - r["total_interest_expense"]
        r["tax_rate"] = float(projection.drivers.at["tax_rate", y])
        r["income_taxes"] = max(r["pretax_income"], 0.0) * r["tax_rate"]
        r["net_income"] = r["pretax_income"] - r["income_taxes"]
        r["capex"] = cf.capex
        r["change_in_nwc"] = cf.change_in_nwc
        r["levered_fcf"] = (r["net_income"] + r["depreciation_and_amortization"]
                            - r["capex"] - r["change_in_nwc"])

        mandatory = [min(bal, p * t.mandatory_amortization_pct)
                     for t, bal, p in zip(tx.tranches, balances, principals, strict=True)]
        r["total_mandatory"] = sum(mandatory)
        r["cash_available_for_sweep"] = (cash + r["levered_fcf"] - r["total_mandatory"]
                                         - tx.minimum_cash)
        r["sweep_pool"] = max(r["cash_available_for_sweep"], 0.0) * tx.cash_sweep_pct
        optional: list[float] = []
        for t, bal, mand in zip(tx.tranches, balances, mandatory, strict=True):
            room = r["sweep_pool"] - sum(optional)
            optional.append(min(room, bal - mand) if t.prepayable else 0.0)
        r["total_optional"] = sum(optional)

        for i in range(len(balances)):
            r[f"tranche{i}_mandatory"] = mandatory[i]
            r[f"tranche{i}_optional"] = optional[i]
            balances[i] = balances[i] - mandatory[i] - optional[i]
            r[f"tranche{i}_ending"] = balances[i]
        cash = cash + r["levered_fcf"] - r["total_mandatory"] - r["total_optional"]
        r["ending_cash"] = cash
        r["total_debt"] = sum(balances)
        r["net_debt"] = r["total_debt"] - cash
        r["leverage"] = metrics.safe_divide(r["total_debt"], r["ebitda"])
        r["interest_coverage"] = (r["ebitda"] / r["total_interest_expense"]
                                  if r["total_interest_expense"] else float("nan"))
        # EBITDA cushion: how far EBITDA can fall before each covenant is breached
        r["leverage_covenant"] = tx.max_leverage_covenant
        r["leverage_cushion"] = 1 - (r["total_debt"] / tx.max_leverage_covenant) / r["ebitda"]
        r["coverage_covenant"] = tx.min_interest_coverage_covenant
        r["coverage_cushion"] = 1 - (tx.min_interest_coverage_covenant
                                     * r["total_interest_expense"]) / r["ebitda"]
        rows[y] = r
    schedule = pd.DataFrame(rows)

    # ---- Exit & returns
    last = years[-1]
    exit_ebitda = float(schedule.at["ebitda", last])
    exit_ev = exit_mult * exit_ebitda
    trapped_after_tax = trapped_cash * (1 - repat_rate)
    exit_equity = (exit_ev - schedule.at["total_debt", last] + schedule.at["ending_cash", last]
                   + trapped_after_tax - bs.noncontrolling_interest)
    flows = [-sponsor_equity] + [0.0] * (len(years) - 1) + [float(exit_equity)]
    exit_ = {
        "exit_year": float(last), "exit_ebitda": exit_ebitda, "exit_multiple": exit_mult,
        "exit_ev": exit_ev, "exit_debt": float(schedule.at["total_debt", last]),
        "exit_cash": float(schedule.at["ending_cash", last]),
        "trapped_cash_after_tax": trapped_after_tax, "exit_equity": float(exit_equity),
    }
    moic = exit_equity / sponsor_equity
    irr = metrics.irr(flows) if exit_equity > 0 else -1.0
    return LBOResult(entry=entry, sources=sources, uses=uses, schedule=schedule, exit=exit_,
                     sponsor_cash_flows=flows, moic=float(moic), irr=irr,
                     tranche_names=[t.name for t in tx.tranches])


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ValuationModel:
    """Everything needed to render the workbook and the IC memo."""

    schema: FinancialStatementSchema
    assumptions: ModelAssumptions
    projection: Projection
    dcf: DCFResult
    lbo: LBOResult


def build_model(schema: FinancialStatementSchema,
                assumptions: ModelAssumptions) -> ValuationModel:
    """Run projection, DCF and LBO for one assumption set."""
    projection = project_statements(schema, assumptions)
    return ValuationModel(
        schema=schema, assumptions=assumptions, projection=projection,
        dcf=run_dcf(projection, schema, assumptions),
        lbo=run_lbo(projection, schema, assumptions),
    )


def load_schema(source: Path, ticker: str | None = None) -> FinancialStatementSchema:
    """Load historicals from an extracted JSON file or extract them from a filing."""
    if source.suffix.lower() == ".json":
        return FinancialStatementSchema.model_validate_json(source.read_text(encoding="utf-8"))
    from src.extractor import TenKExtractor  # local import: extraction is optional here

    return TenKExtractor(source, ticker=ticker).extract()


def summarize(model: ValuationModel) -> str:
    """Plain-text summary of the valuation outputs for the console."""
    d, lbo = model.dcf, model.lbo
    p = model.projection
    fmt = lambda v: f"{v:>12,.0f}"  # noqa: E731
    lines = [
        f"{model.schema.metadata.name}: valuation summary (USD millions)",
        "",
        "Projection      " + "".join(f"{'FY' + str(y):>12}" for y in p.years),
        "Net sales       " + "".join(fmt(p.line("income", "net_sales", y)) for y in p.years),
        "EBITDA          " + "".join(fmt(p.line("income", "ebitda", y)) for y in p.years),
        "Unlevered FCF   " + "".join(fmt(d.schedule.at["unlevered_fcf", y]) for y in p.years),
        "",
        f"WACC {d.wacc.wacc:.2%}  (Ke {d.wacc.cost_of_equity:.2%}, "
        f"Kd after tax {d.wacc.after_tax_cost_of_debt:.2%}, E/V {d.wacc.equity_weight:.1%})",
    ]
    for tv in (d.gordon, d.exit):
        lines.append(
            f"DCF {tv.method:<14} EV {tv.enterprise_value:>10,.0f}  "
            f"equity {tv.equity_value:>10,.0f}  ${tv.implied_share_price:,.2f}/sh "
            f"({tv.premium_to_current:+.1%} vs ${model.assumptions.market.share_price:,.2f})"
        )
    lines += [
        "",
        f"LBO entry {lbo.entry['entry_multiple']:.1f}x -> EV {lbo.entry['entry_ev']:,.0f}, "
        f"offer ${lbo.entry['offer_price_per_share']:,.2f}/sh "
        f"({lbo.entry['offer_premium']:+.1%} premium)",
        f"Sponsor equity {lbo.sources['Sponsor equity']:,.0f} -> exit equity "
        f"{lbo.exit['exit_equity']:,.0f}:  MOIC {lbo.moic:.2f}x, IRR {lbo.irr:.1%}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: historicals -> projection, DCF, LBO -> Excel workbook.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Build the 3-statement / DCF / LBO model.")
    parser.add_argument("source", type=Path, help="10-K PDF/text, or extracted JSON")
    parser.add_argument("--ticker", help="Exchange ticker, e.g. AAPL")
    parser.add_argument("--assumptions", type=Path, default=Path("config/assumptions.json"))
    parser.add_argument("--out", type=Path, default=Path("outputs/model.xlsx"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-8s %(name)s: %(message)s")

    try:
        schema = load_schema(args.source, args.ticker)
        model = build_model(schema, ModelAssumptions.from_json(args.assumptions))
    except (FinancialDataError, ValidationError) as exc:
        logger.error("%s", exc)
        return 1

    from src.excel_export import write_workbook

    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_workbook(model, args.out)
    except PermissionError:
        logger.error("Cannot write %s: close it in Excel (or any other program) and rerun.",
                     args.out)
        return 1
    print("\n" + summarize(model) + f"\n\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
