"""Pydantic data contracts for historical financial statements and model assumptions.

Every model in this module is *self-auditing*: on construction it re-derives each
subtotal from its components and checks the core accounting identities. A model that
exists is therefore a model whose numbers tie.

Conventions
-----------
- **Units.** All monetary amounts are in **USD millions**; share counts in **millions**.
- **Signs.** Income statement items are stored as positive magnitudes unless they are
  naturally signed (e.g. ``other_income_expense``). Cash flow and equity roll-forward
  items are stored **as reported** (outflows negative), which keeps every subtotal a
  plain sum and makes the identities easy to audit.
- **Rates** are decimals (``0.264`` = 26.4%).
- **Tolerance.** Filings round each line to the nearest $1M, so identities are checked
  to within :data:`DEFAULT_TOLERANCE`. Override per validation run with
  ``Model.model_validate(data, context={"tolerance": 0.5})``.

Model hierarchy
---------------
``FinancialStatementSchema``
    ├── ``CompanyMetadata``
    ├── ``IncomeStatement``      (one per fiscal year)
    ├── ``BalanceSheet``         (one per fiscal year)
    ├── ``CashFlowStatement``    (one per fiscal year)
    ├── ``EquityRollForward``    (one per fiscal year)
    ├── ``RevenueSegment``       (geographic and product)
    └── ``DebtSchedule``  ──  ``DebtTranche``

``MacroAssumptions`` is independent: it holds market inputs that do not come from
the filing (risk-free rate, ERP, beta, exit multiple, ...).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from src import metrics
from src.exceptions import AccountingIdentityError, IdentityBreach

# --------------------------------------------------------------------------------------
# Tolerances & reusable field types
# --------------------------------------------------------------------------------------
DEFAULT_TOLERANCE: float = 1.0
"""Absolute tolerance (USD millions) for identity checks; absorbs filing rounding."""

RATE_TOLERANCE: float = 0.0006
"""Tolerance for reported vs. recomputed rates (filings round to 0.1 percentage point)."""

EPS_TOLERANCE: float = 0.011
"""Tolerance for reported vs. recomputed EPS (reported to the cent)."""

Money = Annotated[float, Field(allow_inf_nan=False)]
NonNegMoney = Annotated[float, Field(ge=0, allow_inf_nan=False)]
NonPosMoney = Annotated[float, Field(le=0, allow_inf_nan=False)]
Rate = Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]
FiscalYear = Annotated[int, Field(ge=1990, le=2100)]


def _tolerance(info: ValidationInfo | None) -> float:
    """Return the tolerance from the validation context, or the default."""
    if info is not None and isinstance(info.context, dict):
        return float(info.context.get("tolerance", DEFAULT_TOLERANCE))
    return DEFAULT_TOLERANCE


def enforce_identities(
    context: str,
    checks: Iterable[tuple[str, float, float]],
    tolerance: float = DEFAULT_TOLERANCE,
) -> None:
    """Evaluate a batch of ``lhs == rhs`` identities and raise on any breach.

    All checks are evaluated before raising, so the error lists every failure at once.

    Args:
        context: Where the checks are run, e.g. ``"Balance Sheet FY2015"``.
        checks: Iterable of ``(identity description, lhs, rhs)`` tuples.
        tolerance: Maximum allowed absolute difference.

    Raises:
        AccountingIdentityError: If any ``|lhs - rhs| > tolerance``.
    """
    breaches = [
        IdentityBreach(identity=name, lhs=lhs, rhs=rhs, tolerance=tolerance)
        for name, lhs, rhs in checks
        if abs(lhs - rhs) > tolerance + 1e-9
    ]
    if breaches:
        raise AccountingIdentityError(context, breaches)


class StrictModel(BaseModel):
    """Base model: immutable, rejects unknown fields, forbids NaN/inf."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    def _sum(self, fields: Iterable[str]) -> float:
        """Sum the values of the named fields."""
        return sum(getattr(self, f) for f in fields)


# --------------------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------------------
class CompanyMetadata(StrictModel):
    """Identifying information about the issuer and the source document."""

    name: str = Field(min_length=1, description="Registrant name, e.g. 'Apple Inc.'")
    ticker: str | None = Field(default=None, pattern=r"^[A-Z.\-]{1,10}$")
    cik: str | None = Field(default=None, pattern=r"^\d{1,10}$", description="SEC CIK")
    filing_type: str = "10-K"
    fiscal_year_end: date = Field(description="Balance sheet date of the latest year")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    units: Literal["millions"] = "millions"
    source_document: str = Field(description="Path or URL of the source filing")


