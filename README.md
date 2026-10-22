# Automated M&A / PE Valuation Engine

An end-to-end Python engine that reads an SEC 10-K, extracts audited financials into
a validated data model, and (in later phases) drives a linked 3-statement model, DCF,
LBO returns analysis and an investment committee memo.

**Principle:** a number only enters the model if it ties. Every schema re-derives its
own subtotals and checks the accounting identities (A = L + E, cash roll-forward,
NI → retained earnings, debt note → balance sheet) at construction time.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | 10-K ingestion, Pydantic schemas, identity validation, tests | **Done** |
| 2 | 3-statement projection, DCF (WACC, Gordon + exit multiple), LBO waterfall, Excel export | Next |
| 3 | Sensitivity matrices, Base/Bull/Bear scenarios, Claude-generated IC memo | Planned |

## Sample case: Apple Inc. FY2015 (hypothetical take-private stress test)

The sample filing is Apple's 10-K for the fiscal year ended 26 September 2015. With an
enterprise value of roughly $650B, Apple is far beyond the size of any leveraged buyout
ever done, and it held ~$206B of cash and marketable securities against ~$64B of debt.
The LBO module is therefore run as a **hypothetical stress test**. It shows how the
engine sizes debt, uses balance-sheet cash as a funding source and reports returns.
The memo is expected to reach a reasoned *Pass*. The engine itself does not depend on
the company.

Extracted from the filing and verified (USD millions):

| | FY2013 | FY2014 | FY2015 |
|---|---:|---:|---:|
| Net sales | 170,910 | 182,795 | 233,715 |
| EBITDA (EBIT + D&A) | 55,756 | 60,449 | 82,487 |
| EBITDA margin | 32.6% | 33.1% | 35.3% |
| Capex | 8,165 | 9,571 | 11,247 |
| Unlevered FCF | 41,253 | 44,208 | 64,105 |

Term debt: 28 tranches in 6 currencies, $55.7B face value, 2.09% weighted-average
effective rate, plus $8.5B of commercial paper at 0.14%.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"

python -m src.extractor data/raw/apple_10k_fy2015.pdf --ticker AAPL
pytest
```

The extractor writes `outputs/extracted/apple_inc_fy2015.json` and prints a summary of
historical metrics. It exits with code 1 and a full list of breaches if anything fails
to tie.

## Project layout

```
config/assumptions.json   Market inputs not in the 10-K (Rf, ERP, beta, exit multiple), each with a source
data/raw/                 Source filings
src/exceptions.py         Error hierarchy (AccountingIdentityError, MissingLineItemError, ...)
src/schemas.py            Pydantic models + identity checks
src/metrics.py            Pure formulas (UFCF, NOPAT, EBITDA): one definition, unit-tested
src/extractor.py          10-K -> FinancialStatementSchema pipeline and CLI
src/modeling_engine.py    Phase 2
src/analyzer.py           Phase 3
tests/                    pytest: schema identities, FCF logic, parser, Apple golden values
outputs/                  Generated JSON, model.xlsx, memo.md
```

## What gets validated

| Level | Identity |
|---|---|
| Income statement | GM = Sales − COGS; EBIT = GM − Opex; PBT = EBIT + Other; NI = PBT − Tax; EPS = NI / Diluted shares |
| Balance sheet | Every subtotal; **Assets = Liabilities + Equity** |
| Cash flow | CFO / CFI / CFF = sum of lines; ΔCash = CFO + CFI + CFF; End = Begin + ΔCash |
| Equity | Ending RE = Opening RE + NI − Dividends − Buybacks ± Other; total equity roll-forward |
| Debt | Σ tranches = reported face; Face + discount + hedge adj. = Current + LT debt |
| Cross-statement | NI (IS = CFS = equity statement); CFS cash = BS cash (current and prior year); RE and equity tie to BS; segments sum to net sales; debt schedule ties to BS; reported ETR = Tax / PBT |

Identities are checked to within $1M, which absorbs the filing's own rounding. The
tolerance can be overridden with `Model.model_validate(data, context={"tolerance": x})`.
Every breach found in a validation pass is reported together, e.g.:

```
[ACCOUNTING IDENTITY FAILURE] Balance Sheet FY2015: 2 identity check(s) failed
  - Total assets = Current + Non-current assets: 290,489.00 != 290,479.00 (delta = +10.00, tolerance = 1.00)
  - Assets = Liabilities + Equity: 290,489.00 != 290,479.00 (delta = +10.00, tolerance = 1.00)
```

## Design notes

- **Text extraction.** The sample PDF is a browser print of the EDGAR HTML filing.
  Layout-preserving extraction separates labels from their numbers, so the pipeline
  uses `pypdf` reading order and matches rows by label, never by page position.
- **Label mapping.** Each schema field has a set of regex synonyms (`FieldSpec`).
  Matching is exact on the full normalized label, can be limited to a section
  (operating / investing / financing), and raises if two rows match one field.
  To support another filer, add synonyms; the parsing code does not change.
- **Sign conventions.** Cash flow and equity items are stored as reported (outflows
  negative), so every subtotal is a plain sum. `metrics.unlevered_free_cash_flow`
  takes capex as a positive outflow and ΔNWC as the *increase* in NWC.
- **Debt ranges.** 10-K debt tables group several notes on one line ("0.45%–3.85%
  notes due 2016–2043"), so tranches store `[low, high]` bounds. The maturity ladder
  spreads ranged tranches evenly across their years. This is an explicit assumption.
- **Market inputs** in `config/assumptions.json` are approximate values for September
  2015 and are marked *VERIFY* in their `sources` entries. Confirm them before relying
  on the valuation outputs.
