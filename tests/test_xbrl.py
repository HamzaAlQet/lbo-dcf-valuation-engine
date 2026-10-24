"""Tests for any-company ingestion: inline XBRL parsing, SEC companyfacts selection,
concept mapping, derived assumptions, and the end-to-end pipeline on real filings.

Fixtures (all offline):
- ``inline_xbrl_10k.htm``: a synthetic inline XBRL 10-K exercising the parser's edge
  cases (scale, formats, nested markup, fixed-zero, duplicates, a segment fact).
- ``companyfacts_{HD,MCD,ORCL}.json``: real SEC companyfacts data, trimmed to each
  company's latest 10-K. McDonald's (no COGS, shares tagged in millions, negative
  equity) and Oracle (capex above operating cash flow) are deliberately awkward.
"""

from __future__ import annotations

import copy
import json
import math
import re
import warnings
from pathlib import Path
from typing import Any

import pytest

from src.analyzer import AnalysisConfig, main, run_analysis
from src.auto_assumptions import derive_assumptions
from src.exceptions import AssumptionError, ExtractionError
from src.modeling_engine import build_model, depreciate_fixed_assets
from src.schemas import FinancialStatementSchema
from src.xbrl import (
    OutOfScopeError,
    _parse_number,
    company_facts_to_filing,
    load_inline_xbrl,
    parse_inline_xbrl,
)
from src.xbrl_mapping import build_schema

FIXTURES = Path(__file__).parent / "fixtures"
HTM = FIXTURES / "inline_xbrl_10k.htm"
GENERIC = Path(__file__).resolve().parents[1] / "config" / "scenarios_generic.json"


def facts_json(ticker: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"companyfacts_{ticker}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mapped() -> dict[str, FinancialStatementSchema]:
    return {t: build_schema(company_facts_to_filing(facts_json(t), ticker=t))
            for t in ("HD", "MCD", "ORCL")}


# --------------------------------------------------------------------------------------
# Inline XBRL parser
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "attrs", "expected"),
    [
        ("1,000", {"scale": "6"}, 1e9),
        ("(95)", {"scale": "6", "sign": "-"}, -95e6),
        ("—", {"format": "ixt:fixed-zero"}, 0.0),
        ("1.234,5", {"format": "ixt:num-comma-decimal"}, 1234.5),
        ("1.50", {}, 1.5),
        ("n/a", {}, None),
    ],
)
def test_parse_number(text: str, attrs: dict[str, str], expected: float | None) -> None:
    assert _parse_number(text, attrs) == expected


def test_inline_parser_reads_consolidated_facts_only() -> None:
    ff = load_inline_xbrl(HTM)
    assert (ff.entity_name, ff.ticker, ff.cik, ff.fiscal_year) == ("Widget Corp", "WDGT",
                                                                  "123456", 2024)
    revenue = sorted((f.end.year, f.value) for f in ff.facts["Revenues"])
    assert revenue == [(2023, 900e6), (2024, 1000e6)]  # segment fact and markup ignored
    assert len(ff.facts["NetIncomeLoss"]) == 2          # displayed twice, counted once
    assert ff.facts["Goodwill"][0].value == 0.0
    assert ff.annual("NetCashProvidedByUsedInInvestingActivities",
                     ff.period_end) == -100e6           # sign="-"


def test_inline_schema_ties() -> None:
    schema = build_schema(load_inline_xbrl(HTM))
    assert schema.data_source == "xbrl" and schema.years == [2023, 2024]
    bs = schema.balance_sheet(2024)
    assert bs.total_assets == bs.total_liabilities + bs.total_equity == 1_000
    assert bs.accrued_expenses == pytest.approx(70)      # remainder of current liabilities
    assert schema.cash_flow_statement(2024).ending_cash == 200
    assert schema.shares_outstanding == 99
    assert schema.derivation_notes == []