# --------------------------------------------------------------------------------------
# Income statement
# --------------------------------------------------------------------------------------
class IncomeStatement(StrictModel):
    """One fiscal year of the consolidated statement of operations."""

    fiscal_year: FiscalYear
    net_sales: NonNegMoney
    cost_of_sales: NonNegMoney
    gross_margin: Money
    research_and_development: NonNegMoney = 0.0
    selling_general_administrative: NonNegMoney
    total_operating_expenses: NonNegMoney
    operating_income: Money = Field(description="EBIT: excludes other income/expense")
    other_income_expense: Money = 0.0
    pretax_income: Money
    income_tax_expense: Money
    net_income: Money
    basic_shares: NonNegMoney | None = Field(default=None, description="Millions")
    diluted_shares: float = Field(gt=0, description="Weighted-average diluted, millions")
    eps_basic: Money | None = None
    eps_diluted: Money | None = None
    dividends_per_share: NonNegMoney | None = None

    _OPEX: ClassVar[tuple[str, ...]] = (
        "research_and_development",
        "selling_general_administrative",
    )

    @model_validator(mode="after")
    def _check_identities(self, info: ValidationInfo) -> IncomeStatement:
        """Verify gross margin, opex, EBIT, pre-tax and net income build-ups."""
        tol = _tolerance(info)
        enforce_identities(
            f"Income Statement FY{self.fiscal_year}",
            [
                ("Gross margin = Net sales - COGS",
                 self.gross_margin, self.net_sales - self.cost_of_sales),
                ("Total opex = R&D + SG&A",
                 self.total_operating_expenses, self._sum(self._OPEX)),
                ("Operating income = Gross margin - Total opex",
                 self.operating_income, self.gross_margin - self.total_operating_expenses),
                ("Pre-tax income = Operating income + Other income",
                 self.pretax_income, self.operating_income + self.other_income_expense),
                ("Net income = Pre-tax income - Tax",
                 self.net_income, self.pretax_income - self.income_tax_expense),
            ],
            tol,
        )
        if self.eps_diluted is not None:
            enforce_identities(
                f"Income Statement FY{self.fiscal_year}",
                [("Diluted EPS = Net income / Diluted shares",
                  self.eps_diluted, self.net_income / self.diluted_shares)],
                EPS_TOLERANCE,
            )
        return self

    @property
    def ebit(self) -> float:
        """EBIT, defined as reported operating income."""
        return self.operating_income

    @property
    def gross_margin_pct(self) -> float:
        """Gross margin as a share of net sales."""
        return metrics.safe_divide(self.gross_margin, self.net_sales)

    @property
    def operating_margin(self) -> float:
        """EBIT margin."""
        return metrics.safe_divide(self.operating_income, self.net_sales)

    @property
    def effective_tax_rate(self) -> float:
        """Book effective tax rate = tax expense / pre-tax income."""
        return metrics.effective_tax_rate(self.income_tax_expense, self.pretax_income)


