"""Streamlit dashboard for the LBO & DCF valuation engine.

Run locally::

    streamlit run streamlit_app.py

What a visitor can do:

- **Value any US-listed operating company** by typing its ticker (the 10-K's XBRL data
  is fetched from SEC EDGAR) or uploading the 10-K ``.htm`` from EDGAR, plus the
  current share price. Assumptions are derived from the company's own filing.
- **Explore the Apple FY2015 sample** (the default), built from the PDF 10-K.
- **Flex the sidebar:** the projection, DCF, LBO, scenarios and sensitivities
  recompute live.
- **Generate a Claude memo with their own Anthropic API key.** The app never uses a
  server-side Anthropic key, so hosting it costs the owner nothing.

Configuration: set ``SEC_USER_AGENT`` (``"Your Name you@example.com"``, as the SEC
requires) as an environment variable or a Streamlit secret.
"""

from __future__ import annotations

import hashlib
import json
import os

import anthropic
import streamlit as st
from pydantic import ValidationError

from src import memo_generator
from src.analyzer import (
    GENERIC_SCENARIOS,
    AnalysisConfig,
    AnalysisResult,
    analysis_context,
    run_analysis,
)
from src.auto_assumptions import derive_assumptions
from src.dashboard import (
    ASSUMPTIONS_FILE,
    SAMPLE_FILING,
    SAMPLE_MEMO,
    SCENARIOS_FILE,
    Controls,
    apply_controls,
    covenant_frame,
    debt_paydown_chart,
    heatmap,
    projection_chart,
    scenario_frame,
    sources_uses_frame,
    workbook_bytes,
)
from src.exceptions import FinancialDataError
from src.extractor import TenKExtractor
from src.schemas import FinancialStatementSchema, ModelAssumptions
from src.xbrl import parse_inline_xbrl
from src.xbrl_mapping import build_schema, schema_from_ticker

st.set_page_config(page_title="LBO & DCF Valuation Engine", page_icon=":material/monitoring:",
                   layout="wide")

# The SEC asks for a contact in the User-Agent; take it from Streamlit secrets if set.
try:
    if "SEC_USER_AGENT" in st.secrets:
        os.environ.setdefault("SEC_USER_AGENT", str(st.secrets["SEC_USER_AGENT"]))
except Exception:  # no secrets file: fall back to the environment
    pass

SAMPLE, TICKER, UPLOAD = "Apple FY2015 sample", "Look up a ticker", "Upload a 10-K (.htm)"


# --------------------------------------------------------------------------------------
# Cached loading and computation (shared across sessions)
# --------------------------------------------------------------------------------------
@st.cache_resource(show_spinner="Extracting and validating the sample 10-K (first load only)...")
def load_sample() -> FinancialStatementSchema:
    """Parse the Apple sample PDF once per server."""
    return TenKExtractor(SAMPLE_FILING, ticker="AAPL").extract()


@st.cache_resource(max_entries=32, show_spinner="Fetching the 10-K's XBRL data from SEC EDGAR...")
def load_ticker(ticker: str, fiscal_year: int | None) -> FinancialStatementSchema:
    """Fetch and map a company's 10-K by ticker (cached per ticker and year)."""
    return schema_from_ticker(ticker, fiscal_year)


@st.cache_resource(max_entries=16, show_spinner="Reading the uploaded 10-K...")
def load_upload(digest: str, _html: str, name: str) -> FinancialStatementSchema:
    """Parse an uploaded inline-XBRL 10-K (cached by content hash)."""
    return build_schema(parse_inline_xbrl(_html, source=name))


@st.cache_resource(max_entries=32,
                   show_spinner="Running projection, DCF, LBO, scenarios and sensitivities...")
def run(schema_json: str, assumptions_json: str, config_path: str) -> AnalysisResult:
    """Full analysis for one company and assumption set (cached by value)."""
    return run_analysis(FinancialStatementSchema.model_validate_json(schema_json),
                        ModelAssumptions.model_validate_json(assumptions_json),
                        AnalysisConfig.from_json(config_path))


