"""Tests for the 10-K extraction pipeline.

Three layers:
1. Unit tests of the row parser on hand-written lines.
2. End-to-end extraction of a small synthetic filing, including failure modes.
3. Golden-value tests against Apple's FY2015 10-K.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.exceptions import (
    AccountingIdentityError,
    AmbiguousLineItemError,
    MissingLineItemError,
)
from src.extractor import (
    F,
    TenKExtractor,
    map_fields,
    normalize_label,
    normalize_lines,
    parse_number,
    parse_statement,
    split_line,
)
from src.schemas import FinancialStatementSchema, InstrumentType


# --------------------------------------------------------------------------------------
# 1. Parser units
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("21,120", (21_120.0, False)),
        ("$21,120", (21_120.0, False)),
        ("(2,500)", (-2_500.0, False)),
        ("$(345)", (-345.0, False)),
        ("26.4%", (0.264, True)),
        ("(7)%", (-0.07, True)),
        ("-", (0.0, False)),
        ("9.28", (9.28, False)),
    ],
)
def test_parse_number_valid(token: str, expected: tuple[float, bool]) -> None:
    assert parse_number(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["respectively", "26,", "(1)(3)", "(123", "1,23"])
def test_parse_number_rejects_non_numeric(token: str) -> None:
    assert parse_number(token) is None


def test_split_line_keeps_numbers_inside_label() -> None:
    label, tokens = split_line(
        "Accounts receivable, less allowances of $82 and $86, respectively 16,849 17,460"
    )
    assert label == "Accounts receivable, less allowances of $82 and $86, respectively"
    assert tokens == ["16,849", "17,460"]


def test_normalize_label() -> None:
    assert normalize_label("Other income/(expense), net") == "other income expense net"
    assert normalize_label("Shareholders’ equity:") == "shareholders equity"


def test_normalize_lines_strips_page_furniture() -> None:
    raw = (
        "10/22/26, 11:27 PM Form 10-K\nTable of Contents\nNet sales   $ 1,000\n"
        "https://www.sec.gov/Archives/edgar/data/1/x.htm 4/83\nApple Inc. | 2015 Form 10-K | 39\n"
    )
    assert normalize_lines(raw) == ["Net sales $ 1,000"]


def test_parse_statement_headers_sections_and_wrapped_labels() -> None:
    lines = [
        "Years ended", "2024 2023",
        "Financing activities:",
        "Taxes paid related to net share",
        "settlement of equity awards (5) (4)",
        "Other 1 2",
    ]
    stmt = parse_statement("CFS", lines, section_pattern=r"financing activities")
    assert stmt.years == [2024, 2023]
    assert [r.norm for r in stmt.rows] == [
        "taxes paid related to net share settlement of equity awards", "other",
    ]
    assert all(r.section == "financing activities" for r in stmt.rows)
    assert stmt.rows[0].values == [-5, -4]


def test_map_fields_errors() -> None:
    stmt = parse_statement("IS", ["2024 2023", "Net sales 10 9", "Net sales 11 10"])
    with pytest.raises(AmbiguousLineItemError):
        map_fields(stmt, [F("net_sales", r"net sales")], 2)
    with pytest.raises(MissingLineItemError, match="cost_of_sales"):
        map_fields(stmt, [F("cost_of_sales", r"cost of sales")], 2)
    assert map_fields(stmt, [F("cogs", r"cost of sales", required=False)], 2) == {}


# --------------------------------------------------------------------------------------
# 2. Synthetic filing end-to-end
# --------------------------------------------------------------------------------------
def test_synthetic_filing_extracts_and_validates(synthetic_text: str) -> None:
    schema = TenKExtractor("synthetic", text=synthetic_text).extract()
    assert schema.metadata.name == "Acme Corp."
    assert schema.metadata.fiscal_year_end == date(2024, 12, 31)
    assert schema.years == [2023, 2024]
    assert schema.balance_sheet(2024).total_assets == 1_000
    assert schema.cash_flow_statement(2024).capex == 100
    assert schema.income_statement(2024).diluted_shares == pytest.approx(100.0)
    assert schema.debt_schedule is None and schema.revenue_segments == []


def test_synthetic_filing_with_unbalanced_balance_sheet_fails(synthetic_text: str) -> None:
    broken = synthetic_text.replace("Total assets $ 1,000 $ 850", "Total assets $ 1,010 $ 850")
    with pytest.raises(AccountingIdentityError, match="Assets = Liabilities \\+ Equity"):
        TenKExtractor("synthetic", text=broken).extract()


def test_synthetic_filing_missing_required_line_fails(synthetic_text: str) -> None:
    broken = synthetic_text.replace("Total assets $ 1,000 $ 850\n", "")
    with pytest.raises(MissingLineItemError, match="total_assets"):
        TenKExtractor("synthetic", text=broken).extract()


# --------------------------------------------------------------------------------------
# 3. Apple FY2015 golden values
# --------------------------------------------------------------------------------------
def test_apple_metadata(apple: FinancialStatementSchema) -> None:
    md = apple.metadata
    assert (md.name, md.cik, md.ticker) == ("Apple Inc.", "320193", "AAPL")
    assert md.fiscal_year_end == date(2015, 9, 26)


def test_apple_income_statement(apple: FinancialStatementSchema) -> None:
    assert apple.years == [2013, 2014, 2015]
    inc = apple.income_statement(2015)
    assert inc.net_sales == 233_715
    assert inc.operating_income == 71_230
    assert inc.net_income == 53_394
    assert inc.diluted_shares == pytest.approx(5_793.069)
    assert inc.eps_diluted == 9.22


def test_apple_cash_flow(apple: FinancialStatementSchema) -> None:
    cf = apple.cash_flow_statement(2015)
    assert cf.depreciation_and_amortization == 11_257
    assert cf.capital_expenditures == -11_247
    assert cf.cash_from_operations == 81_266
    assert cf.ending_cash == 21_120


def test_apple_retained_earnings_roll_forward(apple: FinancialStatementSchema) -> None:
    rf = next(r for r in apple.equity_roll_forwards if r.fiscal_year == 2015)
    assert (rf.beginning_retained_earnings, rf.ending_retained_earnings) == (87_152, 92_284)
    assert (rf.dividends_declared, rf.share_repurchases) == (-11_627, -36_026)


def test_apple_segments(apple: FinancialStatementSchema) -> None:
    by_type: dict[str, list[str]] = {}
    for s in apple.revenue_segments:
        by_type.setdefault(s.segment_type, []).append(s.name)
    assert by_type["geographic"] == [
        "Americas", "Europe", "Greater China", "Japan", "Rest of Asia Pacific",
    ]
    assert by_type["product"] == ["iPhone", "iPad", "Mac", "Services", "Other Products"]
    iphone = next(s for s in apple.revenue_segments if s.name == "iPhone")
    assert iphone.net_sales[2015] == 155_041


def test_apple_debt_schedule(apple: FinancialStatementSchema) -> None:
    ds = apple.debt_schedule
    assert ds is not None
    assert len(ds.tranches) == 28
    assert ds.commercial_paper == 8_499 and ds.commercial_paper_rate == 0.0014
    assert ds.principal_by_currency().keys() == {"USD", "EUR", "CHF", "JPY", "GBP", "AUD"}

    ranged = ds.tranches[1]  # "Fixed-rate 0.45% - 3.85% notes, 2016 - 2043"
    assert ranged.instrument_type is InstrumentType.FIXED_RATE_NOTE
    assert (ranged.coupon_rate_low, ranged.coupon_rate_high) == (0.0045, 0.0385)
    assert (ranged.maturity_year_start, ranged.maturity_year_end) == (2016, 2043)
    assert ranged.principal == 14_000

    yen = next(t for t in ds.tranches if t.currency == "JPY")
    assert (yen.principal, yen.effective_rate_low, yen.maturity_year_end) == (2_081, 0.0035, 2020)


def test_apple_tax_rates(apple: FinancialStatementSchema) -> None:
    assert apple.reported_effective_tax_rates == {2015: 0.264, 2014: 0.261, 2013: 0.262}
    assert apple.statutory_tax_rate == 0.35


def test_apple_narrative_disclosures(apple: FinancialStatementSchema) -> None:
    assert apple.shares_outstanding == pytest.approx(5_578.753)
    assert apple.foreign_cash_and_investments == pytest.approx(186_900)
    assert apple.undistributed_foreign_earnings == pytest.approx(91_500)
    assert apple.unrecognized_repatriation_tax == pytest.approx(30_000)
    assert apple.investment_portfolio_yield == 0.0149
