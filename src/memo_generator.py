"""Investment Committee memo generator.

Prompt chain on Claude (``claude-opus-5-5``):

1. **Risk assessment**: the full deal data pack goes in; a structured
   :class:`RiskAssessment` comes back (Pydantic-validated structured output).
2. **Memo narrative**: the data pack plus the step-1 assessment go in; Markdown prose
   comes back, with ``{{TABLE_...}}`` placeholders where tables belong.

Division of labour: **numbers come from the engine, prose from Claude.** Every table
is rendered by Python from the model outputs and substituted into the placeholders,
so no figure in a table can be mis-transcribed. The Proceed / Pass verdict is decided
by :func:`src.analyzer.recommend`; the memo is rejected if Claude's text omits a
required section or contradicts that verdict.

Both requests share a cached system prefix (instructions + data pack), so the second
call reads the data pack from the prompt cache. Server-side refusal fallbacks are
enabled (``fallbacks="default"``).

If no Anthropic credentials are available (or ``offline=True``), the memo is written
from a deterministic template with the same structure and tables.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.analyzer import AnalysisResult, SensitivityTable, analysis_context, deal_framing
from src.exceptions import FinancialDataError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SECTIONS: tuple[str, ...] = (
    "## 1. Executive Summary & Deal Thesis",
    "## 2. Transaction Overview",
    "## 3. Valuation & Returns Summary",
    "## 4. Key Operational Risks",
    "## 5. Downside Sensitivities",
    "## 6. Covenant Headroom Analysis",
    "## 7. Final Recommendation",
)

PLACEHOLDERS: dict[str, str] = {
    # placeholder: section it must appear in
    "{{TABLE_SOURCES_USES}}": SECTIONS[1],
    "{{TABLE_SCENARIOS}}": SECTIONS[2],
    "{{TABLE_DCF_GORDON}}": SECTIONS[2],
    "{{TABLE_DCF_EXIT}}": SECTIONS[2],
    "{{TABLE_LBO_IRR}}": SECTIONS[4],
    "{{TABLE_LBO_MOIC}}": SECTIONS[4],
    "{{TABLE_COVENANTS}}": SECTIONS[5],
    "{{TABLE_CRITERIA}}": SECTIONS[6],
}


class MemoGenerationError(FinancialDataError):
    """Raised when Claude's output is refused, truncated, or fails validation."""


# --------------------------------------------------------------------------------------
# Structured output of step 1
# --------------------------------------------------------------------------------------
class Risk(BaseModel):
    """One key risk, quantified from the model outputs."""

    model_config = ConfigDict(extra="forbid")

    title: str
    category: Literal["operational", "financial", "structural", "market", "tax_regulatory"]
    severity: Literal["high", "medium", "low"]
    description: str
    quantification: str
    mitigant: str


class RiskAssessment(BaseModel):
    """Step-1 output: the analytical backbone of the memo."""

    model_config = ConfigDict(extra="forbid")

    deal_thesis: list[str]
    key_risks: list[Risk]
    downside_commentary: str
    covenant_assessment: str
    recommendation_rationale: str


@dataclass(frozen=True)
class Memo:
    """A generated memo.

    Attributes:
        markdown: The complete memo.
        source: ``"claude-opus-5-5"`` (or another model id) or ``"template"``.
        risk_assessment: Step-1 output when Claude was used.
        usage: Token usage per step when Claude was used.
    """

    markdown: str
    source: str
    risk_assessment: RiskAssessment | None = None
    usage: dict[str, dict[str, int]] | None = None


# --------------------------------------------------------------------------------------
# Formatting & tables (always rendered by Python)
# --------------------------------------------------------------------------------------
def fmt_money(m: float | None) -> str:
    """USD millions -> ``$660.0B`` / ``$850M``."""
    if m is None or not math.isfinite(m):
        return "n.m."
    sign = "-" if m < 0 else ""
    m = abs(m)
    return f"{sign}${m / 1_000:,.1f}B" if m >= 1_000 else f"{sign}${m:,.0f}M"


