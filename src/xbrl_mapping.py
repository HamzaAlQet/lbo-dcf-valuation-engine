"""Map a 10-K's US-GAAP XBRL facts into the validated statement schemas.

For each schema line there is a priority list of US-GAAP concepts (companies tag the
same line differently: ``Revenues`` vs ``RevenueFromContractWithCustomer...``). The
first concept reported for the period wins.

Filings report totals that are not always broken down the way the schema is. Where
a total is reported but its components are not, the remainder goes to a clearly
labelled "other" line, so every subtotal ties. Every such derivation, and every line
built from fallbacks (for example EBIT derived from pre-tax income and net interest),
is recorded in ``FinancialStatementSchema.derivation_notes`` and shown in the
workbook. The checks between independently reported figures remain hard:

- **Total assets** (``Assets``) = **total liabilities and equity**
  (``LiabilitiesAndStockholdersEquity``);
- **beginning cash + reported change in cash = ending cash** on the cash flow
  statement;
- **diluted EPS** = net income / diluted shares.

Operating companies only: filings with an unclassified balance sheet (banks,
insurers, captive-finance groups) raise :class:`~src.xbrl.OutOfScopeError`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from src.exceptions import ExtractionError
from src.schemas import FinancialStatementSchema
from src.xbrl import FilingFacts, OutOfScopeError, load_company_facts, load_inline_xbrl, previous_day

logger = logging.getLogger(__name__)

MILLION = 1_000_000.0

# --------------------------------------------------------------------------------------
# Concept priority lists
# --------------------------------------------------------------------------------------
REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
           "SalesRevenueGoodsNet")
COST_OF_REVENUE = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
                   "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization")
GROSS_PROFIT = ("GrossProfit",)
RND = ("ResearchAndDevelopmentExpense",
       "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")
SGA = ("SellingGeneralAndAdministrativeExpense",)
SGA_PARTS = ("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense")
OPERATING_INCOME = ("OperatingIncomeLoss",)
PRETAX = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
          "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments")
PRETAX_PARTS = ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign")
TAX = ("IncomeTaxExpenseBenefit",)
NET_INCOME = ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic")
PROFIT_LOSS = ("ProfitLoss",)
NCI_INCOME = ("NetIncomeLossAttributableToNoncontrollingInterest",)
INTEREST_EXPENSE = ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                    "InterestExpenseBorrowings")
INTEREST_INCOME = ("InvestmentIncomeInterest", "InterestIncomeOther",
                   "InvestmentIncomeInterestAndDividend")
NET_INTEREST = ("InterestIncomeExpenseNonoperatingNet",)
CASH_INTEREST = ("InterestPaidNet", "InterestPaid")
DILUTED_SHARES = ("WeightedAverageNumberOfDilutedSharesOutstanding",)
BASIC_SHARES = ("WeightedAverageNumberOfSharesOutstandingBasic",)
EPS_DILUTED = ("EarningsPerShareDiluted",)
EPS_BASIC = ("EarningsPerShareBasic",)
DPS = ("CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid")

ASSETS = ("Assets",)
CURRENT_ASSETS = ("AssetsCurrent",)
CURRENT_LIABILITIES = ("LiabilitiesCurrent",)
LIABILITIES = ("Liabilities",)
LIABILITIES_AND_EQUITY = ("LiabilitiesAndStockholdersEquity",)
EQUITY_PARENT = ("StockholdersEquity",)
EQUITY_TOTAL = ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",)
MINORITY = ("MinorityInterest",)
CASH = ("CashAndCashEquivalentsAtCarryingValue", "Cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
ST_INVESTMENTS = ("ShortTermInvestments", "MarketableSecuritiesCurrent",
                  "AvailableForSaleSecuritiesDebtSecuritiesCurrent")
RECEIVABLES = ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent",
               "AccountsNotesAndLoansReceivableNetCurrent")
INVENTORY = ("InventoryNet",)
LT_INVESTMENTS = ("LongTermInvestments", "MarketableSecuritiesNoncurrent",
                  "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent")
PPE = ("PropertyPlantAndEquipmentNet",
       "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization")
GOODWILL = ("Goodwill",)
INTANGIBLES = ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet")
PAYABLES = ("AccountsPayableCurrent", "AccountsPayableTradeCurrent",
            "AccountsPayableAndAccruedLiabilitiesCurrent")
DEFERRED_REVENUE = ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent")
DEFERRED_REVENUE_NC = ("ContractWithCustomerLiabilityNoncurrent", "DeferredRevenueNoncurrent")
COMMERCIAL_PAPER = ("CommercialPaper",)
SHORT_TERM_DEBT = ("ShortTermBorrowings", "OtherShortTermBorrowings",
                   "ShortTermBankLoansAndNotesPayable")
CURRENT_LTD = ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
               "NotesPayableCurrent")
DEBT_CURRENT_TOTAL = ("DebtCurrent",)
LTD_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                  "LongTermNotesPayable", "LongTermNotesAndLoans")
LTD_TOTAL = ("LongTermDebt",)
COMMON_AND_APIC = ("CommonStocksIncludingAdditionalPaidInCapital",)
COMMON_STOCK = ("CommonStockValue",)
APIC = ("AdditionalPaidInCapital", "AdditionalPaidInCapitalCommonStock")
RETAINED_EARNINGS = ("RetainedEarningsAccumulatedDeficit",)
AOCI = ("AccumulatedOtherComprehensiveIncomeLossNetOfTax",)

CFO = ("NetCashProvidedByUsedInOperatingActivities",)
CFI = ("NetCashProvidedByUsedInInvestingActivities",)
CFF = ("NetCashProvidedByUsedInFinancingActivities",)
CFS_CASH = ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations",
            "CashAndCashEquivalentsAtCarryingValue")
CASH_CHANGE = ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
               "CashAndCashEquivalentsPeriodIncreaseDecrease")
DA = ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
      "DepreciationAndAmortization", "DepreciationAmortizationAndOther")
DEPRECIATION = ("Depreciation",)
AMORTIZATION = ("AmortizationOfIntangibleAssets",)
SBC = ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense")
DEFERRED_TAX = ("DeferredIncomeTaxExpenseBenefit", "DeferredIncomeTaxesAndTaxCredits")
CHG_RECEIVABLES = ("IncreaseDecreaseInAccountsReceivable", "IncreaseDecreaseInReceivables")
CHG_INVENTORY = ("IncreaseDecreaseInInventories",)
CHG_PAYABLES = ("IncreaseDecreaseInAccountsPayable", "IncreaseDecreaseInAccountsPayableTrade",
                "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities")
CHG_DEFERRED_REVENUE = ("IncreaseDecreaseInContractWithCustomerLiability",
                        "IncreaseDecreaseInDeferredRevenue")
CAPEX = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
ACQUISITIONS = ("PaymentsToAcquireBusinessesNetOfCashAcquired",)
DIVIDENDS = ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock")
BUYBACKS = ("PaymentsForRepurchaseOfCommonStock",)
DEBT_ISSUED = ("ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfSeniorLongTermDebt",
               "ProceedsFromIssuanceOfDebt", "ProceedsFromDebtNetOfIssuanceCosts")
DEBT_REPAID = ("RepaymentsOfLongTermDebt", "RepaymentsOfDebt", "RepaymentsOfSeniorDebt")
CP_NET = ("ProceedsFromRepaymentsOfCommercialPaper",)
SHARES_OUTSTANDING = ("EntityCommonStockSharesOutstanding",)

BANK_MARKERS = ("Deposits", "InterestAndDividendIncomeOperating",
                "LoansAndLeasesReceivableNetReportedAmount")


class _Mapper:
    """Builds statement dicts from one filing's facts, recording derivations."""

    def __init__(self, ff: FilingFacts) -> None:
        self.ff = ff
        self.notes: list[str] = []

    # ---------------------------------------------------------------- lookups
    def a(self, concepts: Sequence[str], end: date) -> float | None:
        """First annual value (USD millions) among ``concepts`` for the year ending ``end``."""
        for c in concepts:
            v = self.ff.annual(c, end)
            if v is not None:
                return v / MILLION
        return None

    def i(self, concepts: Sequence[str], at: date) -> float | None:
        """First instant value (USD millions) among ``concepts`` on ``at``."""
        for c in concepts:
            v = self.ff.instant(c, at)
            if v is not None:
                return v / MILLION
        return None

    def raw(self, concepts: Sequence[str], end: date) -> float | None:
        """First annual value in raw units (shares, per-share amounts)."""
        for c in concepts:
            v = self.ff.annual(c, end)
            if v is not None:
                return v
        return None

    def sum_a(self, concepts: Sequence[str], end: date) -> float | None:
        """Sum of the annual values that are reported among ``concepts``."""
        vals = [self.a((c,), end) for c in concepts]
        present = [v for v in vals if v is not None]
        return sum(present) if present else None

    def note(self, label: str, text: str) -> None:
        msg = f"{label}: {text}"
        if msg not in self.notes:
            self.notes.append(msg)

    # ---------------------------------------------------------------- income statement
    def income_statement(self, fy: int, end: date) -> dict[str, Any]:
        lbl = f"FY{fy}"
        revenue = self.a(REVENUE, end)
        if revenue is None:
            raise ExtractionError(f"{lbl}: no revenue concept reported")
        rd = self.a(RND, end) or 0.0
        sga = self.a(SGA, end)
        if sga is None:
            sga = self.sum_a(SGA_PARTS, end)
            if sga is not None:
                self.note(lbl, "SG&A = selling & marketing + general & administrative")
            else:
                sga = 0.0
                self.note(lbl, "no SG&A reported; operating costs are in other operating expenses")

        pretax = self.a(PRETAX, end)
        if pretax is None:
            pretax = self.sum_a(PRETAX_PARTS, end)
            if pretax is not None:
                self.note(lbl, "pre-tax income = domestic + foreign pre-tax income")
        tax = self.a(TAX, end)
        if tax is None:
            tax = 0.0
            self.note(lbl, "income tax expense not reported; assumed zero")
        net_income = self.a(NET_INCOME, end)
        if net_income is None:
            profit, nci = self.a(PROFIT_LOSS, end), self.a(NCI_INCOME, end) or 0.0
            if profit is None:
                raise ExtractionError(f"{lbl}: net income not reported")
            net_income = profit - nci
            self.note(lbl, "net income = profit (loss) less noncontrolling interests' share")
        if pretax is None:
            pretax = net_income + tax
            self.note(lbl, "pre-tax income = net income + income tax expense")

        ebit = self.a(OPERATING_INCOME, end)
        if ebit is None:
            net_interest = self.a(NET_INTEREST, end)
            if net_interest is not None:
                ebit = pretax - net_interest
                self.note(lbl, "EBIT derived as pre-tax income less net interest income")
            else:
                expense = self.a(INTEREST_EXPENSE, end) or 0.0
                income = self.a(INTEREST_INCOME, end) or 0.0
                ebit = pretax + expense - income
                self.note(lbl, "EBIT derived as pre-tax income + interest expense - interest income")

        cogs = self.a(COST_OF_REVENUE, end)
        if cogs is None:
            gross = self.a(GROSS_PROFIT, end)
            if gross is not None:
                cogs = revenue - gross
                self.note(lbl, "cost of revenue = revenue - reported gross profit")
            else:
                cogs = max(revenue - ebit - rd - sga, 0.0)
                self.note(lbl, "no cost of revenue reported; derived as revenue - EBIT - R&D - SG&A "
                               "(gross margin is therefore an all-in operating cost ratio)")
        gross_margin = revenue - cogs
        other_opex = gross_margin - ebit - rd - sga
        diluted = self.raw(DILUTED_SHARES, end)
        if not diluted:
            raise ExtractionError(f"{lbl}: diluted weighted-average shares not reported")
        basic = self.raw(BASIC_SHARES, end)
        eps = self.raw(EPS_DILUTED, end)
        scale = _share_scale(net_income, diluted, eps)
        if scale != 1.0:
            diluted *= scale
            basic = basic * scale if basic else basic
            self.note(lbl, f"share counts tagged in units of {scale:,.0f}; rescaled")
        if eps and abs(eps - net_income * MILLION / diluted) > max(0.011, 0.02 * abs(eps)):
            self.note(lbl, f"reported diluted EPS ({eps:,.2f}) differs from net income / diluted "
                           f"shares ({net_income * MILLION / diluted:,.2f}), e.g. preferred "
                           "dividends or two-class method; EPS not cross-checked")
            eps = None
        return {
            "fiscal_year": fy, "net_sales": revenue, "cost_of_sales": cogs,
            "gross_margin": gross_margin, "research_and_development": rd,
            "selling_general_administrative": sga, "other_operating_expenses": other_opex,
            "total_operating_expenses": rd + sga + other_opex, "operating_income": ebit,
            "other_income_expense": pretax - ebit, "pretax_income": pretax,
            "income_tax_expense": tax, "other_net_income_items": net_income - (pretax - tax),
            "net_income": net_income, "diluted_shares": diluted / MILLION,
            "basic_shares": basic / MILLION if basic else None,
            "eps_diluted": eps, "eps_basic": self.raw(EPS_BASIC, end),
            "dividends_per_share": self.raw(DPS, end),
        }

    # ---------------------------------------------------------------- balance sheet
    def balance_sheet(self, fy: int, at: date) -> dict[str, Any]:
        lbl = f"FY{fy} balance sheet"
        total_assets = self.i(ASSETS, at)
        current_assets = self.i(CURRENT_ASSETS, at)
        current_liabilities = self.i(CURRENT_LIABILITIES, at)
        l_and_e = self.i(LIABILITIES_AND_EQUITY, at)
        if total_assets is None or l_and_e is None:
            raise ExtractionError(f"{lbl}: total assets or total liabilities & equity missing")
        if current_assets is None or current_liabilities is None:
            raise OutOfScopeError(
                "The balance sheet is unclassified (no current assets / liabilities), as at "
                "banks, insurers and groups with large finance arms. The operating-company "
                "model needs a classified balance sheet.")

        nci = self.i(MINORITY, at) or 0.0
        parent = self.i(EQUITY_PARENT, at)
        total_equity_reported = self.i(EQUITY_TOTAL, at)
        if parent is None:
            if total_equity_reported is None:
                raise ExtractionError(f"{lbl}: shareholders' equity not reported")
            parent = total_equity_reported - nci
        total_equity = total_equity_reported if total_equity_reported is not None else parent + nci
        liabilities = self.i(LIABILITIES, at)
        if liabilities is None:
            liabilities = l_and_e - total_equity
            self.note(lbl, "total liabilities = liabilities & equity - total equity "
                           "(includes any temporary/mezzanine equity)")
        else:
            mezzanine = l_and_e - liabilities - total_equity
            if abs(mezzanine) > 1.0:
                # Redeemable noncontrolling interests / temporary equity sit between
                # liabilities and equity; carry them with other non-current liabilities.
                liabilities += mezzanine
                self.note(lbl, f"temporary (mezzanine) equity of {mezzanine:,.0f}m included in "
                               "other non-current liabilities")

        cash = self.i(CASH, at)
        if cash is None:
            raise ExtractionError(f"{lbl}: cash and cash equivalents not reported")
        sti = self.i(ST_INVESTMENTS, at) or 0.0
        ar = self.i(RECEIVABLES, at) or 0.0
        inventory = self.i(INVENTORY, at) or 0.0
        lti = self.i(LT_INVESTMENTS, at) or 0.0
        ppe = self.i(PPE, at) or 0.0
        goodwill = self.i(GOODWILL, at) or 0.0
        intangibles = self.i(INTANGIBLES, at) or 0.0
        ap = self.i(PAYABLES, at) or 0.0
        def_rev = self.i(DEFERRED_REVENUE, at) or 0.0
        def_rev_nc = self.i(DEFERRED_REVENUE_NC, at) or 0.0
        cp = self.i(COMMERCIAL_PAPER, at) or 0.0
        std = self.i(SHORT_TERM_DEBT, at) or 0.0
        cur_ltd = self.i(CURRENT_LTD, at)
        if cur_ltd is None:
            debt_current = self.i(DEBT_CURRENT_TOTAL, at)
            cur_ltd = max((debt_current or 0.0) - cp - std, 0.0)
            if debt_current is not None:
                self.note(lbl, "current portion of debt = total current debt - commercial paper "
                               "- short-term borrowings")
        ltd = self.i(LTD_NONCURRENT, at)
        if ltd is None:
            ltd_total = self.i(LTD_TOTAL, at)
            ltd = max((ltd_total or 0.0) - cur_ltd, 0.0)
            if ltd_total is not None:
                self.note(lbl, "long-term debt = total long-term debt - current portion")

        common = self.i(COMMON_AND_APIC, at)
        if common is None:
            common = (self.i(COMMON_STOCK, at) or 0.0) + (self.i(APIC, at) or 0.0)
        retained = self.i(RETAINED_EARNINGS, at) or 0.0
        aoci = self.i(AOCI, at) or 0.0

        bs: dict[str, Any] = {
            "fiscal_year": fy, "cash_and_equivalents": cash, "short_term_investments": sti,
            "accounts_receivable": ar, "inventories": inventory,
            "other_current_assets": current_assets - cash - sti - ar - inventory,
            "total_current_assets": current_assets,
            "long_term_investments": lti, "ppe_net": ppe, "goodwill": goodwill,
            "intangible_assets": intangibles,
            "other_non_current_assets": total_assets - current_assets - lti - ppe - goodwill
            - intangibles,
            "total_assets": total_assets,
            "accounts_payable": ap, "deferred_revenue_current": def_rev,
            "commercial_paper": cp, "short_term_debt": std,
            "current_portion_long_term_debt": cur_ltd,
            "accrued_expenses": current_liabilities - ap - def_rev - cp - std - cur_ltd,
            "total_current_liabilities": current_liabilities,
            "deferred_revenue_non_current": def_rev_nc, "long_term_debt": ltd,
            "other_non_current_liabilities": liabilities - current_liabilities - def_rev_nc - ltd,
            "total_liabilities": liabilities,
            "common_stock_and_apic": common, "retained_earnings": retained,
            "accumulated_other_comprehensive_income": aoci,
            "other_equity": parent - common - retained - aoci,
            "noncontrolling_interest": nci, "total_equity": total_equity,
            "total_liabilities_and_equity": l_and_e,
        }
        for key in ("other_current_assets", "other_non_current_assets", "accrued_expenses",
                    "other_non_current_liabilities"):
            if bs[key] < -0.005 * total_assets:
                self.note(lbl, f"'{key}' remainder is negative ({bs[key]:,.0f}m): a component "
                               "tag may overlap another line")
        return bs

    # ---------------------------------------------------------------- cash flow statement
    def cash_flow(self, fy: int, start: date, end: date, net_income: float,
                  bs_cash: dict[date, float]) -> dict[str, Any]:
        lbl = f"FY{fy} cash flow"
        cfo, cfi, cff = self.a(CFO, end), self.a(CFI, end), self.a(CFF, end)
        if cfo is None or cfi is None or cff is None:
            raise ExtractionError(f"{lbl}: operating, investing or financing totals missing")
        prior = previous_day(start)
        begin, finish = self.i(CFS_CASH, prior), self.i(CFS_CASH, end)
        change = self.a(CASH_CHANGE, end)
        if finish is None:
            finish = bs_cash.get(end)
        if begin is None and finish is not None and change is not None:
            begin = finish - change
        if begin is None or finish is None:
            raise ExtractionError(f"{lbl}: beginning or ending cash not reported")
        if change is None:
            change = finish - begin

        da = self.a(DA, end)
        if da is None:
            dep, amort = self.a(DEPRECIATION, end), self.a(AMORTIZATION, end)
            if dep is not None or amort is not None:
                da = (dep or 0.0) + (amort or 0.0)
                self.note(lbl, "D&A = depreciation + amortization of intangibles")
            else:
                da = 0.0
                self.note(lbl, "D&A not reported; EBITDA equals EBIT")
        sbc = self.a(SBC, end) or 0.0
        deferred = self.a(DEFERRED_TAX, end) or 0.0
        chg_ar = -(self.a(CHG_RECEIVABLES, end) or 0.0)
        chg_inv = -(self.a(CHG_INVENTORY, end) or 0.0)
        chg_ap = self.a(CHG_PAYABLES, end) or 0.0
        chg_dr = self.a(CHG_DEFERRED_REVENUE, end) or 0.0
        operating_known = net_income + da + sbc + deferred + chg_ar + chg_inv + chg_ap + chg_dr

        capex = -abs(self.a(CAPEX, end) or 0.0)
        acquisitions = -abs(self.a(ACQUISITIONS, end) or 0.0)
        dividends = -abs(self.a(DIVIDENDS, end) or 0.0)
        buybacks = -abs(self.a(BUYBACKS, end) or 0.0)
        issued = self.a(DEBT_ISSUED, end) or 0.0
        repaid = -abs(self.a(DEBT_REPAID, end) or 0.0)
        cp_net = self.a(CP_NET, end) or 0.0

        restricted_begin = max(begin - bs_cash[prior], 0.0) if prior in bs_cash else 0.0
        restricted_end = max(finish - bs_cash[end], 0.0) if end in bs_cash else 0.0
        return {
            "fiscal_year": fy, "beginning_cash": begin, "net_income": net_income,
            "depreciation_and_amortization": da, "share_based_compensation": sbc,
            "deferred_income_taxes": deferred,
            "other_non_cash_items": cfo - operating_known,
            "change_accounts_receivable": chg_ar, "change_inventories": chg_inv,
            "change_accounts_payable": chg_ap, "change_deferred_revenue": chg_dr,
            "cash_from_operations": cfo,
            "acquisitions": acquisitions, "capital_expenditures": capex,
            "other_investing": cfi - capex - acquisitions, "cash_from_investing": cfi,
            "dividends_paid": dividends, "share_repurchases": buybacks,
            "proceeds_from_debt_net": issued, "repayment_of_debt": repaid,
            "change_in_commercial_paper": cp_net,
            "other_financing": cff - dividends - buybacks - issued - repaid - cp_net,
            "cash_from_financing": cff,
            "fx_and_other_cash_effects": change - cfo - cfi - cff,
            "net_change_in_cash": change, "ending_cash": finish,
            "restricted_cash_beginning": restricted_begin,
            "restricted_cash_ending": restricted_end,
        }