# --------------------------------------------------------------------------------------
# Balance sheet
# --------------------------------------------------------------------------------------
class BalanceSheet(StrictModel):
    """Consolidated balance sheet at one fiscal year end."""

    fiscal_year: FiscalYear

    # Current assets
    cash_and_equivalents: NonNegMoney
    short_term_investments: NonNegMoney = 0.0
    accounts_receivable: NonNegMoney = 0.0
    inventories: NonNegMoney = 0.0
    deferred_tax_assets_current: NonNegMoney = 0.0
    vendor_non_trade_receivables: NonNegMoney = 0.0
    other_current_assets: NonNegMoney = 0.0
    total_current_assets: NonNegMoney

    # Non-current assets
    long_term_investments: NonNegMoney = 0.0
    ppe_net: NonNegMoney = 0.0
    goodwill: NonNegMoney = 0.0
    intangible_assets: NonNegMoney = 0.0
    other_non_current_assets: NonNegMoney = 0.0
    total_assets: NonNegMoney

    # Current liabilities
    accounts_payable: NonNegMoney = 0.0
    accrued_expenses: NonNegMoney = 0.0
    deferred_revenue_current: NonNegMoney = 0.0
    commercial_paper: NonNegMoney = 0.0
    current_portion_long_term_debt: NonNegMoney = 0.0
    total_current_liabilities: NonNegMoney

    # Non-current liabilities
    deferred_revenue_non_current: NonNegMoney = 0.0
    long_term_debt: NonNegMoney = 0.0
    other_non_current_liabilities: NonNegMoney = 0.0
    total_liabilities: NonNegMoney

    # Shareholders' equity
    common_stock_and_apic: Money = 0.0
    retained_earnings: Money = 0.0
    accumulated_other_comprehensive_income: Money = 0.0
    total_equity: Money
    total_liabilities_and_equity: NonNegMoney

    _CURRENT_ASSETS: ClassVar[tuple[str, ...]] = (
        "cash_and_equivalents", "short_term_investments", "accounts_receivable",
        "inventories", "deferred_tax_assets_current", "vendor_non_trade_receivables",
        "other_current_assets",
    )
    _NON_CURRENT_ASSETS: ClassVar[tuple[str, ...]] = (
        "long_term_investments", "ppe_net", "goodwill", "intangible_assets",
        "other_non_current_assets",
    )
    _CURRENT_LIABILITIES: ClassVar[tuple[str, ...]] = (
        "accounts_payable", "accrued_expenses", "deferred_revenue_current",
        "commercial_paper", "current_portion_long_term_debt",
    )
    _NON_CURRENT_LIABILITIES: ClassVar[tuple[str, ...]] = (
        "deferred_revenue_non_current", "long_term_debt", "other_non_current_liabilities",
    )
    _EQUITY: ClassVar[tuple[str, ...]] = (
        "common_stock_and_apic", "retained_earnings",
        "accumulated_other_comprehensive_income",
    )

    @model_validator(mode="after")
    def _check_identities(self, info: ValidationInfo) -> BalanceSheet:
        """Verify every subtotal and the fundamental identity A = L + E."""
        enforce_identities(
            f"Balance Sheet FY{self.fiscal_year}",
            [
                ("Total current assets = sum of current assets",
                 self.total_current_assets, self._sum(self._CURRENT_ASSETS)),
                ("Total assets = Current + Non-current assets",
                 self.total_assets,
                 self.total_current_assets + self._sum(self._NON_CURRENT_ASSETS)),
                ("Total current liabilities = sum of current liabilities",
                 self.total_current_liabilities, self._sum(self._CURRENT_LIABILITIES)),
                ("Total liabilities = Current + Non-current liabilities",
                 self.total_liabilities,
                 self.total_current_liabilities + self._sum(self._NON_CURRENT_LIABILITIES)),
                ("Total equity = APIC + Retained earnings + AOCI",
                 self.total_equity, self._sum(self._EQUITY)),
                ("Total L&E = Total liabilities + Total equity",
                 self.total_liabilities_and_equity, self.total_liabilities + self.total_equity),
                ("Assets = Liabilities + Equity",
                 self.total_assets, self.total_liabilities + self.total_equity),
            ],
            _tolerance(info),
        )
        return self

    @property
    def total_debt(self) -> float:
        """Commercial paper + current and long-term funded debt (carrying value)."""
        return self.commercial_paper + self.current_portion_long_term_debt + self.long_term_debt

    @property
    def cash_and_investments(self) -> float:
        """Cash plus short- and long-term marketable securities."""
        return self.cash_and_equivalents + self.short_term_investments + self.long_term_investments

    @property
    def net_debt(self) -> float:
        """Total debt less cash and investments (negative = net cash)."""
        return self.total_debt - self.cash_and_investments

    @property
    def net_working_capital(self) -> float:
        """Operating NWC: current assets ex. cash, securities and deferred tax assets,
        less current liabilities ex. funded debt."""
        operating_assets = (
            self.total_current_assets
            - self.cash_and_equivalents
            - self.short_term_investments
            - self.deferred_tax_assets_current
        )
        operating_liabilities = (
            self.total_current_liabilities
            - self.commercial_paper
            - self.current_portion_long_term_debt
        )
        return operating_assets - operating_liabilities