def fmt_pct(x: float | None, digits: int = 1) -> str:
    """Decimal -> percentage string."""
    return "n.m." if x is None or not math.isfinite(x) else f"{x:.{digits}%}"


def fmt_mult(x: float | None) -> str:
    """Multiple -> ``8.0x``."""
    return "n.m." if x is None or not math.isfinite(x) else f"{x:.1f}x"


def fmt_price(x: float | None) -> str:
    """Per-share value -> ``$142.59``."""
    return "n.m." if x is None or not math.isfinite(x) else f"${x:,.2f}"


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
             align: str | None = None) -> str:
    """Render a Markdown table; ``align`` is one char per column (``l``/``r``)."""
    align = align or "l" + "r" * (len(headers) - 1)
    rule = ["---:" if a == "r" else ":---" for a in align]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(rule) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


_FORMATTERS = {"price": fmt_price, "pct": fmt_pct, "multiple": lambda v: f"{v:.2f}x"
               if math.isfinite(v) else "n.m."}
_AXIS_FORMATTERS = {"pct": lambda v: fmt_pct(v, 1), "multiple": fmt_mult}


def sensitivity_markdown(table: SensitivityTable) -> str:
    """Sensitivity grid as Markdown; the base-case cell is bold."""
    cell, row_fmt, col_fmt = (_FORMATTERS[table.value_format],
                              _AXIS_FORMATTERS[table.axis_formats[0]],
                              _AXIS_FORMATTERS[table.axis_formats[1]])
    headers = [f"{table.row_label} \\ {table.col_label}", *(col_fmt(c) for c in table.cols)]
    rows = []
    for r in table.rows:
        cells = []
        for c in table.cols:
            text = cell(float(table.values.at[r, c]))
            if math.isclose(r, table.base[0]) and math.isclose(c, table.base[1]):
                text = f"**{text}**"
            cells.append(text)
        rows.append([f"**{row_fmt(r)}**", *cells])
    return f"*{table.title}* (base case in bold)\n\n" + md_table(headers, rows)