def test_non_10k_document_rejected() -> None:
    html = HTM.read_text(encoding="utf-8").replace(">10-K</ix:nonNumeric>",
                                                   ">10-Q</ix:nonNumeric>")
    with pytest.raises(OutOfScopeError, match="10-Q"):
        parse_inline_xbrl(html)


def test_document_without_xbrl_rejected() -> None:
    with pytest.raises(ExtractionError, match="No inline XBRL"):
        parse_inline_xbrl("<html><body><p>Annual report</p></body></html>")


# --------------------------------------------------------------------------------------
# companyfacts selection and mapping
# --------------------------------------------------------------------------------------
def test_companyfacts_selects_the_10k(mapped: dict[str, FinancialStatementSchema]) -> None:
    hd = mapped["HD"]
    assert hd.metadata.ticker == "HD" and len(hd.years) == 3
    assert hd.income_statement(hd.latest_year).net_sales == pytest.approx(164_683)
    with pytest.raises(ExtractionError, match="No 10-K for fiscal year 1999"):
        company_facts_to_filing(facts_json("HD"), fiscal_year=1999)


def test_awkward_filings_are_mapped_with_notes(mapped: dict[str, FinancialStatementSchema]
                                               ) -> None:
    mcd, orcl = mapped["MCD"], mapped["ORCL"]
    notes = " ".join(mcd.derivation_notes)
    assert "rescaled" in notes and "no cost of revenue reported" in notes
    assert mcd.balance_sheet(mcd.latest_year).total_equity < 0      # negative equity
    assert 600 < mcd.income_statement(mcd.latest_year).diluted_shares < 800
    assert orcl.reported_interest_expense                            # implied cost of debt


def test_bank_is_out_of_scope() -> None:
    data = facts_json("HD")
    any_fact = next(iter(data["facts"]["us-gaap"].values()))
    data["facts"]["us-gaap"]["Deposits"] = copy.deepcopy(any_fact)
    with pytest.raises(OutOfScopeError, match="bank"):
        build_schema(company_facts_to_filing(data))


def test_unclassified_balance_sheet_is_out_of_scope() -> None:
    data = facts_json("HD")
    del data["facts"]["us-gaap"]["AssetsCurrent"]
    with pytest.raises(OutOfScopeError, match="unclassified"):
        build_schema(company_facts_to_filing(data))


# --------------------------------------------------------------------------------------
# Derived assumptions and the generalized engine
# --------------------------------------------------------------------------------------
def test_derived_assumptions_policy(mapped: dict[str, FinancialStatementSchema]) -> None:
    hd = mapped["HD"]
    a = derive_assumptions(hd, share_price=380.0)
    growth = a.operating.revenue_growth
    assert growth[-1] == pytest.approx(0.03)                        # fades to long run
    inc = hd.income_statement(hd.latest_year)
    assert a.operating.gross_margin[0] == pytest.approx(inc.gross_margin / inc.net_sales)
    assert a.market.share_price == 380.0
    lbo = build_model(hd, a).lbo
    # 30% control premium and 5.0x leverage (multiples are stored to two decimals)
    assert lbo.entry["offer_premium"] == pytest.approx(0.30, abs=1e-3)
    assert lbo.entry["debt_to_ebitda"] == pytest.approx(5.0, abs=1e-3)
    assert all(a.market.sources.get(k) for k in ("risk_free_rate", "share_price"))


def test_overrides_and_loss_making_guard(mapped: dict[str, FinancialStatementSchema]) -> None:
    hd = mapped["HD"]
    a = derive_assumptions(hd, 380.0, {"market": {"levered_beta": 1.3}})
    assert a.market.levered_beta == 1.3
    payload = hd.model_dump()
    for row in payload["income_statements"]:
        if row["fiscal_year"] == hd.latest_year:
            row["operating_income"] -= 50_000           # push EBIT negative...
            row["other_operating_expenses"] += 50_000   # ...keeping the statement tied
            row["total_operating_expenses"] += 50_000
            row["other_income_expense"] += 50_000
    loss = FinancialStatementSchema.model_validate(payload)
    with pytest.raises(AssumptionError, match="loss-making"):
        derive_assumptions(loss, 380.0)