def _fiscal_label(ff: FilingFacts, end: date) -> int:
    """Label for the latest fiscal year: the filing's own fiscal-year focus when it is
    consistent with the period end, else the calendar year of the period end."""
    if ff.fiscal_year and abs(ff.fiscal_year - end.year) <= 1:
        return ff.fiscal_year
    return end.year


def build_schema(ff: FilingFacts, ticker: str | None = None) -> FinancialStatementSchema:
    """Map one filing's facts into a validated :class:`FinancialStatementSchema`.

    Args:
        ff: Facts from :func:`src.xbrl.load_inline_xbrl` or :func:`src.xbrl.load_company_facts`.
        ticker: Overrides the ticker recorded in the facts.

    Raises:
        OutOfScopeError: For filings the operating-company model cannot represent.
        ExtractionError: If required lines are missing.
        AccountingIdentityError: If independently reported totals do not tie.
    """
    if any(ff.facts.get(m) for m in BANK_MARKERS):
        raise OutOfScopeError("This looks like a bank (deposits / loans reported). The "
                              "operating-company model does not apply to financial institutions.")
    periods = ff.annual_periods(REVENUE)[:3]
    if not periods:
        raise OutOfScopeError("No annual revenue is reported in this filing")
    latest_label = _fiscal_label(ff, periods[0][1])
    labelled = [(latest_label - k, start, end) for k, (start, end) in enumerate(periods)]

    m = _Mapper(ff)
    income: list[dict[str, Any]] = []
    for fy, _start, end in labelled:
        try:
            income.append(m.income_statement(fy, end))
        except ExtractionError:
            if not income:
                raise
            logger.warning("FY%d income statement incomplete; using later years only", fy)
            break
    years = [row["fiscal_year"] for row in income]
    labelled = [p for p in labelled if p[0] in years]

    balance: list[dict[str, Any]] = []
    for fy, _start, end in labelled[:2]:
        try:
            balance.append(m.balance_sheet(fy, end))
        except OutOfScopeError:
            raise
        except ExtractionError:
            if not balance:
                raise
    bs_cash = {end: b["cash_and_equivalents"] for (fy, _s, end), b in
               zip(labelled, balance, strict=False)}

    cash_flows: list[dict[str, Any]] = []
    for (fy, start, end), inc in zip(labelled, income, strict=True):
        try:
            cash_flows.append(m.cash_flow(fy, start, end, inc["net_income"], bs_cash))
        except ExtractionError:
            if not cash_flows:
                raise
            break

    interest: dict[int, float] = {}
    for fy, _s, end in labelled:
        v = m.a(INTEREST_EXPENSE, end)
        if v is None:
            v = m.a(CASH_INTEREST, end)
            if v is not None:
                m.note(f"FY{fy}", "interest expense not tagged; cash interest paid used to "
                                  "imply the cost of debt")
        if v is not None and v >= 0:
            interest[fy] = v
    shares_out = _shares_outstanding(ff)
    payload = {
        "metadata": {
            "name": ff.entity_name, "ticker": (ticker or ff.ticker or None),
            "cik": ff.cik, "fiscal_year_end": labelled[0][2].isoformat(),
            "source_document": ff.source,
        },
        "income_statements": income, "balance_sheets": balance,
        "cash_flow_statements": cash_flows,
        "reported_effective_tax_rates": {},
        "shares_outstanding": shares_out,
        "reported_interest_expense": interest,
        "data_source": "xbrl",
        "derivation_notes": m.notes,
    }
    schema = FinancialStatementSchema.model_validate(payload)
    logger.info("Mapped %s FY%d-FY%d from XBRL (%d derivation notes)", ff.entity_name,
                min(years), max(years), len(m.notes))
    return schema