def render_tables(analysis: AnalysisResult) -> dict[str, str]:
    """Every table in the memo, keyed by placeholder."""
    model = analysis.base.model
    lbo, sens, tx = model.lbo, analysis.sensitivities, model.assumptions.transaction
    total_uses = lbo.entry["total_uses"]

    su_rows = [[name, fmt_money(v), fmt_pct(v / total_uses)] for name, v in lbo.uses.items()]
    su_rows.append(["**Total uses**", f"**{fmt_money(total_uses)}**", "**100.0%**"])
    src_rows = [[name, fmt_money(v), fmt_pct(v / total_uses)] for name, v in lbo.sources.items()]
    src_rows.append(["**Total sources**", f"**{fmt_money(sum(lbo.sources.values()))}**",
                     "**100.0%**"])
    entry = lbo.entry
    sources_uses = (
        md_table(["Uses of funds", "Amount", "% of total"], su_rows) + "\n\n"
        + md_table(["Sources of funds", "Amount", "% of total"], src_rows) + "\n\n"
        + md_table(["Entry valuation", "Value"], [
            ["LTM EBITDA", fmt_money(entry["ltm_ebitda"])],
            ["Entry multiple", fmt_mult(entry["entry_multiple"])],
            ["Enterprise value", fmt_money(entry["entry_ev"])],
            ["Implied offer price per share", fmt_price(entry["offer_price_per_share"])],
            ["Premium to current price", fmt_pct(entry["offer_premium"])],
            ["Total debt / LTM EBITDA", fmt_mult(entry["debt_to_ebitda"])],
            ["Offshore cash left trapped at close", fmt_money(entry["trapped_cash"])],
        ]))

    metrics = [
        ("Revenue CAGR (5y)", "revenue_cagr", fmt_pct),
        ("Final-year EBITDA", "final_year_ebitda", fmt_money),
        ("Final-year EBITDA margin", "final_year_ebitda_margin", fmt_pct),
        ("Cumulative unlevered FCF", "cumulative_ufcf", fmt_money),
        ("DCF share price: Gordon Growth", "dcf_gordon_price", fmt_price),
        ("DCF share price: Exit Multiple", "dcf_exit_price", fmt_price),
        ("Sponsor equity", "lbo_sponsor_equity", fmt_money),
        ("Exit equity value", "lbo_exit_equity", fmt_money),
        ("Sponsor MOIC", "lbo_moic", lambda v: f"{v:.2f}x"),
        ("Sponsor IRR", "lbo_irr", fmt_pct),
        ("Peak total debt / EBITDA", "peak_leverage", fmt_mult),
        ("Acquisition debt repaid by exit", "debt_repaid_pct", fmt_pct),
    ]
    scen = analysis.scenarios
    scenarios = md_table(["Metric", *(r.name for r in scen)],
                         [[label, *(f(r.metrics[key]) for r in scen)]
                          for label, key, f in metrics])
    scenarios += "\n\n" + "\n".join(f"- **{r.name}:** {r.description}" for r in scen)

    sched = lbo.schedule
    cov_rows = []
    for y in sched.columns:
        c = sched[y]
        cov_rows.append([f"FY{y}", fmt_money(c["ebitda"]), fmt_money(c["total_debt"]),
                         fmt_mult(c["leverage"]), fmt_pct(c["leverage_cushion"], 0),
                         fmt_mult(c["interest_coverage"]), fmt_pct(c["coverage_cushion"], 0)])
    covenants = md_table(
        ["Year", "EBITDA", "Total debt", f"Debt/EBITDA (max {tx.max_leverage_covenant:.1f}x)",
         "EBITDA cushion", f"EBITDA/interest (min {tx.min_interest_coverage_covenant:.1f}x)",
         "EBITDA cushion"], cov_rows)

    criteria = md_table(["Investment criterion", "Result"],
                        [[desc, "Met" if ok else "**Not met**"]
                         for desc, ok in analysis.recommendation.tests])
    return {
        "{{TABLE_SOURCES_USES}}": sources_uses,
        "{{TABLE_SCENARIOS}}": scenarios,
        "{{TABLE_DCF_GORDON}}": sensitivity_markdown(sens.dcf_wacc_growth),
        "{{TABLE_DCF_EXIT}}": sensitivity_markdown(sens.dcf_multiple_wacc),
        "{{TABLE_LBO_IRR}}": sensitivity_markdown(sens.lbo_irr),
        "{{TABLE_LBO_MOIC}}": sensitivity_markdown(sens.lbo_moic),
        "{{TABLE_COVENANTS}}": covenants,
        "{{TABLE_CRITERIA}}": criteria,
    }


def header_block(analysis: AnalysisResult, source: str) -> str:
    """Memo title and metadata (always rendered by Python)."""
    s = analysis.base.model.schema
    narrative = ("deterministic template (offline)" if source == "template"
                 else f"drafted by Claude (`{source}`) from the model outputs")
    source_label = ("10-K XBRL data (SEC EDGAR)" if s.data_source == "xbrl"
                    else s.metadata.filing_type)
    return (
        f"# Investment Committee Memorandum: {s.metadata.name}\n\n"
        f"**Take-private analysis | Recommendation: {analysis.recommendation.verdict}**\n\n"
        + md_table(["Item", "Detail"], [
            ["Date", date.today().strftime("%d %B %Y")],
            ["Source data", f"{source_label}, fiscal year ended "
                            f"{s.metadata.fiscal_year_end:%d %B %Y}"
                            + (f" (CIK {s.metadata.cik})" if s.metadata.cik else "")],
            ["Model", "model.xlsx (3-statement, DCF, LBO)"],
            ["Narrative", narrative],
            ["Figures", "USD; every table is generated by the valuation engine"],
        ], align="ll")
        + f"\n\n> **Note:** an analytical exercise built from public filings. "
          f"{deal_framing(analysis.base.model.lbo.entry['entry_ev'])}\n"
    )