# --------------------------------------------------------------------------------------
# Cash flow statement
# --------------------------------------------------------------------------------------
class CashFlowStatement(StrictModel):
    """One fiscal year of the consolidated statement of cash flows.

    All items are stored **as reported**: inflows positive, outflows negative.
    """

    fiscal_year: FiscalYear
    beginning_cash: NonNegMoney

    # Operating activities
    net_income: Money
    depreciation_and_amortization: NonNegMoney
    share_based_compensation: Money = 0.0
    deferred_income_taxes: Money = 0.0
    other_non_cash_items: Money = 0.0
    change_accounts_receivable: Money = 0.0
    change_inventories: Money = 0.0
    change_vendor_non_trade_receivables: Money = 0.0
    change_other_assets: Money = 0.0
    change_accounts_payable: Money = 0.0
    change_deferred_revenue: Money = 0.0
    change_other_liabilities: Money = 0.0
    cash_from_operations: Money

    # Investing activities
    purchases_of_investments: NonPosMoney = 0.0
    maturities_of_investments: NonNegMoney = 0.0
    sales_of_investments: NonNegMoney = 0.0
    acquisitions: NonPosMoney = 0.0
    capital_expenditures: NonPosMoney
    purchases_of_intangibles: NonPosMoney = 0.0
    other_investing: Money = 0.0
    cash_from_investing: Money

    # Financing activities
    proceeds_from_stock_issuance: Money = 0.0
    excess_tax_benefits: Money = 0.0
    taxes_on_net_share_settlement: Money = 0.0
    dividends_paid: NonPosMoney = 0.0
    share_repurchases: NonPosMoney = 0.0
    proceeds_from_debt_net: Money = 0.0
    repayment_of_debt: NonPosMoney = 0.0
    change_in_commercial_paper: Money = 0.0
    other_financing: Money = 0.0
    cash_from_financing: Money

    net_change_in_cash: Money
    ending_cash: NonNegMoney

    # Supplemental
    cash_taxes_paid: NonNegMoney | None = None
    cash_interest_paid: NonNegMoney | None = None

    _WORKING_CAPITAL: ClassVar[tuple[str, ...]] = (
        "change_accounts_receivable", "change_inventories",
        "change_vendor_non_trade_receivables", "change_other_assets",
        "change_accounts_payable", "change_deferred_revenue", "change_other_liabilities",
    )
    _OPERATING: ClassVar[tuple[str, ...]] = (
        "net_income", "depreciation_and_amortization", "share_based_compensation",
        "deferred_income_taxes", "other_non_cash_items", *_WORKING_CAPITAL,
    )
    _INVESTING: ClassVar[tuple[str, ...]] = (
        "purchases_of_investments", "maturities_of_investments", "sales_of_investments",
        "acquisitions", "capital_expenditures", "purchases_of_intangibles",
        "other_investing",
    )
    _FINANCING: ClassVar[tuple[str, ...]] = (
        "proceeds_from_stock_issuance", "excess_tax_benefits",
        "taxes_on_net_share_settlement", "dividends_paid", "share_repurchases",
        "proceeds_from_debt_net", "repayment_of_debt", "change_in_commercial_paper",
        "other_financing",
    )

    @model_validator(mode="after")
    def _check_identities(self, info: ValidationInfo) -> CashFlowStatement:
        """Verify CFO/CFI/CFF build-ups and the cash roll-forward."""
        enforce_identities(
            f"Cash Flow Statement FY{self.fiscal_year}",
            [
                ("CFO = sum of operating items",
                 self.cash_from_operations, self._sum(self._OPERATING)),
                ("CFI = sum of investing items",
                 self.cash_from_investing, self._sum(self._INVESTING)),
                ("CFF = sum of financing items",
                 self.cash_from_financing, self._sum(self._FINANCING)),
                ("Net change in cash = CFO + CFI + CFF",
                 self.net_change_in_cash,
                 self.cash_from_operations + self.cash_from_investing + self.cash_from_financing),
                ("Ending cash = Beginning cash + Net change",
                 self.ending_cash, self.beginning_cash + self.net_change_in_cash),
            ],
            _tolerance(info),
        )
        return self

    @property
    def capex(self) -> float:
        """Capital expenditure as a positive outflow."""
        return -self.capital_expenditures

    @property
    def working_capital_cash_flow(self) -> float:
        """Cash impact of working capital movements (positive = cash released)."""
        return self._sum(self._WORKING_CAPITAL)

    @property
    def change_in_nwc(self) -> float:
        """Increase in NWC (positive = investment), per the UFCF sign convention."""
        return -self.working_capital_cash_flow


