"""Derive a complete, documented assumption set for any company from its filing.

The Apple sample uses a hand-written ``config/assumptions.json``. For any other
company the defaults below are computed from the company's own historicals plus one
market input, the current share price, so a valuation can run straight from a 10-K.
Every value records its derivation in the ``sources`` dict, which the workbook and
memo display. Every value can be overridden.

Default policy
--------------
- **Growth:** the trailing revenue CAGR (clamped to -10% / +20%), fading linearly to
  3% by the final projection year.
- **Margins and cost ratios:** the latest fiscal year's actuals, held flat.
- **Tax:** the latest effective rate, clamped to 10%-30% (25% if not meaningful).
- **Payouts:** the latest dividends and buybacks as a share of net income.
- **Market:** risk-free rate, equity risk premium and beta are generic defaults
  marked VERIFY; the terminal multiple is the company's current trading EV/EBITDA.
- **LBO:** entry at a 30% premium to the current share price; debt sized at 5.0x
  EBITDA, capped at 60% of entry EV; exit at the entry multiple.
"""

from __future__ import annotations

from typing import Any

from src.analyzer import deep_merge
from src.exceptions import AssumptionError
from src.modeling_engine import implied_cost_of_debt
from src.schemas import FinancialStatementSchema, ModelAssumptions

# Generic market defaults (override per run; flagged VERIFY in the sources)
DEFAULT_RISK_FREE = 0.0425
DEFAULT_ERP = 0.050
DEFAULT_BETA = 1.00
DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_CREDIT_SPREAD = 0.015  # over risk-free, when no interest expense is reported
LONG_RUN_GROWTH = 0.03
CONTROL_PREMIUM = 0.30
TARGET_LEVERAGE = 5.0          # x EBITDA
MAX_DEBT_PCT_OF_EV = 0.60
PROJECTION_YEARS = 5


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def trading_enterprise_value(schema: FinancialStatementSchema, share_price: float) -> float:
    """Market value of equity + debt + noncontrolling interests - cash & investments."""
    bs = schema.balance_sheet(schema.latest_year)
    equity = share_price * schema.diluted_shares_outstanding
    return equity + bs.total_debt + bs.noncontrolling_interest - bs.cash_and_investments