def appendix_block(analysis: AnalysisResult) -> str:
    """Appendix: key assumptions and their sources."""
    a = analysis.base.model.assumptions
    m, op, tx = a.market, a.operating, a.transaction
    rows = [
        ["Risk-free rate", fmt_pct(m.risk_free_rate, 2), m.sources.get("risk_free_rate", "")],
        ["Equity risk premium", fmt_pct(m.equity_risk_premium, 2),
         m.sources.get("equity_risk_premium", "")],
        ["Levered beta", f"{m.levered_beta:.2f}", m.sources.get("levered_beta", "")],
        ["Terminal growth", fmt_pct(m.terminal_growth_rate),
         m.sources.get("terminal_growth_rate", "")],
        ["Revenue growth (FY+1 to FY+5)", ", ".join(fmt_pct(g) for g in op.revenue_growth),
         op.sources.get("revenue_growth", "")],
        ["Gross margin", ", ".join(fmt_pct(g) for g in op.gross_margin),
         op.sources.get("gross_margin", "")],
        ["Entry / exit multiple", f"{fmt_mult(tx.entry_ev_ebitda_multiple)} / "
                                  f"{fmt_mult(tx.exit_ev_ebitda_multiple)}",
         tx.sources.get("entry_ev_ebitda_multiple", "")],
        ["Debt, % of entry EV", fmt_pct(tx.debt_pct_of_entry_ev),
         tx.sources.get("debt_pct_of_entry_ev", "")],
    ]
    return ("\n---\n\n## Appendix: Key Assumptions\n\n"
            + md_table(["Assumption", "Base case", "Source / rationale"], rows, align="lrl")
            + "\n\nInputs marked VERIFY in `config/assumptions.json` are approximations "
            "pending confirmation against market data.\n")


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
def validate_memo_body(body: str, verdict: str) -> list[str]:
    """Check Claude's narrative before tables are substituted.

    Returns:
        A list of problems (empty if the body is valid).
    """
    problems = []
    positions = [body.find(s) for s in SECTIONS]
    for section, pos in zip(SECTIONS, positions, strict=True):
        if pos < 0:
            problems.append(f"missing section heading '{section}'")
    found = [p for p in positions if p >= 0]
    if found != sorted(found):
        problems.append("section headings are out of order")
    for placeholder, section in PLACEHOLDERS.items():
        count = body.count(placeholder)
        if count != 1:
            problems.append(f"placeholder {placeholder} appears {count} times (expected 1)")
            continue
        start = body.find(section)
        nxt = [p for p in positions if p > start]
        end = min(nxt) if nxt else len(body)
        if start >= 0 and not start < body.find(placeholder) < end:
            problems.append(f"placeholder {placeholder} is outside '{section}'")
    final = body[body.find(SECTIONS[-1]):] if SECTIONS[-1] in body else ""
    opposite = "PROCEED" if verdict == "PASS" else "PASS"
    if f"**Recommendation: {verdict}**" not in final:
        problems.append(f"final section lacks the line '**Recommendation: {verdict}**'")
    if f"Recommendation: {opposite}" in body:
        problems.append(f"memo states 'Recommendation: {opposite}', contradicting the model")
    return problems


def assemble(analysis: AnalysisResult, body: str, source: str) -> str:
    """Header + body with tables substituted + appendix."""
    for placeholder, table in render_tables(analysis).items():
        body = body.replace(placeholder, table)
    return header_block(analysis, source) + "\n" + body.strip() + "\n" + appendix_block(analysis)