@st.cache_data(max_entries=8, show_spinner="Building the Excel workbook...")
def build_workbook(schema_json: str, assumptions_json: str, config_path: str) -> bytes:
    """Formula-driven workbook for one analysis."""
    return workbook_bytes(run(schema_json, assumptions_json, config_path))


@st.cache_data(max_entries=32, show_spinner=False)
def template_memo(schema_json: str, assumptions_json: str, config_path: str) -> str:
    """Deterministic memo for one analysis."""
    return memo_generator.generate_memo(run(schema_json, assumptions_json, config_path),
                                        offline=True).markdown


def stop_with(message: str) -> None:
    """Show an error and halt this run."""
    st.error(message, icon=":material/error:")
    st.stop()


# --------------------------------------------------------------------------------------
# Sidebar: company
# --------------------------------------------------------------------------------------
with st.sidebar:
    st.header("Company")
    mode = st.radio("Source", [SAMPLE, TICKER, UPLOAD], key="mode", label_visibility="collapsed")
    schema: FinancialStatementSchema | None = None
    company_key = "sample"
    share_price: float | None = None
    try:
        if mode == SAMPLE:
            schema = load_sample()
        elif mode == TICKER:
            with st.form("ticker_form", border=False):
                ticker = st.text_input("Ticker", placeholder="e.g. MSFT", key="ticker_input")
                fy = st.number_input("Fiscal year of the 10-K (0 = latest)", 0, 2100, 0,
                                     key="fy_input")
                price_in = st.number_input("Current share price (USD)", 0.0, 1e6, 0.0,
                                           step=1.0, format="%.2f", key="price_ticker")
                st.form_submit_button("Value company", type="primary")
            if ticker.strip() and price_in > 0:
                schema = load_ticker(ticker.strip().upper(), int(fy) or None)
                company_key, share_price = f"ticker:{ticker.strip().upper()}:{int(fy)}", price_in
        else:
            upload = st.file_uploader("10-K .htm from EDGAR", type=["htm", "html", "xhtml"],
                                      key="upload")
            price_in = st.number_input("Current share price (USD)", 0.0, 1e6, 0.0,
                                       step=1.0, format="%.2f", key="price_upload")
            if upload is not None and price_in > 0:
                raw = upload.getvalue()
                digest = hashlib.sha256(raw).hexdigest()
                schema = load_upload(digest, raw.decode("utf-8", errors="replace"), upload.name)
                company_key, share_price = f"upload:{digest}", price_in
    except (FinancialDataError, ValidationError) as exc:
        stop_with(f"This filing could not be modelled.\n\n{exc}")

if schema is None:
    st.title("Automated LBO & DCF Valuation Engine")
    st.info("Enter a ticker or upload a 10-K, together with the current share price, in the "
            "sidebar. Data comes from the 10-K's XBRL financial statements on SEC EDGAR.",
            icon=":material/arrow_back:")
    st.markdown(
        "- **Ticker lookup** fetches the most recent 10-K (or the fiscal year you choose).\n"
        "- **Upload** accepts the 10-K `.htm` document from EDGAR (inline XBRL, filed since "
        "2019-2021). PDFs are not accepted: the XBRL data is what makes any company work.\n"
        "- **In scope:** US-listed operating companies reporting in USD. Banks, insurers and "
        "other unclassified balance sheets are out of scope, as are years with operating "
        "losses (their margins would have to be invented).")
    st.stop()

# Assumptions: the hand-written config for the sample, derived from the filing otherwise.
try:
    if mode == SAMPLE:
        base_assumptions = ModelAssumptions.from_json(ASSUMPTIONS_FILE)
        config_path = str(SCENARIOS_FILE)
    else:
        base_assumptions = derive_assumptions(schema, share_price)  # type: ignore[arg-type]
        config_path = str(GENERIC_SCENARIOS)
except (FinancialDataError, ValidationError) as exc:
    stop_with(f"Default assumptions could not be derived for this company.\n\n{exc}")

defaults = Controls.from_assumptions(base_assumptions)
CONTROL_KEYS = list(defaults.as_dict())

