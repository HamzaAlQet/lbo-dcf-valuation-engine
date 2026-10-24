# Automated M&A / PE Valuation Engine

An end-to-end Python engine that values **any US-listed operating company from its
10-K**. Give it a ticker, or the 10-K `.htm` from SEC EDGAR, plus the current share
price. It builds a validated 3-statement model, a DCF and an LBO returns analysis,
exports them as a fully formula-driven Excel workbook, stress-tests the deal across
scenarios and sensitivity grids, and writes an investment committee memo, with the
narrative drafted by Claude.

```bash
python -m src.analyzer MSFT --share-price 420        # any ticker: 10-K data from SEC EDGAR
python -m src.analyzer path/to/10-k.htm --share-price 55.10
```

**Principle:** a number only enters the model if it ties. Every schema re-derives its
own subtotals and checks the accounting identities (A = L + E, cash roll-forward,
NI → retained earnings, debt note → balance sheet) at construction time, for reported
*and* projected years.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | 10-K ingestion, Pydantic schemas, identity validation | **Done** |
| 2 | 3-statement projection, DCF (WACC, Gordon + exit multiple), LBO waterfall, Excel export | **Done** |
| 3 | Sensitivity matrices, Base/Bull/Bear scenarios, risk metrics, Claude-generated IC memo | **Done** |
| 4 | Any company: XBRL ingestion (ticker or `.htm`), concept mapping, derived assumptions, web app | **Done** |

## Valuing any company

Every 10-K carries its financial statements as XBRL facts with standard US-GAAP tags.
The engine reads those facts, so it does not depend on any company's statement layout:

1. **Ingest** (`src/xbrl.py`): by ticker, through the SEC's `companyfacts` API
   (restricted to one 10-K filing), or from an uploaded inline-XBRL `.htm` 10-K. Both
   routes produce identical statements for the same filing (verified on Home Depot
   and Coca-Cola).
2. **Map** (`src/xbrl_mapping.py`): each schema line has a priority list of US-GAAP
   concepts. Where a filing reports a total without the breakdown, the remainder goes
   to a labelled "other" line, and every such derivation is recorded as a **data
   note**, shown on the workbook cover and in the app. Independently reported figures
   are still hard-checked: total assets = total liabilities & equity, the cash
   roll-forward, and EPS = net income / diluted shares.
