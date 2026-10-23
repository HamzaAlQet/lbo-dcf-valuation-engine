# Automated M&A / PE Valuation Engine

An end-to-end Python engine that reads an SEC 10-K, extracts audited financials into
a validated data model, and drives a linked 3-statement model, a DCF, and an LBO
returns analysis, exported as a fully formula-driven Excel workbook. An investment
committee memo follows in Phase 3.

**Principle:** a number only enters the model if it ties. Every schema re-derives its
own subtotals and checks the accounting identities (A = L + E, cash roll-forward,
NI → retained earnings, debt note → balance sheet) at construction time, for reported
*and* projected years.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | 10-K ingestion, Pydantic schemas, identity validation | **Done** |
| 2 | 3-statement projection, DCF (WACC, Gordon + exit multiple), LBO waterfall, Excel export | **Done** |
| 3 | Sensitivity matrices, Base/Bull/Bear scenarios, Claude-generated IC memo | Next |

## Sample case: Apple Inc. FY2015 (hypothetical take-private stress test)

The sample filing is Apple's 10-K for the fiscal year ended 26 September 2015. Apple is
far beyond the size of any leveraged buyout ever done, so the LBO is run as a
**hypothetical stress test**. The engine itself does not depend on the company.

Extracted from the filing and verified (USD millions):

| | FY2013 | FY2014 | FY2015 |
|---|---:|---:|---:|
| Net sales | 170,910 | 182,795 | 233,715 |
| EBITDA (EBIT + D&A) | 55,756 | 60,449 | 82,487 |
| EBITDA margin | 32.6% | 33.1% | 35.3% |
| Unlevered FCF | 41,253 | 44,208 | 64,105 |

The filing also discloses the facts that dominate any Apple LBO: **$186.9B of its
$205.7B** in cash and securities was held offshore, and the unrecognized tax on
repatriating foreign earnings implies a **32.8% repatriation tax** ($30.0B / $91.5B).

### Base-case outputs

| | |
|---|---|
| WACC | 7.51% (Ke 8.13% via CAPM; after-tax Kd 1.35%; E/V 90.9%) |
| DCF, Gordon Growth (g = 2.5%) | EV $1,083B → **$206.92 / share** (+80% vs $114.71) |
| DCF, Exit Multiple (10.0x) | EV $884B → **$171.57 / share** (+50%) |
| LBO entry | 8.0x LTM EBITDA = $660B EV; implied offer $142.59 (+24% premium) |
| LBO financing | $330B debt (4.0x), **$540B sponsor equity** |
| LBO returns (5-year hold, exit at 8.0x) | **1.43x MOIC, 7.5% IRR**: below any sponsor hurdle |

The term loan is swept down from $231B to $8B by FY2020, but returns are capped by the
size of the equity cheque and by the offshore cash, which cannot fund the deal without
the repatriation tax. Phase 3 will stress these drivers.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"