# A new company resets the sliders to its own defaults.
if st.session_state.get("company_key") != company_key:
    for key in CONTROL_KEYS:
        st.session_state.pop(key, None)
    st.session_state["company_key"] = company_key
    st.session_state.pop("claude_memo", None)


def reset_controls() -> None:
    """Restore every control to the base case."""
    for key in CONTROL_KEYS:
        st.session_state.pop(key, None)


def pct(label: str, key: str, lo: float, hi: float, step: float, help_: str = "") -> float:
    """Slider in percent that returns a decimal; the range always contains the default."""
    default = round(getattr(defaults, key) * 100, 2)
    return st.slider(label, min(lo, default), max(hi, default), default, step,
                     format="%.1f%%", key=key, help=help_ or None) / 100


def multiple(label: str, key: str) -> float:
    """Multiple slider ranging from half to double the company's default."""
    default = round(getattr(defaults, key), 2)
    return st.slider(label, round(max(1.0, default / 2), 1), round(default * 2, 1), default,
                     0.5, format="%.1fx", key=key)


with st.sidebar:
    st.header("Assumptions")
    st.caption("Defaults: " + ("`config/assumptions.json`." if mode == SAMPLE else
                               "derived from the company's own 10-K (see the Inputs sheet)."))
    st.button("Reset to base case", on_click=reset_controls, icon=":material/restart_alt:")

    st.subheader("LBO transaction")
    entry = multiple("Entry EV / EBITDA", "entry_multiple")
    exit_ = multiple("Exit EV / EBITDA", "exit_multiple")
    debt = pct("Debt, % of entry EV", "debt_pct", 5.0, 90.0, 5.0,
               "Acquisition debt as a share of entry enterprise value.")
    repat = defaults.foreign_cash_usable_pct
    if schema.foreign_cash_and_investments:
        repat = pct("Offshore cash repatriated at close", "foreign_cash_usable_pct", 0.0,
                    100.0, 25.0, "Share of the offshore cash disclosed in the 10-K that is "
                                 "brought home, net of the repatriation tax, to fund the deal.")

    st.subheader("Operations (all projection years)")
    growth = pct("Revenue growth shift", "revenue_growth_shift", -6.0, 6.0, 0.5,
                 "Added to every year's base-case growth rate.")
    margin = pct("Gross margin shift", "gross_margin_shift", -4.0, 4.0, 0.25,
                 "Added to every year's base-case gross margin.")

    st.subheader("DCF valuation")
    rf = pct("Risk-free rate", "risk_free_rate", 0.5, 7.0, 0.05, "10-year Treasury yield.")
    erp = pct("Equity risk premium", "equity_risk_premium", 3.0, 9.0, 0.25)
    beta = st.slider("Levered beta", min(0.3, defaults.levered_beta), max(2.5, defaults.levered_beta),
                     defaults.levered_beta, 0.05, key="levered_beta")
    tg = pct("Terminal growth", "terminal_growth_rate", 0.0, 4.0, 0.25, "Gordon Growth method.")
    dcf_mult = multiple("Terminal EV / EBITDA", "dcf_exit_multiple")

controls = Controls(
    entry_multiple=entry, exit_multiple=exit_, debt_pct=debt, foreign_cash_usable_pct=repat,
    revenue_growth_shift=growth, gross_margin_shift=margin, risk_free_rate=rf,
    equity_risk_premium=erp, levered_beta=beta, terminal_growth_rate=tg,
    dcf_exit_multiple=dcf_mult)
is_base_case = controls == defaults

try:
    assumptions = apply_controls(base_assumptions, controls)
    run_key = (schema.model_dump_json(), assumptions.model_dump_json(), config_path)
    analysis = run(*run_key)
except (ValidationError, FinancialDataError) as exc:
    stop_with(f"These assumptions are not valid together:\n\n{exc}")

model = analysis.base.model
lbo, dcf, rec = model.lbo, model.dcf, analysis.recommendation
price = model.assumptions.market.share_price
md = model.schema.metadata