# --------------------------------------------------------------------------------------
# Statement of shareholders' equity
# --------------------------------------------------------------------------------------
class EquityRollForward(StrictModel):
    """Retained earnings and total equity roll-forward for one fiscal year.

    Movements are stored as reported in the statement of shareholders' equity
    (reductions negative). This is the schedule that proves net income actually
    flows into retained earnings on the balance sheet.
    """

    fiscal_year: FiscalYear
    beginning_retained_earnings: Money
    net_income: Money
    dividends_declared: NonPosMoney = 0.0
    share_repurchases: NonPosMoney = 0.0
    other_retained_earnings_changes: Money = 0.0
    ending_retained_earnings: Money
    beginning_total_equity: Money
    total_equity_movements: Money
    ending_total_equity: Money

    @model_validator(mode="after")
    def _check_identities(self, info: ValidationInfo) -> EquityRollForward:
        """Verify the retained earnings and total equity roll-forwards."""
        enforce_identities(
            f"Equity Roll-Forward FY{self.fiscal_year}",
            [
                ("Ending RE = Beginning RE + NI + Dividends + Buybacks + Other",
                 self.ending_retained_earnings,
                 self.beginning_retained_earnings + self.net_income + self.dividends_declared
                 + self.share_repurchases + self.other_retained_earnings_changes),
                ("Ending equity = Beginning equity + Movements",
                 self.ending_total_equity,
                 self.beginning_total_equity + self.total_equity_movements),
            ],
            _tolerance(info),
        )
        return self


# --------------------------------------------------------------------------------------
# Revenue segments
# --------------------------------------------------------------------------------------
class RevenueSegment(StrictModel):
    """Net sales for one reporting segment or product line across fiscal years."""

    name: str = Field(min_length=1)
    segment_type: Literal["geographic", "product"]
    net_sales: dict[FiscalYear, NonNegMoney] = Field(min_length=1)


# --------------------------------------------------------------------------------------
# Debt
# --------------------------------------------------------------------------------------
class InstrumentType(str, Enum):
    """Debt instrument classification."""

    FIXED_RATE_NOTE = "fixed_rate_note"
    FLOATING_RATE_NOTE = "floating_rate_note"
    COMMERCIAL_PAPER = "commercial_paper"
    TERM_LOAN = "term_loan"
    REVOLVER = "revolver"
    SUBORDINATED_NOTE = "subordinated_note"


class Seniority(str, Enum):
    """Claim priority in the capital structure (used by the LBO waterfall)."""

    SENIOR_SECURED = "senior_secured"
    SENIOR_UNSECURED = "senior_unsecured"
    SUBORDINATED = "subordinated"


class DebtTranche(StrictModel):
    """A single debt instrument or a group of notes disclosed on one line.

    10-K debt tables often aggregate several notes into one row with a *range* of
    coupons and maturities (e.g. "Fixed-rate 0.45% - 3.85% notes, 2016 - 2043"), so
    rates and maturities are stored as ``[low, high]`` bounds. Single instruments
    simply have ``low == high``.
    """

    name: str = Field(min_length=1)
    instrument_type: InstrumentType
    seniority: Seniority = Seniority.SENIOR_UNSECURED
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    issuance: str | None = Field(default=None, description="Issuance programme / date")
    principal: NonNegMoney = Field(description="Face value in USD millions")
    prior_year_principal: NonNegMoney | None = None
    coupon_rate_low: Rate | None = None
    coupon_rate_high: Rate | None = None
    effective_rate_low: Rate
    effective_rate_high: Rate
    maturity_year_start: FiscalYear
    maturity_year_end: FiscalYear
    annual_amortization_pct: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Scheduled amortization (0 = bullet)"
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> DebtTranche:
        """Ensure every range is ordered and fixed-rate notes carry a coupon."""
        if self.maturity_year_start > self.maturity_year_end:
            raise ValueError(f"{self.name}: maturity range is inverted")
        if self.effective_rate_low > self.effective_rate_high:
            raise ValueError(f"{self.name}: effective rate range is inverted")
        if (self.coupon_rate_low is None) != (self.coupon_rate_high is None):
            raise ValueError(f"{self.name}: coupon range must have both bounds or neither")
        if self.coupon_rate_low is not None and self.coupon_rate_low > self.coupon_rate_high:
            raise ValueError(f"{self.name}: coupon range is inverted")
        if self.instrument_type is InstrumentType.FIXED_RATE_NOTE and self.coupon_rate_low is None:
            raise ValueError(f"{self.name}: fixed-rate note requires a coupon")
        return self

    @property
    def effective_rate(self) -> float:
        """Midpoint of the effective interest rate range."""
        return (self.effective_rate_low + self.effective_rate_high) / 2

    @property
    def is_bullet(self) -> bool:
        """True if principal is repaid in full at maturity."""
        return self.annual_amortization_pct == 0.0