def _share_scale(net_income: float, diluted_raw: float, eps: float | None) -> float:
    """Detect share counts tagged in thousands or millions instead of units.

    Returns the factor (1, 1e3 or 1e6) that makes net income / shares match reported
    diluted EPS within 10%; 1 if EPS is not reported or no factor fits.
    """
    if not eps or not net_income:
        return 1.0
    for factor in (1.0, 1e3, 1e6):
        implied = net_income * MILLION / (diluted_raw * factor)
        if abs(implied / eps - 1) < 0.10:
            return factor
    return 1.0


def _shares_outstanding(ff: FilingFacts) -> float | None:
    """Shares outstanding from the cover page (millions), summing share classes."""
    facts = [f for c in SHARES_OUTSTANDING for f in ff.dei.get(c, [])
             if isinstance(f.value, float)]
    if not facts:
        return None
    latest = max(f.end for f in facts)
    total = sum(f.value for f in facts if f.end == latest)  # type: ignore[misc]
    return total / MILLION if total > 0 else None


def schema_from_ticker(ticker_or_cik: str, fiscal_year: int | None = None
                       ) -> FinancialStatementSchema:
    """Fetch a company's 10-K facts from SEC EDGAR and map them."""
    ff = load_company_facts(ticker_or_cik, fiscal_year)
    return build_schema(ff)


def schema_from_htm(path: str, ticker: str | None = None) -> FinancialStatementSchema:
    """Map an inline XBRL 10-K ``.htm`` file downloaded from EDGAR."""
    return build_schema(load_inline_xbrl(path), ticker=ticker)