# --------------------------------------------------------------------------------------
# Header & KPIs
# --------------------------------------------------------------------------------------
st.title(f"{md.name}" + (f" ({md.ticker})" if md.ticker else ""))
source_text = ("10-K PDF" if model.schema.data_source == "pdf"
               else "10-K XBRL data from SEC EDGAR")
st.caption(f"{source_text}, fiscal year ended {md.fiscal_year_end:%d %B %Y} → validated "
           f"3-statement model, DCF, LBO and investment committee memo. "
           f"{analysis_context(analysis)['framing']}")

if not is_base_case:
    st.info("Showing custom assumptions. Use **Reset to base case** in the sidebar to "
            "return to the defaults.", icon=":material/tune:")
verdict = f"**Recommendation: {rec.verdict}**"
if rec.verdict == "PASS":
    st.error(f"{verdict}. Not met: {'; '.join(rec.failed)}.", icon=":material/block:")
else:
    st.success(f"{verdict}. Every investment criterion is met.",
               icon=":material/check_circle:")

k = st.columns(5)
k[0].metric("DCF: Gordon Growth", f"${dcf.gordon.implied_share_price:,.2f}",
            f"{dcf.gordon.premium_to_current:+.1%} vs ${price:,.2f}", border=True)
k[1].metric("DCF: Exit Multiple", f"${dcf.exit.implied_share_price:,.2f}",
            f"{dcf.exit.premium_to_current:+.1%} vs ${price:,.2f}", border=True)
k[2].metric("LBO offer price", f"${lbo.entry['offer_price_per_share']:,.2f}",
            f"{lbo.entry['offer_premium']:+.1%} vs ${price:,.2f}", delta_color="off",
            border=True)
k[3].metric("Sponsor IRR", f"{lbo.irr:.1%}",
            f"{lbo.irr - analysis.criteria.target_irr:+.1%} vs hurdle", border=True)
k[4].metric("Sponsor MOIC", f"{lbo.moic:.2f}x",
            f"{lbo.moic - analysis.criteria.minimum_moic:+.2f}x vs minimum", border=True)

tab_overview, tab_dcf, tab_lbo, tab_scen, tab_memo, tab_files = st.tabs(
    ["Overview", "DCF", "LBO", "Scenarios", "IC memo", "Downloads"])

# --------------------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------------------
with tab_overview:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Net sales and EBITDA ($B)")
        st.altair_chart(projection_chart(model), width="stretch")
    with right:
        st.subheader("Historical metrics (10-K)")
        hist = model.schema.historical_metrics().loc[
            ["Net sales", "Revenue growth", "EBITDA", "EBITDA margin %", "Capex",
             "Unlevered FCF"]]
        shown = hist.apply(lambda row: row.map(
            (lambda v: f"{v:.1%}") if ("%" in row.name or "growth" in row.name)
            else (lambda v: f"${v / 1_000:,.1f}B")), axis=1).replace("nan%", "")
        st.dataframe(shown.rename(columns=lambda y: f"FY{y}"), width="stretch")
        st.caption("Every accounting identity (A = L + E, the cash roll-forward, net income "
                   "ties) is verified on extraction and again for every projected year.")
    s = model.schema
    if s.foreign_cash_and_investments:
        st.subheader("What the filing says about the cash")
        c = st.columns(3)
        c[0].metric("Cash & securities",
                    f"${s.balance_sheet(s.latest_year).cash_and_investments / 1e3:,.1f}B")
        c[1].metric("Held offshore", f"${s.foreign_cash_and_investments / 1e3:,.1f}B")
        c[2].metric("Implied repatriation tax", f"{s.implied_repatriation_tax_rate or 0:.1%}",
                    help="Unrecognized deferred tax liability / undistributed foreign "
                         "earnings, both disclosed in the 10-K.")
    if s.derivation_notes:
        with st.expander(f"Data notes: {len(s.derivation_notes)} lines derived rather than "
                         "reported", icon=":material/info:"):
            st.markdown("\n".join(f"- {n}" for n in s.derivation_notes))