def test_revolver_funds_shortfall_and_cash_never_negative(
        mapped: dict[str, FinancialStatementSchema]) -> None:
    orcl = mapped["ORCL"]
    model = build_model(orcl, derive_assumptions(orcl, 200.0))
    bal = model.projection.balance
    assert (bal.loc["cash_and_equivalents"] >= 0).all()
    assert bal.loc["short_term_debt"].iloc[-1] > orcl.balance_sheet(orcl.latest_year).short_term_debt
    assert (model.projection.cash_flow.loc["dividends_paid"] <= 0).all()


def test_depreciation_runs_off_intangibles_then_other_assets() -> None:
    assert depreciate_fixed_assets(100, 50, 20, capex=10, da=30) == (80, 50, 20)
    assert depreciate_fixed_assets(10, 50, 20, capex=5, da=40) == (0, 25, 20)
    assert depreciate_fixed_assets(10, 5, 20, capex=0, da=30) == (0, 0, 5)


def test_full_analysis_runs_for_every_fixture(mapped: dict[str, FinancialStatementSchema]
                                              ) -> None:
    config = AnalysisConfig.from_json(GENERIC)
    for ticker, schema in mapped.items():
        inc = schema.income_statement(schema.latest_year)
        price = inc.net_income / inc.diluted_shares * 25
        analysis = run_analysis(schema, derive_assumptions(schema, price), config)
        assert analysis.recommendation.verdict in ("PROCEED", "PASS"), ticker
        bull, base, bear = (next(r for r in analysis.scenarios if r.name == n).metrics
                            for n in ("Bull", "Base", "Bear"))
        assert bear["lbo_irr"] < base["lbo_irr"] < bull["lbo_irr"], ticker


# --------------------------------------------------------------------------------------
# Workbook reconciliation on a real non-Apple filing
# --------------------------------------------------------------------------------------
def test_mcdonalds_workbook_reconciles(mapped: dict[str, FinancialStatementSchema],
                                       tmp_path: Path) -> None:
    formulas = pytest.importorskip("formulas")
    from src.excel_export import write_workbook

    mcd = mapped["MCD"]
    inc = mcd.income_statement(mcd.latest_year)
    analysis = run_analysis(mcd, derive_assumptions(mcd, inc.net_income / inc.diluted_shares * 25),
                            AnalysisConfig.from_json(GENERIC))
    report = write_workbook(analysis.base.model, tmp_path / "mcd.xlsx", analysis=analysis)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        solution = formulas.ExcelModel().loads(str(report.path)).finish().calculate()
    values = {}
    for key, ranges in solution.items():
        m = re.match(r"^'\[.+?\](.+?)'!([A-Z]+\d+)$", key)
        if m:
            values[f"{m.group(1)}!{m.group(2)}".upper()] = ranges.value[0][0]
    bad = []
    for addr, expected in report.expected.items():
        got = values.get(addr.upper())
        tol = report.tolerances.get(addr, 1e-6)
        ok = (got == expected if isinstance(expected, str) else
              isinstance(got, (int, float)) and math.isclose(got, expected, rel_tol=1e-9,
                                                             abs_tol=tol))
        if not ok:
            bad.append(f"{addr}: excel={got!r} python={expected!r}")
    assert not bad, bad[:10]
    assert values["CHECKS!C4"] == "OK"


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def test_cli_values_an_uploaded_10k(tmp_path: Path) -> None:
    code = main([str(HTM), "--share-price", "20", "--offline", "--out-dir", str(tmp_path)])
    assert code == 0
    assert {p.name for p in tmp_path.iterdir()} == {"model.xlsx", "memo.md", "analysis.json"}
    memo = (tmp_path / "memo.md").read_text(encoding="utf-8")
    assert "Widget Corp" in memo and "{{" not in memo


def test_cli_requires_share_price_for_xbrl(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main([str(HTM), "--offline", "--out-dir", str(tmp_path)])