# --------------------------------------------------------------------------------------
# Template memo (offline)
# --------------------------------------------------------------------------------------
def template_body(analysis: AnalysisResult) -> str:
    """Rule-based narrative with the same structure as the Claude memo."""
    model = analysis.base.model
    s, d, lbo, risk = model.schema, model.dcf, model.lbo, analysis.risk
    rec, crit = analysis.recommendation, analysis.criteria
    m = analysis.base.metrics
    by_name = {r.name: r for r in analysis.scenarios}
    entry = lbo.entry
    y0 = model.projection.base_year
    bull, bear = by_name.get("Bull"), by_name.get("Bear")
    sens_irr = analysis.sensitivities.lbo_irr.values
    best_irr = float(sens_irr.max().max())

    def scen(name: str, key: str, f: Any) -> str:
        return f(by_name[name].metrics[key]) if name in by_name else "n.a."

    breakeven = (fmt_mult(risk.breakeven_exit_multiple) if risk.breakeven_exit_multiple
                 else "above 40x")
    max_entry = (fmt_mult(risk.max_entry_multiple_for_hurdle)
                 if risk.max_entry_multiple_for_hurdle else "none in range")

    # Largest product line (falls back to the largest segment of any type).
    sales0 = s.income_statement(y0).net_sales
    segments = ([x for x in s.revenue_segments if x.segment_type == "product"]
                or list(s.revenue_segments))
    top = max(segments, key=lambda x: x.net_sales.get(y0, 0.0), default=None)
    bear_ebitda_hit = (fmt_money(-analysis.variance.at["final_year_ebitda", "Bear vs Base"])
                       if bear else "n.a.")
    demand_risk = (
        f"**Product concentration (high):** {top.name} was "
        f"{fmt_pct(top.net_sales.get(y0, 0.0) / sales0)} of FY{y0} net sales."
        if top else "**Demand (high):** revenue growth is the main driver of exit value.")
    demand_risk += (f" The bear case cuts final-year EBITDA by {bear_ebitda_hit} and IRR to "
                    f"{scen('Bear', 'lbo_irr', fmt_pct)}.")
    offshore = s.foreign_cash_and_investments or 0.0
    big_deal = entry["entry_ev"] > 100_000

    # Entry multiples at which more leverage lowers IRR (negative leverage).
    negative = [e for e in sens_irr.index
                if float(sens_irr.loc[e].iloc[-1]) < float(sens_irr.loc[e].iloc[0])]
    leverage_note = (
        f"At entry multiples of {fmt_mult(min(negative))} and above, additional leverage "
        "reduces returns: the cost of acquisition debt exceeds the unlevered return on "
        "the purchase price." if negative else
        "Within the grid, additional leverage increases returns at every entry multiple.")
    bull_repatriates = bool(bull and bull.model.assumptions.transaction.foreign_cash_usable_pct)
    trapped_note = (
        f"The bull case, which repatriates offshore cash at close, reaches "
        f"{scen('Bull', 'lbo_irr', fmt_pct)} IRR." if bull_repatriates else
        "No scenario repatriates offshore cash at close.")
    margin_hit = (f"{-100 * analysis.variance.at['final_year_ebitda_margin', 'Bear vs Base']:.1f}"
                  " percentage points" if bear else "n.a.")
    failed = [desc[0].lower() + desc[1:] for desc in rec.failed]
    paths = [f"entry at or below {max_entry}" if risk.max_entry_multiple_for_hurdle else None,
             f"an exit at {breakeven} or higher" if risk.breakeven_exit_multiple else None,
             (f"the bull-case conditions ({scen('Bull', 'lbo_irr', fmt_pct)} IRR)"
              if bull and by_name['Bull'].metrics['lbo_irr'] >= crit.target_irr else None)]
    change_note = ", or ".join(p for p in paths if p)
    decision = (
        f"The transaction does not meet {len(failed)} of {len(rec.tests)} investment "
        f"criteria: {'; '.join(failed)}. Holding other assumptions at the base case, the "
        f"hurdle would require {change_note}." if failed
        else "The transaction meets every investment criterion.")
    breach = risk.covenant_breach
    covenant_note = (
        "Credit risk is not the binding constraint; returns are." if not breach else
        "The base case breaches a maintenance covenant, so the structure must be re-cut.")

    thesis = [
        f"**Cash generation and deleveraging:** {fmt_pct(m['debt_repaid_pct'])} of acquisition "
        f"debt is repaid by exit, from {fmt_mult(entry['debt_to_ebitda'])} EBITDA at close.",
        f"**Equity cheque:** {fmt_money(lbo.sources['Sponsor equity'])} of sponsor equity "
        f"({fmt_pct(risk.sponsor_equity_pct_of_uses)} of uses) with debt sized at "
        f"{fmt_pct(model.assumptions.transaction.debt_pct_of_entry_ev, 0)} of EV"
        + (", a scale no sponsor has written." if big_deal else "."),
    ]
    if offshore:
        thesis.append(
            f"**Offshore cash:** {fmt_money(offshore)} is held offshore and would bear a "
            f"{fmt_pct(risk.repatriation_tax_rate)} repatriation tax, so it cannot fund the "
            "purchase price efficiently.")
    risks = [
        demand_risk,
        f"**Margin pressure (medium):** gross margin compression flows directly to cash "
        f"available for debt service; in the bear case the final-year EBITDA margin is "
        f"{margin_hit} lower than in the base case.",
    ]
    if offshore:
        risks.append(f"**Trapped cash and tax (high):** {fmt_money(risk.trapped_cash)} of "
                     f"offshore cash stays trapped at close in the base case. {trapped_note}")
    risks.append(f"**Exit multiple (high):** the hurdle requires an exit at {breakeven} "
                 f"EBITDA, versus entry at {fmt_mult(entry['entry_multiple'])}.")
    if s.derivation_notes:
        risks.append("**Data (low):** some lines are derived rather than reported in the "
                     "filing; see the data notes on the workbook's cover sheet.")
    thesis_md = "\n".join(f"- {t}" for t in thesis)
    risks_md = "\n".join(f"{i}. {r}" for i, r in enumerate(risks, start=1))
    return f"""
{SECTIONS[0]}

We evaluated a hypothetical take-private of {s.metadata.name} at {fmt_mult(entry['entry_multiple'])} FY{y0} EBITDA of {fmt_money(entry['ltm_ebitda'])}, an enterprise value of {fmt_money(entry['entry_ev'])} and an implied offer of {fmt_price(entry['offer_price_per_share'])} per share ({fmt_pct(entry['offer_premium'])} premium). The projection reaches {fmt_pct(m['final_year_ebitda_margin'])} EBITDA margins in the final year and {fmt_money(m['cumulative_ufcf'])} of cumulative unlevered free cash flow over five years. The base-case return is {fmt_pct(m['lbo_irr'])} IRR and {m['lbo_moic']:.2f}x MOIC, against a {fmt_pct(crit.target_irr, 0)} hurdle.

{thesis_md}

{SECTIONS[1]}

{{{{TABLE_SOURCES_USES}}}}

{SECTIONS[2]}

The public-market DCF values the shares at {fmt_price(d.gordon.implied_share_price)} (Gordon Growth, WACC {fmt_pct(d.wacc.wacc, 2)}, g {fmt_pct(d.terminal_growth_rate)}) and {fmt_price(d.exit.implied_share_price)} (exit multiple of {fmt_mult(d.exit_multiple)}). Terminal value accounts for {fmt_pct(d.gordon.pv_terminal_share_of_ev)} and {fmt_pct(d.exit.pv_terminal_share_of_ev)} of enterprise value respectively. The LBO returns {scen('Base', 'lbo_irr', fmt_pct)} in the base case, {scen('Bull', 'lbo_irr', fmt_pct)} in the bull case and {scen('Bear', 'lbo_irr', fmt_pct)} in the bear case.

{{{{TABLE_SCENARIOS}}}}

{{{{TABLE_DCF_GORDON}}}}

{{{{TABLE_DCF_EXIT}}}}

{SECTIONS[3]}

{risks_md}

{SECTIONS[4]}

Across the entry-multiple and leverage grid, the best outcome is {fmt_pct(best_irr)} IRR. {leverage_note} The maximum entry multiple that clears the hurdle is {max_entry}.

{{{{TABLE_LBO_IRR}}}}

{{{{TABLE_LBO_MOIC}}}}

{SECTIONS[5]}

In the base case, the minimum EBITDA cushion is {fmt_pct(risk.min_leverage_cushion, 0)} to the leverage covenant and {fmt_pct(risk.min_coverage_cushion, 0)} to the interest coverage covenant. In the bear case the minimum leverage cushion is {scen('Bear', 'min_leverage_cushion', lambda v: fmt_pct(v, 0))}. {covenant_note}

{{{{TABLE_COVENANTS}}}}

{SECTIONS[6]}

**Recommendation: {rec.verdict}**

{decision}

{{{{TABLE_CRITERIA}}}}
"""