python -m src.extractor data/raw/apple_10k_fy2015.pdf --ticker AAPL   # -> outputs/extracted/*.json
python -m src.modeling_engine outputs/extracted/apple_inc_fy2015.json  # -> outputs/model.xlsx
pytest
```

Both commands exit with code 1 and print every failed identity if anything does not tie.
`modeling_engine` also accepts the PDF directly and runs extraction first.

## The Excel model (`outputs/model.xlsx`)

| Sheet | Contents |
|---|---|
| Cover | Key outputs (linked), contents, modelling conventions |
| Inputs | Every assumption, plus 10-K disclosures; historical driver ratios shown for context |
| IS / BS / CFS | Three linked statements: reported history (blue) and projections (formulas) |
| Debt | 28 existing tranches, reconciled to the balance sheet; blended cost of debt by `SUMPRODUCT` |
| DCF | WACC build, unlevered FCF, mid-year discounting, both terminal methods, equity bridge |
| LBO | Cash at close, Sources & Uses, interest, cash sweep by tranche, covenants, MOIC / IRR |
| Checks | Every balance and cash tie for every year, plus a master check |

- **No hard-coded calculations.** Hard-coded numbers appear only for reported 10-K data
  and on the Inputs sheet. Every subtotal, projection and return is a formula
  (`=SUM`, `=MIN`, `=MAX`, `=IF`, `=SUMPRODUCT`, `=IRR`), and a test enforces this.
- **Excel = Python, proven.** The tests recalculate the workbook with the `formulas`
  engine and reconcile all ~750 formula cells against the Python engine to 1e-9
  relative tolerance. A negative-control test adds $1 to one balance-sheet cell and
  checks that the master check flips to `ERROR`.
- **Colour convention:** blue = input / reported, black = formula, green = link to
  another sheet. Gridlines are on and panes are frozen.

## Project layout

```
config/assumptions.json   Market, operating and transaction assumptions, each with a source
data/raw/                 Source filings
src/exceptions.py         Error hierarchy (AccountingIdentityError, AssumptionError, ...)
src/schemas.py            Pydantic models: statements, debt, assumptions, identity checks
src/metrics.py            Pure formulas (UFCF, NOPAT, EBITDA, NPV, IRR): unit-tested
src/extractor.py          10-K -> FinancialStatementSchema pipeline and CLI
src/modeling_engine.py    3-statement projection, DCF, LBO, and CLI
src/excel_export.py       Formula-driven openpyxl workbook
src/analyzer.py           Phase 3
tests/                    103 pytest tests (see below)
outputs/                  Generated JSON, model.xlsx, memo.md
```

## What gets validated

| Level | Identity |
|---|---|
| Income statement | GM = Sales − COGS; EBIT = GM − Opex; PBT = EBIT + Other; NI = PBT − Tax; EPS = NI / Diluted shares |
| Balance sheet | Every subtotal; **Assets = Liabilities + Equity** (reported and every projected year) |
| Cash flow | CFO / CFI / CFF = sum of lines; ΔCash = CFO + CFI + CFF; End = Begin + ΔCash |
| Equity | Ending RE = Opening RE + NI − Dividends − Buybacks ± Other; total equity roll-forward |
| Debt | Σ tranches = reported face; Face + discount + hedge adj. = Current + LT debt |
| Cross-statement | NI (IS = CFS = equity statement); CFS cash = BS cash; RE and equity tie to BS; segments sum to net sales; debt schedule ties to BS; reported ETR = Tax / PBT |
| LBO | Sources = Uses; debt never negative; senior repaid before subordinated; cash conserved through the waterfall |

Identities are checked to within $1M, which absorbs the filing's own rounding. Every
breach found in a validation pass is reported together, e.g.:

```
[ACCOUNTING IDENTITY FAILURE] Balance Sheet FY2015: 2 identity check(s) failed
  - Total assets = Current + Non-current assets: 290,489.00 != 290,479.00 (delta = +10.00, tolerance = 1.00)
  - Assets = Liabilities + Equity: 290,489.00 != 290,479.00 (delta = +10.00, tolerance = 1.00)
```

## Modelling conventions

- **No circular references.** Interest expense and income are calculated on
  beginning-of-year balances, so neither Python nor Excel needs iteration.
- **3-statement projection.** Revenue grows at the driver rate; COGS, opex, D&A and
  capex follow sales. Working capital uses days (DSO on sales, DIO and DPO on COGS) or
  % of sales, held at FY2015 actuals unless overridden. Funded debt is refinanced (held
  flat). D&A runs through PP&E. Cash is the output of the cash flow statement, never a
  plug, and the balance sheet must then balance.
- **DCF.** UFCF = EBIT × (1 − t) + D&A − Capex − ΔNWC, with mid-year discounting and
  terminal value at the end of year 5. The equity bridge adds cash and securities,
  deducts debt, and deducts repatriation tax on offshore cash. Each terminal method
  reports the other's implied metric as a cross-check.
- **LBO.** Entry EV = multiple × LTM EBITDA; target equity is bought at EV − debt + cash.
  Domestic cash above a $10B floor funds the deal; offshore cash stays trapped by
  default and is credited at exit net of repatriation tax. Each year, levered FCF pays
  mandatory amortization first; the cash sweep then retires prepayable tranches in
  priority order. Cash taxes are floored at zero (no NOL carry-forward).
- **Shares.** Per-share values use period-end shares outstanding plus FY2015 dilution
  (5,618M).

## Design notes

- **Text extraction.** The sample PDF is a browser print of the EDGAR HTML filing.
  Layout-preserving extraction separates labels from their numbers, so the pipeline
  uses `pypdf` reading order and matches rows by label, never by page position.
- **Label mapping.** Each schema field has a set of regex synonyms (`FieldSpec`).
  Matching is exact on the full normalized label, can be limited to a section, and
  raises if two rows match one field. To support another filer, add synonyms.
- **One vocabulary.** Projection row keys are the schema field names, so projected
  statements are validated by the same Pydantic models as reported ones, and the
  Excel writer, the engine and the tests all refer to the same keys.
- **Market inputs** in `config/assumptions.json` are approximate values for September
  2015 and are marked *VERIFY* in their `sources` entries. Confirm them before relying
  on the valuation outputs.