def derive_assumptions(schema: FinancialStatementSchema, share_price: float,
                       overrides: dict[str, Any] | None = None) -> ModelAssumptions:
    """Build a validated assumption set from a company's historicals.

    Args:
        schema: Validated historical data (any source).
        share_price: Current share price in USD (needed for WACC weights, the trading
            multiple and the LBO offer premium).
        overrides: Optional nested dict deep-merged over the derived defaults, e.g.
            ``{"market": {"levered_beta": 1.2}}``.

    Raises:
        AssumptionError: If the latest year has an operating loss (the template cannot
            project a margin it would have to invent) or non-positive EBITDA.
    """
    if share_price <= 0:
        raise AssumptionError("Share price must be positive")
    y0 = schema.latest_year
    years = schema.years
    inc = schema.income_statement(y0)
    cf = schema.cash_flow_statement(y0)
    sales = inc.net_sales
    ebitda = schema.ebitda(y0)
    if inc.operating_income <= 0 or ebitda <= 0:
        raise AssumptionError(
            f"FY{y0} operating income is {inc.operating_income:,.0f}m and EBITDA "
            f"{ebitda:,.0f}m. The default projection holds margins at the latest year, so "
            "a loss-making year cannot be projected or levered. Supply operating "
            "assumptions explicitly (overrides) to value this company.")

    # ---- growth: trailing CAGR fading to the long-run rate
    first = schema.income_statement(years[0]).net_sales
    span = len(years) - 1
    cagr = (sales / first) ** (1 / span) - 1 if span and first > 0 else LONG_RUN_GROWTH
    g0 = _clamp(cagr, -0.10, 0.20)
    growth = [g0 + (LONG_RUN_GROWTH - g0) * t / (PROJECTION_YEARS - 1)
              for t in range(PROJECTION_YEARS)]

    # ---- cost structure: latest year
    gm = _clamp(inc.gross_margin / sales, 0.01, 0.999)
    rd = inc.research_and_development / sales
    sga = inc.selling_general_administrative / sales
    other = inc.other_operating_expenses / sales
    da = cf.depreciation_and_amortization / sales
    capex = _clamp(cf.capex / sales, 0.0, 0.5)
    etr = inc.effective_tax_rate
    tax = _clamp(etr, 0.10, 0.30) if 0 < etr < 1 else 0.25
    ni = inc.net_income
    payout = _clamp(-cf.dividends_paid / ni, 0.0, 1.0) if ni > 0 else 0.0
    buyback = _clamp(-cf.share_repurchases / ni, 0.0, 1.5) if ni > 0 else 0.0

    # ---- market and transaction
    ev_trading = trading_enterprise_value(schema, share_price)
    trading_multiple = _clamp(ev_trading / ebitda, 4.0, 30.0)
    bs = schema.balance_sheet(y0)
    offer_equity = share_price * (1 + CONTROL_PREMIUM) * schema.diluted_shares_outstanding
    entry_ev = offer_equity + bs.total_debt + bs.noncontrolling_interest - bs.cash_and_investments
    entry_multiple = _clamp(entry_ev / ebitda, 4.0, 30.0)
    debt_pct = _clamp(TARGET_LEVERAGE / entry_multiple, 0.10, MAX_DEBT_PCT_OF_EV)
    rf = DEFAULT_RISK_FREE
    verify = "Generic default (VERIFY against current market data)."
    if implied_cost_of_debt(schema) is None and bs.total_debt > 0:
        kd: float | None = rf + DEFAULT_CREDIT_SPREAD
        kd_source = (f"Interest expense not reported: risk-free + "
                     f"{DEFAULT_CREDIT_SPREAD * 1e4:.0f}bp ({verify})")
    else:
        kd, kd_source = None, "Implied: interest expense / average funded debt."

    base: dict[str, Any] = {
        "market": {
            "valuation_date": schema.metadata.fiscal_year_end.isoformat(),
            "risk_free_rate": rf, "equity_risk_premium": DEFAULT_ERP,
            "levered_beta": DEFAULT_BETA, "pre_tax_cost_of_debt": kd,
            "marginal_tax_rate": tax, "terminal_growth_rate": DEFAULT_TERMINAL_GROWTH,
            "exit_ev_ebitda_multiple": round(trading_multiple, 2), "share_price": share_price,
            "mid_year_convention": True, "repatriation_tax_rate": None,
            "deduct_repatriation_tax_in_equity_bridge": True,
            "sources": {
                "valuation_date": f"Fiscal year end of the FY{y0} 10-K.",
                "risk_free_rate": f"{rf:.2%}: {verify}",
                "equity_risk_premium": f"{DEFAULT_ERP:.2%}: {verify}",
                "levered_beta": f"{DEFAULT_BETA:.2f}: {verify}",
                "pre_tax_cost_of_debt": kd_source,
                "marginal_tax_rate": f"FY{y0} effective tax rate {etr:.1%}, clamped to 10%-30%.",
                "terminal_growth_rate": "Long-run nominal growth assumption.",
                "exit_ev_ebitda_multiple": f"Current trading EV/EBITDA "
                                          f"({ev_trading / ebitda:.1f}x at ${share_price:,.2f}).",
                "share_price": "Entered by the user.",
            },
        },
        "operating": {
            "projection_years": PROJECTION_YEARS,
            "revenue_growth": growth, "gross_margin": gm, "rd_pct_sales": rd,
            "sga_pct_sales": sga, "other_opex_pct_sales": other, "da_pct_sales": da,
            "capex_pct_sales": capex, "tax_rate": tax,
            "dividend_payout_ratio": payout, "buyback_pct_net_income": buyback,
            "interest_income_yield": rf,
            "sources": {
                "revenue_growth": f"FY{years[0]}-FY{y0} revenue CAGR {cagr:.1%} (clamped "
                                  f"{g0:.1%}), fading to {LONG_RUN_GROWTH:.0%} by year 5.",
                "gross_margin": f"FY{y0} actual {inc.gross_margin / sales:.1%}.",
                "rd_pct_sales": f"FY{y0} actual {rd:.1%}.",
                "sga_pct_sales": f"FY{y0} actual {sga:.1%}.",
                "other_opex_pct_sales": f"FY{y0} actual {other:.1%}.",
                "da_pct_sales": f"FY{y0} actual {da:.1%}.",
                "capex_pct_sales": f"FY{y0} actual {cf.capex / sales:.1%}.",
                "tax_rate": "As marginal tax rate.",
                "dividend_payout_ratio": f"FY{y0} dividends / net income {payout:.0%}.",
                "buyback_pct_net_income": f"FY{y0} buybacks / net income {buyback:.0%}.",
                "interest_income_yield": "Cash earns the risk-free rate.",
                "working_capital": f"Days and %-of-sales drivers held at FY{y0} actuals.",
            },
        },
        "transaction": {
            "entry_ev_ebitda_multiple": round(entry_multiple, 2),
            "exit_ev_ebitda_multiple": round(entry_multiple, 2),
            "holding_period_years": PROJECTION_YEARS,
            "debt_pct_of_entry_ev": round(debt_pct, 4),
            "tranches": [
                {"name": "Senior Term Loan B", "seniority": "senior_secured",
                 "share_of_debt": 0.70, "interest_rate": round(rf + 0.035, 4),
                 "mandatory_amortization_pct": 0.01, "prepayable": True},
                {"name": "Senior Notes", "seniority": "senior_unsecured",
                 "share_of_debt": 0.30, "interest_rate": round(rf + 0.055, 4),
                 "mandatory_amortization_pct": 0.0, "prepayable": True},
            ],
            "cash_sweep_pct": 1.0,
            "minimum_cash": round(0.02 * sales, 1),
            "advisory_fee_pct_ev": 0.01, "financing_fee_pct_debt": 0.02,
            "foreign_cash_usable_pct": 0.0,
            "max_leverage_covenant": round(max(6.0, TARGET_LEVERAGE + 1.5), 2),
            "min_interest_coverage_covenant": 2.0,
            "sources": {
                "entry_ev_ebitda_multiple": f"{CONTROL_PREMIUM:.0%} premium to ${share_price:,.2f} "
                                            f"=> {entry_multiple:.1f}x LTM EBITDA.",
                "exit_ev_ebitda_multiple": "Exit at the entry multiple: no multiple expansion.",
                "debt_pct_of_entry_ev": f"{TARGET_LEVERAGE:.1f}x EBITDA, capped at "
                                        f"{MAX_DEBT_PCT_OF_EV:.0%} of entry EV.",
                "tranches": "TLB at risk-free + 350bp, notes at risk-free + 550bp; both "
                            "assumed prepayable at par.",
                "minimum_cash": "2% of revenue.",
                "covenants": "Illustrative maintenance covenants: 6.5x leverage, 2.0x coverage.",
            },
        },
    }
    return ModelAssumptions.model_validate(deep_merge(base, overrides or {}))