# --------------------------------------------------------------------------------------
# Claude prompt chain
# --------------------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a senior associate at a large-cap private equity fund, preparing \
material for the investment committee. You write with the precision of investment banking \
deal materials: specific, quantified, and candid about risks.

The deal data in <deal_data> comes from a validated valuation engine (3-statement model, \
DCF and LBO built from the company's 10-K). Treat it as the single source of truth: \
every figure you cite must appear in, or follow arithmetically from, that data. Amounts \
are USD millions unless stated; write large amounts in billions with one decimal \
(e.g. $540.0B). Rates and returns are decimals in the data; write them as percentages.

The recommendation is set by the fund's investment criteria and is recorded in \
recommendation.verdict. Your job is to explain it, not to overturn it."""


def risk_prompt() -> str:
    """Step-1 user prompt."""
    return (
        "Assess this transaction for the investment committee.\n\n"
        "- deal_thesis: three to five bullets on why a sponsor would look at this "
        "business, each grounded in a figure from the data.\n"
        "- key_risks: the four to six risks that matter most to returns or credit, "
        "most severe first. Quantify each one with the scenario, sensitivity or "
        "break-even data, and name a realistic mitigant.\n"
        "- downside_commentary: what the sensitivity grids and the bear case show, "
        "including where leverage stops helping returns.\n"
        "- covenant_assessment: headroom against both maintenance covenants in the "
        "base and bear cases.\n"
        "- recommendation_rationale: why the verdict follows from the criteria tests."
    )


def memo_prompt(assessment: RiskAssessment, verdict: str) -> str:
    """Step-2 user prompt."""
    sections = "\n".join(SECTIONS)
    placements = "\n".join(f"- {p} in '{s}'" for p, s in PLACEHOLDERS.items())
    return f"""Write the body of the investment committee memo in Markdown, using the \
risk assessment below as your analytical basis.

<risk_assessment>
{assessment.model_dump_json(indent=2)}
</risk_assessment>

Structure: use exactly these section headings, in this order, and no title (the title \
block is added separately):
{sections}

Tables are generated by the engine. Do not write tables yourself; instead put each \
placeholder below on its own line, exactly once, in the named section, where the table \
should appear:
{placements}

In the final section, include the line **Recommendation: {verdict}** exactly as written, \
then explain the decision and state what would have to change (price, structure, or tax \
regime) for the answer to be different. Aim for roughly 900-1,300 words of prose."""


def _check_stop(response: Any, step: str) -> None:
    """Raise on refusal or truncation before reading content."""
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise MemoGenerationError(f"{step}: Claude declined the request (category: {category})")
    if response.stop_reason == "max_tokens":
        raise MemoGenerationError(f"{step}: response truncated at max_tokens")


def _usage(response: Any) -> dict[str, int]:
    """Token usage of one response, including prompt-cache reads and writes."""
    u = response.usage
    return {k: int(getattr(u, k, 0) or 0) for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens")}


def _claude_memo(analysis: AnalysisResult, client: Any, model: str) -> Memo:
    """Run the two-step prompt chain and return a validated memo."""
    context = analysis_context(analysis)
    verdict = analysis.recommendation.verdict
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": f"<deal_data>\n{json.dumps(context, indent=1)}\n</deal_data>",
         "cache_control": {"type": "ephemeral"}},
    ]
    common = {"model": model, "betas": [FALLBACK_BETA], "fallbacks": "default",
              "output_config": {"effort": "high"}, "system": system}

    # Step 1: structured risk assessment
    step1 = client.beta.messages.parse(
        max_tokens=16000, output_format=RiskAssessment,
        messages=[{"role": "user", "content": risk_prompt()}], **common)
    _check_stop(step1, "Risk assessment")
    assessment = step1.parsed_output
    if assessment is None:
        raise MemoGenerationError("Risk assessment: no structured output returned")
    logger.info("Step 1 complete: %d risks identified", len(assessment.key_risks))

    # Step 2: memo narrative (streamed: long output)
    with client.beta.messages.stream(
            max_tokens=32000,
            messages=[{"role": "user", "content": memo_prompt(assessment, verdict)}],
            **common) as stream:
        step2 = stream.get_final_message()
    _check_stop(step2, "Memo narrative")
    body = "".join(b.text for b in step2.content if b.type == "text")
    problems = validate_memo_body(body, verdict)
    if problems:
        raise MemoGenerationError("Memo failed validation:\n  - " + "\n  - ".join(problems))

    usage = {"risk_assessment": _usage(step1), "memo": _usage(step2)}
    logger.info("Step 2 complete; cache read %d tokens",
                usage["memo"]["cache_read_input_tokens"])
    return Memo(markdown=assemble(analysis, body, model), source=model,
                risk_assessment=assessment, usage=usage)