3. **Derive assumptions** (`src/auto_assumptions.py`) from the company's own history:
   - **Growth:** trailing revenue CAGR fading to 3%.
   - **Ratios:** latest-year margins and cost ratios.
   - **Tax:** effective tax rate, clamped to 10%-30%.
   - **Payouts:** dividend and buyback ratios.
   - **Entry price:** a 30% control premium.
   - **Leverage:** debt sized at 5.0x EBITDA.
   - **Market inputs:** risk-free rate, ERP and beta are defaults marked **VERIFY**.

   Every value records its derivation and can be overridden (`--overrides file.json`, or
   the app's sidebar).
4. **Model, stress-test and write up** exactly as for the Apple sample, with generic
   Bull / Bear cases (`config/scenarios_generic.json`): growth ±2/4 points, margin
   ±1/2 points, exit multiple ±1/1.5 turns.

**Verified on 20 companies** (each one's latest 10-K, run offline from SEC data):

| Result | Companies |
|---|---|
| Modelled, workbook reconciles to Python with the master check **OK** (18) | AAPL, ADBE, BA, CAT, COST, HD, JNJ, KO, MCD, MSFT, NKE, NVDA, ORCL, PEP, PG, TSLA, UNH, WMT |
| Out of scope, refused with a clear reason (2) | DE (unclassified balance sheet, finance arm), INTC (operating loss in the latest year) |

Real-filing quirks the mapper handles, each disclosed in the data notes:
- **McDonald's:** share counts tagged in millions; no COGS line; negative equity.
- **Tesla and UnitedHealth:** mezzanine (temporary) equity.
- **Boeing:** preferred dividends, so the EPS cross-check is skipped.
- **Microsoft and Oracle:** split SG&A tags.
- **Nike and J&J:** EBIT derived from pre-tax income and net interest.
- **Oracle:** capex above operating cash flow, funded by revolver draws in the projection.

**Scope.** US-listed operating companies reporting in USD with a classified balance
sheet. Banks, insurers and finance-arm groups are refused, and so are years with an
operating loss (the default projection would have to invent a margin).

**SEC access.** Set `SEC_USER_AGENT="Your Name you@example.com"` as an environment
variable or Streamlit secret; the SEC asks every requester to identify itself.

## Sample case: Apple Inc. FY2015 (hypothetical take-private stress test)

The committed sample is Apple's 10-K for the fiscal year ended 26 September 2015,
extracted from a PDF with hand-written assumptions (`config/assumptions.json`). Apple
is far beyond the size of any leveraged buyout ever done, so the LBO is run as a
**hypothetical stress test**.

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
the repatriation tax.

### Stress test and recommendation: **PASS**

| Scenario | Key changes | IRR | MOIC | DCF (Gordon / Exit) |
|---|---|---:|---:|---:|
| Base | As above | 7.5% | 1.43x | $206.92 / $171.57 |
| Bull | 6–7% growth, offshore cash repatriated at 14%, 9.5x exit | 21.3% | 2.62x | $248.77 / $204.89 |
| Bear | iPhone peak: −8% revenue in FY2016, margin compression, 6.5x exit | −5.8% | 0.74x | $154.65 / $114.06 |

- **Break-evens (base case, 20% hurdle):** exit at 14.1x EBITDA, or entry at or below 4.4x.
- **Negative leverage:** at entry multiples of 9.0x and above, more debt *lowers* the
  IRR, because the cost of acquisition debt exceeds the unlevered return on the price paid.
- **Covenants are not the constraint:** the minimum EBITDA cushion is 26% in the base
  case and remains positive in the bear case. Returns are what fail.
- **The verdict is rule-based:** PASS because base-case IRR (7.5% vs 20%) and MOIC
  (1.43x vs 2.0x) miss the criteria in `config/scenarios.json`.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev,memo,app]"

python -m src.analyzer HD --share-price 380             # any company -> outputs/HD/
python -m src.analyzer outputs/extracted/apple_inc_fy2015.json   # Apple sample -> outputs/
streamlit run streamlit_app.py                           # interactive dashboard
pytest
```

`src.analyzer` runs the whole pipeline: projection, DCF, LBO, scenarios, sensitivities
and the memo, writing `model.xlsx`, `memo.md` and `analysis.json`.
- **Input:** a ticker or CIK, an EDGAR 10-K `.htm`, the Apple PDF, or extracted JSON.
- **Other options:** `--fiscal-year` picks an older 10-K, and `--overrides` adjusts the
  derived assumptions.
- **Errors:** any data problem exits with code 1 and prints the reason (an
  out-of-scope filing, or every failed identity).

**Claude memo narrative.** With `ANTHROPIC_API_KEY` set (`pip install -e ".[memo]"`),
the analyzer drafts the memo with Claude (`claude-opus-5-5`). Without credentials, or
with `--offline`, it writes the same memo from a deterministic template and says so in
the memo header.

## Web app (`streamlit_app.py`)

An interactive dashboard over the same engine:

- **Any company.** Choose *Look up a ticker* or *Upload a 10-K (.htm)*, enter the
  share price, and the full analysis runs on that company's filing. The Apple sample
  (with its committed memo) is the default view.
- **The sidebar flexes the deal.** Entry and exit multiples, leverage, growth and
  margin shifts, and the DCF inputs are all adjustable, with ranges scaled to each
  company's defaults.
- **Data notes.** Any line derived rather than reported is listed on the Overview tab.
- **Everything recomputes live.** Each change reruns the projection, DCF, LBO,
  scenarios and sensitivities across six tabs: Overview, DCF, LBO, Scenarios, IC memo
  and Downloads.
- **Downloads match the screen.** The Excel model, memo and data pack are all built
  for the assumptions currently shown.

**Claude memos use the visitor's own API key.** The IC memo tab has a password field
for an Anthropic API key:

- **The app holds no Anthropic key of its own.** It never reads one from `st.secrets`
  or the environment, so hosting it costs the owner nothing. (The only secret it reads
  is the non-sensitive `SEC_USER_AGENT`.)
- **A visitor's key is used only for their request.** It is held in memory for their
  browser session and never stored or logged.
- **Problems are reported.** A rejected key or an unreachable API produces an error
  message, never a silent template memo.

**Deploy to Streamlit Community Cloud:**

1. Push this repository to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io), click **Create app** and pick
   the repository, branch `main` and main file `streamlit_app.py`.
3. Under **Advanced settings**, choose Python 3.12 or later. Dependencies install from
   `requirements.txt`.
4. Under **Secrets**, add one line: `SEC_USER_AGENT = "Your Name you@example.com"`.
   No Anthropic key is needed.

The first page load extracts the Apple sample PDF, which takes 10–20 seconds; ticker
lookups take a few seconds. Results are cached for every visitor after that.

## The Excel model (`outputs/model.xlsx`)

| Sheet | Contents |
|---|---|
| Cover | Key outputs (linked), contents, modelling conventions |
| Inputs | Every assumption, plus 10-K disclosures; historical driver ratios shown for context |
| IS / BS / CFS | Three linked statements: reported history (blue) and projections (formulas) |
| Debt | 28 existing tranches, reconciled to the balance sheet; blended cost of debt by `SUMPRODUCT` |
| DCF | WACC build, unlevered FCF, mid-year discounting, both terminal methods, equity bridge |
| LBO | Cash at close, Sources & Uses, interest, cash sweep by tranche, covenants, MOIC / IRR |
| Sensitivity | Heat-mapped grids: DCF price for WACC × growth and exit multiple × WACC (**live formulas**), LBO IRR and MOIC for entry multiple × leverage (engine values) |
| Scenarios | Base / Bull / Bear assumptions, outputs and variance vs. base; recommendation tests |
| Checks | Every balance and cash tie for every year, plus a master check |

- **No hard-coded calculations.** Hard-coded numbers appear only for reported 10-K data
  and on the Inputs sheet. Every subtotal, projection and return is a formula
  (`=SUM`, `=MIN`, `=MAX`, `=IF`, `=SUMPRODUCT`, `=IRR`), and a test enforces this.
- **Excel = Python, proven.** The tests recalculate the workbook with the `formulas`
  engine and reconcile all ~820 formula cells, including the live DCF sensitivity
  grids, against the Python engine to 1e-9 relative tolerance. A negative-control test adds $1 to one balance-sheet cell and
  checks that the master check flips to `ERROR`.
- **Colour convention:** blue = input / reported, black = formula, green = link to
  another sheet. Gridlines are on and panes are frozen.

## The IC memo (`outputs/memo.md`)

The memo has seven sections: executive summary and thesis, transaction overview (S&U),
valuation and returns (Base / Bull / Bear), key risks, downside sensitivities, covenant
headroom, and the final recommendation, followed by an assumptions appendix.

**Numbers come from the engine, prose from Claude.** The memo is built in a two-step
prompt chain on `claude-opus-5-5`:

1. **Risk assessment.** The full deal data pack (`analysis.json`) goes in; a
   Pydantic-validated `RiskAssessment` comes back via structured outputs.
2. **Narrative.** Claude writes the prose around `{{TABLE_...}}` placeholders, and
   Python substitutes tables rendered directly from the model, so no table figure can
   be mis-transcribed.

**Guardrails:**
- **The verdict is fixed.** Proceed / Pass is decided by explicit investment
  criteria. The memo is rejected if Claude contradicts it, drops a section, or
  misplaces a table.
- **The data pack is cached.** It sits in a prompt-cached system prefix shared by both
  calls.
- **Refusals are handled.** Server-side refusal fallbacks are enabled
  (`fallbacks: "default"`), and a refusal or truncation raises rather than producing a
  partial memo.

## Project layout

```
config/assumptions.json   Apple sample: market, operating and transaction assumptions
config/scenarios.json     Apple sample: Bull / Bear overrides, criteria, grid axes
config/scenarios_generic.json  Any company: relative Bull / Bear shifts, criteria, grids
data/raw/                 Source filings (Apple sample PDF)
src/exceptions.py         Error hierarchy (AccountingIdentityError, AssumptionError, ...)
src/schemas.py            Pydantic models: statements, debt, assumptions, identity checks
src/metrics.py            Pure formulas (UFCF, NOPAT, EBITDA, NPV, IRR): unit-tested
src/xbrl.py               XBRL ingestion: SEC companyfacts by ticker, inline-XBRL .htm parser
src/xbrl_mapping.py       US-GAAP concepts -> validated statements, with data notes
src/auto_assumptions.py   Per-company assumptions derived from the filing + share price
src/extractor.py          Apple sample: 10-K PDF -> FinancialStatementSchema
src/modeling_engine.py    3-statement projection, DCF, LBO, and CLI
src/excel_export.py       Formula-driven openpyxl workbook
src/analyzer.py           Scenarios, sensitivity grids, risk metrics, recommendation, CLI
src/memo_generator.py     Claude prompt chain, memo validation, template fallback
src/dashboard.py          Pure helpers for the web app: controls, charts, workbook bytes
streamlit_app.py          Streamlit dashboard (entry point for Streamlit Community Cloud)
requirements.txt          Runtime dependencies for the hosted app
tests/                    166 pytest tests, all offline (SEC data via trimmed fixtures;
                          Claude via a fake client; app via AppTest)
outputs/                  Generated JSON, model.xlsx, memo.md, analysis.json
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