# --------------------------------------------------------------------------------------
# DCF
# --------------------------------------------------------------------------------------
with tab_dcf:
    w = dcf.wacc
    left, right = st.columns(2)
    with left:
        st.subheader("Cost of capital")
        st.dataframe({
            "Component": ["Cost of equity (CAPM)", "After-tax cost of debt",
                          "Equity / capital", "Debt / capital", "WACC"],
            "Value": [f"{w.cost_of_equity:.2%}", f"{w.after_tax_cost_of_debt:.2%}",
                      f"{w.equity_weight:.1%}", f"{w.debt_weight:.1%}", f"{w.wacc:.2%}"],
        }, hide_index=True, width="stretch")
    with right:
        st.subheader("Terminal value methods")
        st.dataframe({
            "": ["Enterprise value", "Equity value", "Implied share price",
                 "Terminal value, % of EV", "Cross-check"],
            "Gordon Growth": [f"${dcf.gordon.enterprise_value / 1e3:,.1f}B",
                              f"${dcf.gordon.equity_value / 1e3:,.1f}B",
                              f"${dcf.gordon.implied_share_price:,.2f}",
                              f"{dcf.gordon.pv_terminal_share_of_ev:.1%}",
                              f"{dcf.gordon.implied_cross_check:.1f}x implied exit"],
            "Exit Multiple": [f"${dcf.exit.enterprise_value / 1e3:,.1f}B",
                              f"${dcf.exit.equity_value / 1e3:,.1f}B",
                              f"${dcf.exit.implied_share_price:,.2f}",
                              f"{dcf.exit.pv_terminal_share_of_ev:.1%}",
                              f"{dcf.exit.implied_cross_check:.2%} implied growth"],
        }, hide_index=True, width="stretch")
    left, right = st.columns(2)
    left.altair_chart(heatmap(analysis.sensitivities.dcf_wacc_growth), width="stretch")
    right.altair_chart(heatmap(analysis.sensitivities.dcf_multiple_wacc), width="stretch")

# --------------------------------------------------------------------------------------
# LBO
# --------------------------------------------------------------------------------------
with tab_lbo:
    uses, sources = sources_uses_frame(lbo)
    money = {"$B": st.column_config.NumberColumn(format="$%.1f"),
             "% of total": st.column_config.NumberColumn(format="%.1f%%")}
    left, right = st.columns(2)
    left.subheader("Uses of funds")
    left.dataframe(uses, hide_index=True, column_config=money, width="stretch")
    right.subheader("Sources of funds")
    right.dataframe(sources, hide_index=True, column_config=money, width="stretch")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Debt paydown")
        st.altair_chart(debt_paydown_chart(lbo), width="stretch")
    with right:
        st.subheader("Break-evens for the hurdle IRR")
        r = analysis.risk
        st.metric("Exit multiple needed", f"{r.breakeven_exit_multiple:.1f}x"
                  if r.breakeven_exit_multiple else "Not reachable")
        st.metric("Maximum entry multiple", f"{r.max_entry_multiple_for_hurdle:.1f}x"
                  if r.max_entry_multiple_for_hurdle else "Not reachable")
        st.metric("Sponsor equity, % of uses", f"{r.sponsor_equity_pct_of_uses:.1%}")

    st.subheader("Credit statistics and covenant headroom")
    st.dataframe(covenant_frame(lbo), width="stretch", column_config={
        "Total debt ($B)": st.column_config.NumberColumn(format="$%.1f"),
        "Debt / EBITDA": st.column_config.NumberColumn(format="%.2fx"),
        "EBITDA / interest": st.column_config.NumberColumn(format="%.2fx"),
        "Cushion: leverage covenant": st.column_config.NumberColumn(format="%.1f%%"),
        "Cushion: coverage covenant": st.column_config.NumberColumn(format="%.1f%%"),
    })
    st.caption("Cushion = how far EBITDA can fall before the covenant is breached "
               f"(max {model.assumptions.transaction.max_leverage_covenant:.1f}x leverage, "
               f"min {model.assumptions.transaction.min_interest_coverage_covenant:.1f}x "
               "interest coverage).")
    left, right = st.columns(2)
    left.altair_chart(heatmap(analysis.sensitivities.lbo_irr), width="stretch")
    right.altair_chart(heatmap(analysis.sensitivities.lbo_moic), width="stretch")

# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------
with tab_scen:
    st.subheader("Base / Bull / Bear")
    st.dataframe(scenario_frame(analysis), width="stretch")
    for res in analysis.scenarios:
        st.markdown(f"**{res.name}:** {res.description}")
    st.subheader(f"Investment criteria: {rec.verdict}")
    for desc, ok in rec.tests:
        st.markdown(f"{':material/check_circle:' if ok else ':material/cancel:'} {desc}")

# --------------------------------------------------------------------------------------
# IC memo
# --------------------------------------------------------------------------------------
claude_memo = st.session_state.get("claude_memo")
if claude_memo and claude_memo["run_key"] == run_key:
    memo_md, memo_label = claude_memo["markdown"], "Memo drafted by Claude for these assumptions"
elif mode == SAMPLE and is_base_case and SAMPLE_MEMO.is_file():
    memo_md, memo_label = SAMPLE_MEMO.read_text(encoding="utf-8"), "Sample memo (base case)"
else:
    memo_md, memo_label = template_memo(*run_key), "Template memo for these assumptions"

with tab_memo:
    with st.expander("Generate a new memo with Claude, using your own Anthropic API key",
                     icon=":material/edit_note:"):
        st.markdown(
            "Claude (`claude-opus-5-5`) drafts the narrative in two steps, a structured "
            "risk assessment and then the memo. Every table is still generated by the "
            "engine, and the memo is rejected if it contradicts the recommendation.\n\n"
            "- **Your key is used only for this request.** It is sent to Anthropic's API, "
            "held in memory for your browser session, and never stored or logged. This "
            "app has no API key of its own.\n"
            "- **Cost:** two API calls billed to your account at Claude Opus 5.5 rates "
            "($4 per million input tokens, $20 per million output tokens; the second call "
            "reads the deal data from the prompt cache). Generation takes a minute or two.")
        api_key = st.text_input("Anthropic API key", type="password", key="api_key",
                                placeholder="sk-ant-...")
        if st.button("Generate memo with Claude", type="primary", key="generate_memo",
                     disabled=not api_key.strip()):
            with st.status("Claude is drafting the memo...", expanded=True) as status:
                try:
                    st.write("Step 1: structured risk assessment. Step 2: memo narrative.")
                    client = anthropic.Anthropic(api_key=api_key.strip(), max_retries=2)
                    memo = memo_generator.generate_memo(
                        analysis, client=client, allow_template_fallback=False)
                except memo_generator.MemoGenerationError as exc:
                    status.update(label="Memo not generated", state="error")
                    st.error(str(exc))
                else:
                    st.session_state["claude_memo"] = {"markdown": memo.markdown,
                                                       "run_key": run_key}
                    status.update(label="Memo generated", state="complete")
                    st.rerun()

    st.caption(memo_label)
    st.download_button("Download memo (.md)", memo_md, file_name="ic_memo.md",
                       mime="text/markdown", icon=":material/download:", key="memo_download")
    with st.container(border=True):
        st.markdown(memo_md)

# --------------------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------------------
with tab_files:
    st.subheader("Model files for the assumptions shown")
    st.markdown(
        "- **Excel model:** 11 sheets, built from live formulas. The Checks sheet's master "
        "check reconciles every balance and cash tie.\n"
        "- **IC memo:** the memo currently shown on the IC memo tab.\n"
        "- **Data pack:** the JSON the memo is written from.")
    c = st.columns(3)
    c[0].download_button("Excel model (.xlsx)", build_workbook(*run_key), file_name="model.xlsx",
                         mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         icon=":material/table:", key="xlsx_download")
    c[1].download_button("IC memo (.md)", memo_md, file_name="ic_memo.md",
                         mime="text/markdown", icon=":material/description:",
                         key="memo_download_2")
    c[2].download_button("Data pack (.json)", json.dumps(analysis_context(analysis), indent=2),
                         file_name="analysis.json", mime="application/json",
                         icon=":material/data_object:", key="json_download")