def _is_missing_credentials(exc: Exception) -> bool:
    """The SDK raises ``TypeError`` at request time when no credential resolves."""
    return isinstance(exc, TypeError) and "authentication method" in str(exc)


def generate_memo(analysis: AnalysisResult, *, offline: bool = False,
                  model: str | None = None, client: Any = None,
                  allow_template_fallback: bool = True) -> Memo:
    """Generate the IC memo with Claude, or from the template.

    Args:
        analysis: Output of :func:`src.analyzer.run_analysis`.
        offline: Skip the API and use the deterministic template.
        model: Claude model id (default ``claude-opus-5-5``).
        client: An ``anthropic.Anthropic`` client (created if omitted).
        allow_template_fallback: When credentials are missing or rejected, or the API
            is unreachable, write the template memo (``True``, CLI behaviour) or raise
            (``False``, for callers such as the web app that supply a user's key).

    Returns:
        The memo.

    Raises:
        MemoGenerationError: If Claude refuses, truncates, or produces a memo that
            fails validation (these are content problems, so there is no silent
            fallback), or on credential / connection errors when
            ``allow_template_fallback`` is ``False``.
    """
    if offline:
        return Memo(markdown=assemble(analysis, template_body(analysis), "template"),
                    source="template")

    import anthropic

    client = client or anthropic.Anthropic()
    try:
        return _claude_memo(analysis, client, model or DEFAULT_MODEL)
    except (anthropic.AuthenticationError, anthropic.APIConnectionError, TypeError) as exc:
        if isinstance(exc, TypeError) and not _is_missing_credentials(exc):
            raise
        if not allow_template_fallback:
            if isinstance(exc, anthropic.AuthenticationError):
                reason = "the API key was rejected"
            elif isinstance(exc, anthropic.APIConnectionError):
                reason = "the Anthropic API could not be reached"
            else:
                reason = "no API key was provided"
            raise MemoGenerationError(f"Claude memo not generated: {reason}.") from exc
        logger.warning("Claude unavailable (%s); writing the template memo instead. "
                       "Set ANTHROPIC_API_KEY to generate the narrative with Claude.",
                       type(exc).__name__)
        return generate_memo(analysis, offline=True)
    except anthropic.APIStatusError as exc:
        raise MemoGenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