class DebtSchedule(StrictModel):
    """Funded debt at a balance sheet date, reconciled to the balance sheet."""

    as_of_fiscal_year: FiscalYear
    tranches: list[DebtTranche]
    reported_total_term_debt: NonNegMoney = Field(description="Face value per the note")
    prior_reported_total_term_debt: NonNegMoney | None = None
    unamortized_discount: NonPosMoney = 0.0
    hedge_accounting_adjustments: Money = 0.0
    current_portion_long_term_debt: NonNegMoney = 0.0
    long_term_debt: NonNegMoney
    commercial_paper: NonNegMoney = 0.0
    commercial_paper_rate: Rate | None = None

    @model_validator(mode="after")
    def _check_identities(self, info: ValidationInfo) -> DebtSchedule:
        """Verify tranches sum to the reported total and face value ties to carrying value."""
        tol = _tolerance(info)
        checks = [
            ("Sum of tranche principal = Reported total term debt",
             self.total_principal, self.reported_total_term_debt),
            ("Face + Discount + Hedge adj. = Current + Long-term debt",
             self.reported_total_term_debt + self.unamortized_discount
             + self.hedge_accounting_adjustments,
             self.current_portion_long_term_debt + self.long_term_debt),
        ]
        if self.prior_reported_total_term_debt is not None:
            checks.append(
                ("Sum of prior-year principal = Prior reported total",
                 sum(t.prior_year_principal or 0.0 for t in self.tranches),
                 self.prior_reported_total_term_debt)
            )
        enforce_identities(f"Debt Schedule FY{self.as_of_fiscal_year}", checks, tol)
        return self

    @property
    def total_principal(self) -> float:
        """Sum of tranche face values."""
        return sum(t.principal for t in self.tranches)

    @property
    def carrying_value(self) -> float:
        """Balance sheet carrying value of term debt (current + long-term)."""
        return self.current_portion_long_term_debt + self.long_term_debt

    @property
    def weighted_average_effective_rate(self) -> float:
        """Principal-weighted average effective rate of term debt (range midpoints)."""
        return metrics.safe_divide(
            sum(t.principal * t.effective_rate for t in self.tranches), self.total_principal
        )

    def principal_by_currency(self) -> dict[str, float]:
        """Total face value grouped by currency of issuance."""
        totals: dict[str, float] = defaultdict(float)
        for t in self.tranches:
            totals[t.currency] += t.principal
        return dict(totals)

    def maturity_ladder(self) -> dict[int, float]:
        """Face value maturing per year.

        Tranches disclosed with a maturity *range* are spread evenly across the years
        in that range. This is an explicit simplifying assumption: the filing does not
        disclose the split within an aggregated line.
        """
        ladder: dict[int, float] = defaultdict(float)
        for t in self.tranches:
            years = range(t.maturity_year_start, t.maturity_year_end + 1)
            for year in years:
                ladder[year] += t.principal / len(years)
        return dict(sorted(ladder.items()))


# --------------------------------------------------------------------------------------
# Market / macro assumptions
# --------------------------------------------------------------------------------------
class MacroAssumptions(StrictModel):
    """Market inputs for valuation that are *not* found in the filing.

    Each input should have a matching entry in ``sources`` so the model is auditable.
    """

    valuation_date: date
    risk_free_rate: float = Field(ge=0.0, le=0.20, description="10Y government yield")
    equity_risk_premium: float = Field(ge=0.0, le=0.20)
    levered_beta: float = Field(gt=0.0, le=5.0)
    pre_tax_cost_of_debt: float | None = Field(default=None, ge=0.0, le=0.30)
    marginal_tax_rate: float = Field(ge=0.0, le=0.60)
    terminal_growth_rate: float = Field(ge=-0.02, le=0.06)
    exit_ev_ebitda_multiple: float = Field(gt=0.0, le=40.0)
    forecast_years: int = Field(default=5, ge=3, le=10)
    share_price: float | None = Field(default=None, gt=0.0)
    sources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_terminal_growth(self) -> MacroAssumptions:
        """Perpetual growth must be below the cost of equity, or Gordon Growth explodes."""
        if self.terminal_growth_rate >= self.cost_of_equity:
            raise ValueError(
                f"terminal_growth_rate ({self.terminal_growth_rate:.2%}) must be below "
                f"the cost of equity ({self.cost_of_equity:.2%})"
            )
        return self

    @property
    def cost_of_equity(self) -> float:
        """CAPM cost of equity = Rf + Beta x ERP."""
        return self.risk_free_rate + self.levered_beta * self.equity_risk_premium

    @classmethod
    def from_json(cls, path: str | Path) -> MacroAssumptions:
        """Load and validate assumptions from a JSON file.

        Args:
            path: Path to the JSON file.

        Returns:
            Validated assumptions.
        """
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Top-level container
# --------------------------------------------------------------------------------------
class FinancialStatementSchema(StrictModel):
    """Complete, cross-validated historical financial dataset for one company.

    Beyond each statement's own identities, this model checks the links *between*
    statements that make a 3-statement model coherent:

    - Net income on the income statement equals net income on the cash flow statement.
    - Ending cash on the cash flow statement equals cash on the balance sheet, and
      beginning cash equals the prior year's balance sheet cash.
    - The equity roll-forward picks up income-statement net income and ties to
      retained earnings and total equity on the balance sheet.
    - Revenue segments (geographic and product, separately) sum to net sales.
    - The debt schedule ties to commercial paper, current and long-term debt.
    - Reported effective tax rates match tax expense / pre-tax income.
    """

    metadata: CompanyMetadata
    income_statements: list[IncomeStatement] = Field(min_length=1)
    balance_sheets: list[BalanceSheet] = Field(min_length=1)
    cash_flow_statements: list[CashFlowStatement] = Field(min_length=1)
    equity_roll_forwards: list[EquityRollForward] = Field(default_factory=list)
    revenue_segments: list[RevenueSegment] = Field(default_factory=list)
    debt_schedule: DebtSchedule | None = None
    reported_effective_tax_rates: dict[FiscalYear, Rate] = Field(default_factory=dict)
    statutory_tax_rate: float | None = Field(default=None, ge=0.0, le=0.60)

    @model_validator(mode="after")
    def _check_structure(self) -> FinancialStatementSchema:
        """Each statement list must hold unique fiscal years."""
        for label, items in (
            ("income_statements", self.income_statements),
            ("balance_sheets", self.balance_sheets),
            ("cash_flow_statements", self.cash_flow_statements),
            ("equity_roll_forwards", self.equity_roll_forwards),
        ):
            years = [s.fiscal_year for s in items]
            if len(years) != len(set(years)):
                raise ValueError(f"{label} contains duplicate fiscal years: {years}")
        return self

    @model_validator(mode="after")
    def _check_cross_statement_links(self, info: ValidationInfo) -> FinancialStatementSchema:
        """Verify all inter-statement links (see class docstring)."""
        tol = _tolerance(info)
        inc = {s.fiscal_year: s for s in self.income_statements}
        bal = {s.fiscal_year: s for s in self.balance_sheets}
        cfs = {s.fiscal_year: s for s in self.cash_flow_statements}
        checks: list[tuple[str, float, float]] = []

        for y, cf in cfs.items():
            if y in inc:
                checks.append((f"FY{y} Net income: IS = CFS", inc[y].net_income, cf.net_income))
            if y in bal:
                checks.append((f"FY{y} Cash: CFS ending = BS",
                               cf.ending_cash, bal[y].cash_and_equivalents))
            if y - 1 in bal:
                checks.append((f"FY{y} Cash: CFS beginning = prior-year BS",
                               cf.beginning_cash, bal[y - 1].cash_and_equivalents))

        for rf in self.equity_roll_forwards:
            y = rf.fiscal_year
            if y in inc:
                checks.append((f"FY{y} Net income: IS = Equity roll-forward",
                               inc[y].net_income, rf.net_income))
            if y in bal:
                checks.append((f"FY{y} Retained earnings: roll-forward = BS",
                               rf.ending_retained_earnings, bal[y].retained_earnings))
                checks.append((f"FY{y} Total equity: roll-forward = BS",
                               rf.ending_total_equity, bal[y].total_equity))
            if y - 1 in bal:
                checks.append((f"FY{y} Opening RE = prior-year BS",
                               rf.beginning_retained_earnings, bal[y - 1].retained_earnings))

        for seg_type in ("geographic", "product"):
            segments = [s for s in self.revenue_segments if s.segment_type == seg_type]
            if not segments:
                continue
            for y, stmt in inc.items():
                if all(y in s.net_sales for s in segments):
                    checks.append((f"FY{y} {seg_type.title()} segments sum = Net sales",
                                   sum(s.net_sales[y] for s in segments), stmt.net_sales))

        if self.debt_schedule is not None and self.debt_schedule.as_of_fiscal_year in bal:
            ds, bs = self.debt_schedule, bal[self.debt_schedule.as_of_fiscal_year]
            y = ds.as_of_fiscal_year
            checks += [
                (f"FY{y} Current debt: schedule = BS",
                 ds.current_portion_long_term_debt, bs.current_portion_long_term_debt),
                (f"FY{y} Long-term debt: schedule = BS", ds.long_term_debt, bs.long_term_debt),
                (f"FY{y} Commercial paper: schedule = BS", ds.commercial_paper, bs.commercial_paper),
            ]

        enforce_identities("Cross-statement linkage", checks, tol)

        enforce_identities(
            "Reported vs. computed effective tax rate",
            [(f"FY{y} ETR", rate, inc[y].effective_tax_rate)
             for y, rate in self.reported_effective_tax_rates.items() if y in inc],
            RATE_TOLERANCE,
        )
        return self

    # ---------------------------------------------------------------- accessors
    @property
    def years(self) -> list[int]:
        """Fiscal years covered by the income statement, ascending."""
        return sorted(s.fiscal_year for s in self.income_statements)

    @property
    def latest_year(self) -> int:
        """Most recent fiscal year with a balance sheet."""
        return max(s.fiscal_year for s in self.balance_sheets)

    def income_statement(self, year: int) -> IncomeStatement:
        """Return the income statement for ``year`` (``KeyError`` if absent)."""
        return {s.fiscal_year: s for s in self.income_statements}[year]

    def balance_sheet(self, year: int) -> BalanceSheet:
        """Return the balance sheet for ``year`` (``KeyError`` if absent)."""
        return {s.fiscal_year: s for s in self.balance_sheets}[year]

    def cash_flow_statement(self, year: int) -> CashFlowStatement:
        """Return the cash flow statement for ``year`` (``KeyError`` if absent)."""
        return {s.fiscal_year: s for s in self.cash_flow_statements}[year]

    def ebitda(self, year: int) -> float:
        """EBITDA = income-statement EBIT + cash-flow-statement D&A."""
        return metrics.ebitda(
            self.income_statement(year).ebit,
            self.cash_flow_statement(year).depreciation_and_amortization,
        )

    def unlevered_fcf(self, year: int, tax_rate: float | None = None) -> float:
        """Historical unlevered FCF for ``year``.

        Args:
            year: Fiscal year.
            tax_rate: Tax rate to apply to EBIT; defaults to that year's effective rate.

        Returns:
            EBIT x (1 - t) + D&A - Capex - Change in NWC.
        """
        inc, cf = self.income_statement(year), self.cash_flow_statement(year)
        return metrics.unlevered_free_cash_flow(
            ebit=inc.ebit,
            tax_rate=inc.effective_tax_rate if tax_rate is None else tax_rate,
            depreciation_and_amortization=cf.depreciation_and_amortization,
            capex=cf.capex,
            change_in_nwc=cf.change_in_nwc,
        )

    def historical_metrics(self) -> pd.DataFrame:
        """Key operating metrics by fiscal year, the starting point for projections.

        Returns:
            DataFrame indexed by metric name with one column per fiscal year.
        """
        rows: dict[int, dict[str, float]] = {}
        prev_sales: float | None = None
        for y in self.years:
            inc, cf = self.income_statement(y), self.cash_flow_statement(y)
            sales = inc.net_sales
            rows[y] = {
                "Net sales": sales,
                "Revenue growth": (sales / prev_sales - 1) if prev_sales else float("nan"),
                "Gross margin %": inc.gross_margin_pct,
                "EBIT": inc.ebit,
                "EBIT margin %": inc.operating_margin,
                "D&A": cf.depreciation_and_amortization,
                "EBITDA": self.ebitda(y),
                "EBITDA margin %": metrics.safe_divide(self.ebitda(y), sales),
                "Capex": cf.capex,
                "Capex % sales": metrics.safe_divide(cf.capex, sales),
                "D&A % sales": metrics.safe_divide(cf.depreciation_and_amortization, sales),
                "Change in NWC": cf.change_in_nwc,
                "Effective tax rate": inc.effective_tax_rate,
                "Unlevered FCF": self.unlevered_fcf(y),
            }
            prev_sales = sales
        return pd.DataFrame(rows)
