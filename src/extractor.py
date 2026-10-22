"""SEC 10-K ingestion: unstructured filing -> validated ``FinancialStatementSchema``.

Pipeline
--------
1. **Load**: read a PDF (via ``pypdf``) or plain-text filing into raw text.
2. **Normalize**: unify dashes, quotes and whitespace; strip browser/print page
   furniture (headers, footers, "Table of Contents" links).
3. **Locate**: find each statement or note by its anchor heading (e.g.
   ``CONSOLIDATED BALANCE SHEETS`` followed by ``(In millions...``), skipping the
   table-of-contents entries that repeat the same titles.
4. **Parse rows**: split every line into a *label* and trailing *numeric columns*,
   handling ``$`` signs, thousands separators, ``(negatives)``, percentages and
   labels that wrap onto several lines.
5. **Map labels**: match each row's normalized label against a dictionary of regex
   synonyms (:class:`FieldSpec`) so that differently-worded filings map onto the same
   canonical schema fields.
6. **Validate**: build the Pydantic schema; every accounting identity is re-checked
   and any breach raises :class:`~src.exceptions.AccountingIdentityError`.

Required line items that cannot be found raise
:class:`~src.exceptions.MissingLineItemError`; optional ones are logged and default
to zero in the schema.

Usage::

    python -m src.extractor data/raw/apple_10k_fy2015.pdf --ticker AAPL
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.exceptions import (
    AmbiguousLineItemError,
    ExtractionError,
    FinancialDataError,
    MissingLineItemError,
    StatementNotFoundError,
)
from src.schemas import FinancialStatementSchema, InstrumentType

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# 1-2. Loading & normalization
# --------------------------------------------------------------------------------------
_CHAR_MAP = str.maketrans({
    "–": "-", "—": "-", "−": "-",      # en dash, em dash, minus sign
    "‘": "'", "’": "'",                       # curly single quotes
    "“": '"', "”": '"',                       # curly double quotes
    " ": " ", " ": " ", " ": " ",        # non-breaking / thin spaces
})

# Page furniture added when a filing is printed from a browser to PDF.
_NOISE_PATTERNS = [
    re.compile(r"^table of contents$", re.I),
    re.compile(r"^https?://\S+(\s+\d+/\d+)?$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}\s?[AP]M\b.*$"),
    re.compile(r"^.*\|\s*\d{4} Form 10-K\s*\|\s*\d+$"),
]


def load_document(path: str | Path) -> str:
    """Read a filing from disk as raw text.

    Args:
        path: Path to a ``.pdf``, ``.txt`` or ``.md`` file.

    Returns:
        The document's raw text, pages joined by newlines.

    Raises:
        ExtractionError: If the file is missing, unsupported, or yields no text.
    """
    path = Path(path)
    if not path.is_file():
        raise ExtractionError(f"Source document not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader  # imported lazily: only needed for PDFs

        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        logger.info("Loaded %s (%d pages)", path.name, len(reader.pages))
    elif suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8")
    else:
        raise ExtractionError(f"Unsupported document type '{suffix}' for {path}")

    if not text.strip():
        raise ExtractionError(f"No extractable text in {path} (scanned image PDF?)")
    return text


def normalize_lines(raw_text: str) -> list[str]:
    """Clean raw text into a list of non-empty, whitespace-collapsed lines.

    Args:
        raw_text: Text as returned by :func:`load_document`.

    Returns:
        Normalized lines with page furniture removed.
    """
    lines: list[str] = []
    for line in raw_text.translate(_CHAR_MAP).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and not any(p.match(line) for p in _NOISE_PATTERNS):
            lines.append(line)
    return lines


def normalize_label(label: str) -> str:
    """Canonicalize a line-item label for matching.

    Lower-cases, replaces punctuation with spaces and collapses whitespace, e.g.
    ``"Other income/(expense), net"`` -> ``"other income expense net"``.

    Args:
        label: Raw label text.

    Returns:
        Normalized label.
    """
    return re.sub(r"[^a-z0-9]+", " ", label.translate(_CHAR_MAP).lower()).strip()


# --------------------------------------------------------------------------------------
# 4. Row parsing
# --------------------------------------------------------------------------------------
_NUMBER_TOKEN = re.compile(
    r"^\$?(?P<open>\()?\$?(?P<num>-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?P<close>\))?(?P<pct>%)?$"
)
_YEAR_TOKEN = re.compile(r"^(19|20)\d{2}$")
_DASH_ZERO = {"-", "--"}


def pct_to_decimal(value: float | str) -> float:
    """Convert a percentage (``26.4`` or ``"26.4"``) to a decimal (``0.264``),
    rounded to remove binary floating-point noise."""
    return round(float(value) / 100.0, 10)


def parse_number(token: str) -> tuple[float, bool] | None:
    """Parse one numeric token from a financial table.

    Handles ``$1,234``, ``(1,234)`` (negative), ``26.4%`` and a bare dash (zero).

    Args:
        token: A single whitespace-delimited token.

    Returns:
        ``(value, is_percent)`` with percentages converted to decimals, or ``None``
        if the token is not numeric. Unbalanced parentheses are rejected.
    """
    if token in _DASH_ZERO:
        return 0.0, False
    m = _NUMBER_TOKEN.match(token)
    if not m or bool(m["open"]) != bool(m["close"]):
        return None
    value = float(m["num"].replace(",", ""))
    if m["open"]:
        value = -value
    if m["pct"]:
        return pct_to_decimal(value), True
    return value, False


@dataclass(frozen=True)
class ParsedRow:
    """A table row split into its label and trailing numeric columns.

    Attributes:
        label: Raw label, including any wrapped prefix lines.
        norm: Normalized label used for matching (see :func:`normalize_label`).
        values: Non-percentage numbers, left to right.
        percents: Percentage numbers as decimals, left to right.
        section: Most recent section header (e.g. ``"investing activities"``).
        raw: The original source line.
    """

    label: str
    norm: str
    values: list[float]
    percents: list[float]
    section: str | None
    raw: str


def split_line(line: str) -> tuple[str, list[str]]:
    """Split a line into ``(label, trailing numeric tokens)``.

    Walks tokens from the right, consuming numbers and stray ``$`` signs until the
    first non-numeric token, so numbers embedded in labels (``"allowances of $82"``)
    stay in the label.

    Args:
        line: A normalized line.

    Returns:
        The label text and the list of numeric tokens (possibly empty).
    """
    tokens = line.split(" ")
    i = len(tokens)
    while i > 0 and (tokens[i - 1] == "$" or parse_number(tokens[i - 1]) is not None):
        i -= 1
    numeric = [t for t in tokens[i:] if t != "$"]
    return " ".join(tokens[:i]), numeric


@dataclass
class ParsedStatement:
    """Rows parsed from one statement block, with its column-header years."""

    name: str
    years: list[int] = field(default_factory=list)
    rows: list[ParsedRow] = field(default_factory=list)


def parse_statement(
    name: str, lines: Sequence[str], section_pattern: str | None = None
) -> ParsedStatement:
    """Parse a block of statement lines into labelled rows.

    Rules:
      - Lines whose numeric tokens are all bare years (``2015``) before the first data
        row are column headers and define :attr:`ParsedStatement.years`.
      - Lines ending in ``:`` are section headers; a header that fully matches
        ``section_pattern`` becomes the ``section`` of subsequent rows.
      - Other lines without numbers are treated as the wrapped beginning of the next
        row's label.

    Args:
        name: Statement name, used in error messages.
        lines: Normalized lines of the block (excluding the title line).
        section_pattern: Regex of headers that define sections; ``None`` = any header.

    Returns:
        The parsed statement.
    """
    section_re = re.compile(section_pattern) if section_pattern else None
    out = ParsedStatement(name=name)
    pending: list[str] = []
    section: str | None = None

    for line in lines:
        label, tokens = split_line(line)
        if not tokens:
            if line.endswith(":"):
                header = normalize_label(line)
                if section_re is None or section_re.fullmatch(header):
                    section = header
                pending = []
            else:
                pending.append(line)
            continue

        if all(_YEAR_TOKEN.match(t) for t in tokens):
            if not out.rows:
                out.years.extend(int(t) for t in tokens)
            pending = []
            continue

        parsed = [parse_number(t) for t in tokens]
        full_label = " ".join([*pending, label]).strip()
        pending = []
        out.rows.append(ParsedRow(
            label=full_label,
            norm=normalize_label(full_label),
            values=[v for v, pct in parsed if not pct],
            percents=[v for v, pct in parsed if pct],
            section=section,
            raw=line,
        ))
    return out


# --------------------------------------------------------------------------------------
# 5. Label -> field mapping
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """How to find one schema field in a parsed statement.

    Attributes:
        field: Target schema field name.
        patterns: Regexes, any of which must *fully* match the normalized label.
        required: If ``True``, absence raises :class:`MissingLineItemError`.
        section: Optional regex that must fully match the row's section.
        scale: Multiplier applied to the values (e.g. ``1e-3`` thousands -> millions).
        absolute: Store the magnitude (for "Less: ..." rows shown in parentheses).
    """

    field: str
    patterns: tuple[str, ...]
    required: bool = True
    section: str | None = None
    scale: float = 1.0
    absolute: bool = False


def F(field_name: str, *patterns: str, **kwargs: Any) -> FieldSpec:
    """Shorthand constructor for :class:`FieldSpec`."""
    return FieldSpec(field_name, patterns, **kwargs)


def map_fields(
    statement: ParsedStatement, specs: Sequence[FieldSpec], n_periods: int
) -> dict[str, list[float]]:
    """Resolve each :class:`FieldSpec` to one row and return its period values.

    Args:
        statement: Parsed statement to search.
        specs: Fields to extract.
        n_periods: Number of period columns expected; the *last* ``n_periods``
            numbers on the row are used, which skips footnote markers or embedded
            dates that precede the data columns.

    Returns:
        Mapping of field name to values ordered like ``statement.years``. Optional
        fields that are not found are omitted (the schema default applies).

    Raises:
        MissingLineItemError: A required field has no matching row.
        AmbiguousLineItemError: More than one row matches a field.
        ExtractionError: A matching row has fewer than ``n_periods`` values.
    """
    result: dict[str, list[float]] = {}
    for spec in specs:
        compiled = [re.compile(p) for p in spec.patterns]
        matches = [
            r for r in statement.rows
            if any(c.fullmatch(r.norm) for c in compiled)
            and (spec.section is None or (r.section and re.fullmatch(spec.section, r.section)))
        ]
        if not matches:
            if spec.required:
                raise MissingLineItemError(statement.name, spec.field, list(spec.patterns))
            logger.warning("%s: optional field '%s' not found; defaulting to 0",
                           statement.name, spec.field)
            continue
        if len(matches) > 1:
            raise AmbiguousLineItemError(statement.name, spec.field, [r.label for r in matches])

        row = matches[0]
        if len(row.values) < n_periods:
            raise ExtractionError(
                f"{statement.name}: row '{row.label}' for '{spec.field}' has "
                f"{len(row.values)} value(s), expected {n_periods}. Source: {row.raw!r}"
            )
        values = [v * spec.scale for v in row.values[-n_periods:]]
        result[spec.field] = [abs(v) for v in values] if spec.absolute else values
    return result


def by_period(years: Sequence[int], fields: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Pivot ``{field: [v_y1, v_y2]}`` into ``[{fiscal_year: y1, field: v_y1}, ...]``."""
    return [
        {"fiscal_year": y, **{k: v[i] for k, v in fields.items()}}
        for i, y in enumerate(years)
    ]


# --------------------------------------------------------------------------------------
# Field dictionaries (label synonyms). Extend these to support other filers.
# --------------------------------------------------------------------------------------
INCOME_STATEMENT_FIELDS: tuple[FieldSpec, ...] = (
    F("net_sales", r"(total )?net sales", r"(total )?(net )?revenues?"),
    F("cost_of_sales", r"cost of (sales|revenues?|goods sold)"),
    F("gross_margin", r"gross (margin|profit)"),
    F("research_and_development", r"research and development", required=False),
    F("selling_general_administrative", r"selling general and administrative"),
    F("total_operating_expenses", r"total operating expenses"),
    F("operating_income", r"operating income( loss)?", r"income from operations"),
    F("other_income_expense", r"other income( expense)?( net)?", required=False),
    F("pretax_income", r"income before (provision for )?income taxes"),
    F("income_tax_expense", r"provision for income taxes", r"income tax expense"),
    F("net_income", r"net income"),
    F("eps_basic", r"basic", section=r"earnings per share", required=False),
    F("eps_diluted", r"diluted", section=r"earnings per share", required=False),
    F("basic_shares", r"basic", section=r"shares used .*", scale=1e-3, required=False),
    F("diluted_shares", r"diluted", section=r"shares used .*", scale=1e-3),
    F("dividends_per_share", r"cash dividends declared per share", required=False),
)

BALANCE_SHEET_FIELDS: tuple[FieldSpec, ...] = (
    F("cash_and_equivalents", r"cash and cash equivalents"),
    F("short_term_investments", r"short term (marketable securities|investments)",
      required=False),
    F("accounts_receivable", r"accounts receivable.*", required=False),
    F("inventories", r"inventor(y|ies)", required=False),
    F("deferred_tax_assets_current", r"deferred tax assets", required=False),
    F("vendor_non_trade_receivables", r"vendor non ?trade receivables", required=False),
    F("other_current_assets", r"other current assets", required=False),
    F("total_current_assets", r"total current assets"),
    F("long_term_investments", r"long term (marketable securities|investments)",
      required=False),
    F("ppe_net", r"property plant and equipment net", required=False),
    F("goodwill", r"goodwill", required=False),
    F("intangible_assets", r"(acquired )?intangible assets net", required=False),
    F("other_non_current_assets", r"other (non current )?assets", required=False),
    F("total_assets", r"total assets"),
    F("accounts_payable", r"accounts payable", required=False),
    F("accrued_expenses", r"accrued (expenses|liabilities)", required=False),
    F("deferred_revenue_current", r"deferred revenue( current)?", required=False),
    F("commercial_paper", r"commercial paper", required=False),
    F("current_portion_long_term_debt", r"current portion of (long term|term) debt",
      required=False),
    F("total_current_liabilities", r"total current liabilities"),
    F("deferred_revenue_non_current", r"deferred revenue non current", required=False),
    F("long_term_debt", r"long term debt", required=False),
    F("other_non_current_liabilities", r"other non current liabilities", required=False),
    F("total_liabilities", r"total liabilities"),
    F("common_stock_and_apic", r"common stock.*", required=False),
    F("retained_earnings", r"retained earnings", required=False),
    F("accumulated_other_comprehensive_income",
      r"accumulated other comprehensive (income|loss|income loss)", required=False),
    F("total_equity", r"total (shareholders|stockholders) equity"),
    F("total_liabilities_and_equity", r"total liabilities and (shareholders|stockholders) equity"),
)

_OPS, _INV, _FIN = r"operating activities", r"investing activities", r"financing activities"

CASH_FLOW_FIELDS: tuple[FieldSpec, ...] = (
    F("beginning_cash", r"cash and cash equivalents beginning of (the )?(year|period)"),
    F("net_income", r"net income", section=_OPS),
    F("depreciation_and_amortization", r"depreciation and amortization", section=_OPS),
    F("share_based_compensation", r"(share|stock) based compensation( expense)?",
      section=_OPS, required=False),
    F("deferred_income_taxes", r"deferred income tax(es)?( expense)?( benefit)?",
      section=_OPS, required=False),
    F("change_accounts_receivable", r"accounts receivable( net)?", section=_OPS,
      required=False),
    F("change_inventories", r"inventories", section=_OPS, required=False),
    F("change_vendor_non_trade_receivables", r"vendor non ?trade receivables",
      section=_OPS, required=False),
    F("change_other_assets", r"other current and non current assets", section=_OPS,
      required=False),
    F("change_accounts_payable", r"accounts payable", section=_OPS, required=False),
    F("change_deferred_revenue", r"deferred revenue", section=_OPS, required=False),
    F("change_other_liabilities", r"other current and non current liabilities",
      section=_OPS, required=False),
    F("cash_from_operations",
      r"(net )?cash (generated by|provided by|from) operating activities"),
    F("purchases_of_investments", r"purchases of marketable securities", section=_INV,
      required=False),
    F("maturities_of_investments", r"proceeds from maturities of marketable securities",
      section=_INV, required=False),
    F("sales_of_investments", r"proceeds from sales of marketable securities",
      section=_INV, required=False),
    F("acquisitions", r"payments made in connection with business acquisitions( net)?",
      section=_INV, required=False),
    F("capital_expenditures",
      r"payments for acquisition of property plant and equipment",
      r"capital expenditures", r"purchases of property (plant )?and equipment", section=_INV),
    F("purchases_of_intangibles", r"payments for acquisition of intangible assets",
      section=_INV, required=False),
    F("other_investing", r"other", section=_INV, required=False),
    F("cash_from_investing",
      r"(net )?cash (used in|generated by|provided by|from) investing activities"),
    F("proceeds_from_stock_issuance", r"proceeds from issuance of common stock",
      section=_FIN, required=False),
    F("excess_tax_benefits", r"excess tax benefits from equity awards", section=_FIN,
      required=False),
    F("taxes_on_net_share_settlement", r"taxes paid related to net share settlement.*",
      section=_FIN, required=False),
    F("dividends_paid", r"dividends( and dividend equivalents)? paid", section=_FIN,
      required=False),
    F("share_repurchases", r"repurchases? of common stock", section=_FIN, required=False),
    F("proceeds_from_debt_net", r"proceeds from issuance of (term|long term) debt( net)?",
      section=_FIN, required=False),
    F("repayment_of_debt", r"repayments? of (term|long term) debt", section=_FIN,
      required=False),
    F("change_in_commercial_paper", r"change in commercial paper( net)?", section=_FIN,
      required=False),
    F("other_financing", r"other", section=_FIN, required=False),
    F("cash_from_financing",
      r"(net )?cash (used in|generated by|provided by|from) financing activities"),
    F("net_change_in_cash",
      r"increase decrease in cash and cash equivalents", r"net (increase|change) in cash.*"),
    F("ending_cash", r"cash and cash equivalents end of (the )?(year|period)"),
    F("cash_taxes_paid", r"cash paid for income taxes( net)?", required=False),
    F("cash_interest_paid", r"cash paid for interest", required=False),
)

DEBT_TOTAL_FIELDS: tuple[FieldSpec, ...] = (
    F("reported_total_term_debt", r"total term debt"),
    F("unamortized_discount", r"unamortized (discount|premium).*", required=False),
    F("hedge_accounting_adjustments", r"hedge accounting fair value adjustments",
      required=False),
    F("current_portion_long_term_debt", r"less current portion of (long term|term) debt",
      absolute=True, required=False),
    F("long_term_debt", r"total long term debt"),
)

_CURRENCY_KEYWORDS: dict[str, str] = {
    "euro": "EUR", "swiss franc": "CHF", "japanese yen": "JPY", "british pound": "GBP",
    "australian dollar": "AUD", "canadian dollar": "CAD",
}

_RATE = r"\d+(?:\.\d+)?"
_TRANCHE_ROW = re.compile(
    rf"^(?P<kind>Floating|Fixed)-rate\s+"
    rf"(?:(?P<c_lo>{_RATE})%(?:\s*-\s*(?P<c_hi>{_RATE})%)?\s+)?notes\s+"
    rf"(?P<m_lo>\d{{4}})(?:\s*-\s*(?P<m_hi>\d{{4}}))?\s+"
    rf"\$?\s*(?P<amt>[\d,]+)\s+"
    rf"(?P<r_lo>{_RATE})%(?:\s*-\s*(?P<r_hi>{_RATE})%)?\s+"
    rf"\$?\s*(?P<prior>[\d,]+)\b",
    re.I,
)


# --------------------------------------------------------------------------------------
# 3 + 6. The extractor
# --------------------------------------------------------------------------------------
class TenKExtractor:
    """Extracts a validated :class:`FinancialStatementSchema` from a 10-K filing.

    Example::

        schema = TenKExtractor("data/raw/apple_10k_fy2015.pdf", ticker="AAPL").extract()
        schema.balance_sheet(2015).total_assets   # 290479.0

    Args:
        source: Path to the filing (PDF or text).
        ticker: Optional exchange ticker (not stated in most 10-K cover pages).
        text: Pre-loaded raw text; if given, ``source`` is used only as a label.
    """

    def __init__(self, source: str | Path, ticker: str | None = None,
                 text: str | None = None) -> None:
        self.source = str(source)
        self.ticker = ticker
        self.raw_text = text if text is not None else load_document(source)
        self.lines = normalize_lines(self.raw_text)
        # Single-line view for prose regexes that may span line breaks.
        self.flat_text = re.sub(r"\s+", " ", self.raw_text.translate(_CHAR_MAP))

    # ------------------------------------------------------------------ public
    def extract(self) -> FinancialStatementSchema:
        """Run the full pipeline.

        Returns:
            A schema whose accounting identities have all been verified.

        Raises:
            ExtractionError: If a statement or required line item is missing.
            AccountingIdentityError: If the extracted figures do not tie.
        """
        income = self._extract_statement(
            "Income Statement", r"consolidated statements of operations",
            INCOME_STATEMENT_FIELDS,
        )
        years = [row["fiscal_year"] for row in income]
        balance = self._extract_statement(
            "Balance Sheet", r"consolidated balance sheets?", BALANCE_SHEET_FIELDS,
        )
        cash_flow = self._extract_statement(
            "Cash Flow Statement", r"consolidated statements of cash flows",
            CASH_FLOW_FIELDS, section_pattern=rf"{_OPS}|{_INV}|{_FIN}",
        )

        payload = {
            "metadata": self._extract_metadata(),
            "income_statements": income,
            "balance_sheets": balance,
            "cash_flow_statements": cash_flow,
            "equity_roll_forwards": self._extract_equity_roll_forwards(),
            "revenue_segments": self._extract_segments(years),
            "debt_schedule": self._extract_debt_schedule(
                max(years),
                commercial_paper=next((b.get("commercial_paper", 0.0) for b in balance
                                       if b["fiscal_year"] == max(years)), 0.0),
            ),
            "reported_effective_tax_rates": self._extract_effective_tax_rates(years),
            "statutory_tax_rate": self._extract_statutory_tax_rate(),
        }
        schema = FinancialStatementSchema.model_validate(payload)
        logger.info("Extraction complete: %s FY%s-FY%s, all identities verified",
                    schema.metadata.name, min(years), max(years))
        return schema

    # ------------------------------------------------------------------ locating
    def _find_line(self, pattern: str, start: int = 0, *, required_for: str) -> int:
        """Index of the first line at or after ``start`` whose normalized text fully
        matches ``pattern``."""
        regex = re.compile(pattern)
        for i in range(start, len(self.lines)):
            if regex.fullmatch(normalize_label(self.lines[i])):
                return i
        raise StatementNotFoundError(required_for, pattern)

    def _statement_block(self, title_pattern: str, name: str) -> list[str]:
        """Lines of a primary statement: from its title (followed by an
        ``(In millions`` units line, which excludes TOC entries) to
        ``See accompanying Notes``."""
        i = 0
        while True:
            i = self._find_line(title_pattern, i, required_for=name)
            if i + 1 < len(self.lines) and self.lines[i + 1].lower().startswith("(in millions"):
                break
            i += 1
        end = self._find_line(r"see accompanying notes.*", i, required_for=f"{name} (end)")
        return self.lines[i + 2:end]

    # ------------------------------------------------------------------ statements
    def _extract_statement(
        self, name: str, title_pattern: str, specs: Sequence[FieldSpec],
        section_pattern: str | None = None,
    ) -> list[dict[str, Any]]:
        """Locate, parse and map one primary statement into per-year dicts."""
        parsed = parse_statement(name, self._statement_block(title_pattern, name),
                                 section_pattern)
        if not parsed.years:
            raise ExtractionError(f"{name}: could not determine fiscal-year column headers")
        logger.info("%s: %d rows, fiscal years %s", name, len(parsed.rows), parsed.years)
        return by_period(parsed.years, map_fields(parsed, specs, len(parsed.years)))

    def _extract_equity_roll_forwards(self) -> list[dict[str, Any]]:
        """Parse the statement of shareholders' equity into yearly roll-forwards.

        The statement has five columns: shares, common stock & APIC, retained
        earnings, AOCI and total equity. Each year runs from one ``Balances as of``
        row to the next.
        """
        name = "Statement of Shareholders' Equity"
        try:
            lines = self._statement_block(
                r"consolidated statements of (shareholders|stockholders) equity", name)
        except StatementNotFoundError:
            logger.warning("%s not found; retained earnings roll-forward not checked", name)
            return []
        re_col, total_col, n_cols = 2, 4, 5
        balance_re = re.compile(r"Balances as of [A-Za-z]+ \d{1,2}, (\d{4})")

        results: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in parse_statement(name, lines).rows:
            if len(row.values) < n_cols:
                raise ExtractionError(f"{name}: expected {n_cols} columns in {row.raw!r}")
            cols = row.values[-n_cols:]
            balance = balance_re.search(row.raw)
            if balance:
                if current is not None:
                    current.update(fiscal_year=int(balance.group(1)),
                                   ending_retained_earnings=cols[re_col],
                                   ending_total_equity=cols[total_col])
                    results.append(current)
                current = {
                    "beginning_retained_earnings": cols[re_col],
                    "beginning_total_equity": cols[total_col],
                    "net_income": 0.0, "dividends_declared": 0.0, "share_repurchases": 0.0,
                    "other_retained_earnings_changes": 0.0, "total_equity_movements": 0.0,
                }
                continue
            if current is None:
                raise ExtractionError(f"{name}: movement row before opening balance: {row.raw!r}")
            if re.fullmatch(r"net income", row.norm):
                key = "net_income"
            elif re.fullmatch(r"dividends.*", row.norm):
                key = "dividends_declared"
            elif re.fullmatch(r"repurchases? of common stock", row.norm):
                key = "share_repurchases"
            else:
                key = "other_retained_earnings_changes"
            current[key] += cols[re_col]
            current["total_equity_movements"] += cols[total_col]
        return results

    def _extract_segments(self, years: Sequence[int]) -> list[dict[str, Any]]:
        """Parse the MD&A net-sales tables by geographic segment and by product.

        Each table runs from its heading to the ``Total net sales`` row. Year-over-year
        change columns are percentages and are discarded; footnote markers such as
        ``(1)`` are stripped from names.
        """
        tables = (
            ("geographic", r"net sales by (operating )?segment"),
            ("product", r"net sales by product"),
        )
        segments: list[dict[str, Any]] = []
        for seg_type, anchor in tables:
            try:
                start = self._find_line(anchor, required_for=f"{seg_type} segments")
            except StatementNotFoundError:
                logger.warning("No %s segment table found; skipping", seg_type)
                continue
            for line in self.lines[start + 1:]:
                if normalize_label(line).startswith("total net sales"):
                    break
                label, tokens = split_line(line)
                values = [v for v, pct in filter(None, map(parse_number, tokens)) if not pct]
                if len(values) < len(years):
                    continue
                name = re.sub(r"(\s*\(\d\))+$", "", label).strip()
                segments.append({
                    "name": name, "segment_type": seg_type,
                    "net_sales": dict(zip(years, values[-len(years):], strict=True)),
                })
        return segments

    def _extract_debt_schedule(self, as_of_year: int,
                               commercial_paper: float) -> dict[str, Any] | None:
        """Parse the term-debt note: one :class:`DebtTranche` per row, plus the
        reconciliation from face value to balance-sheet carrying value.

        Args:
            as_of_year: Fiscal year of the latest balance sheet.
            commercial_paper: Commercial paper balance from that balance sheet
                (short-term debt sits outside the term-debt table).
        """
        try:
            start = self._find_line(r".*summary of the company s term debt.*",
                                    required_for="Term debt note")
        except StatementNotFoundError:
            logger.warning("No term debt table found; debt schedule omitted")
            return None
        end = self._find_line(r"total long term debt.*", start,
                              required_for="Term debt note (end)")
        block = self.lines[start + 1:end + 1]

        tranches: list[dict[str, Any]] = []
        issuance, currency = None, "USD"
        i = 0
        while i < len(block):
            line = block[i]
            if "issuance" in line.lower():
                # Issuance headers may wrap: "...debt issuance of" / "SFr1.25 billion:"
                header = line
                while not header.endswith(":") and i + 1 < len(block):
                    i += 1
                    header += " " + block[i]
                issuance = header.rstrip(":")
                currency = next((code for kw, code in _CURRENCY_KEYWORDS.items()
                                 if kw in issuance.lower()), "USD")
            elif m := _TRANCHE_ROW.match(line):
                tranches.append(self._tranche_from_match(m, issuance, currency))
            i += 1

        if not tranches:
            raise ExtractionError("Term debt note located but no tranche rows parsed")

        totals_stmt = parse_statement("Term Debt Note", block)
        totals = map_fields(totals_stmt, DEBT_TOTAL_FIELDS, n_periods=2)
        schedule = {k: v[0] for k, v in totals.items()}
        schedule["prior_reported_total_term_debt"] = totals["reported_total_term_debt"][1]
        schedule.update(
            as_of_fiscal_year=as_of_year,
            tranches=tranches,
            commercial_paper=commercial_paper,
            commercial_paper_rate=self._extract_commercial_paper_rate(),
        )
        logger.info("Debt: %d tranches parsed", len(tranches))
        return schedule

    @staticmethod
    def _tranche_from_match(m: re.Match[str], issuance: str | None,
                            currency: str) -> dict[str, Any]:
        """Convert a regex match on a debt-table row into a tranche dict."""
        def pct(s: str | None) -> float | None:
            return pct_to_decimal(s) if s is not None else None

        fixed = m["kind"].lower() == "fixed"
        c_lo, c_hi = pct(m["c_lo"]), pct(m["c_hi"] or m["c_lo"])
        m_lo = int(m["m_lo"])
        m_hi = int(m["m_hi"] or m_lo)
        coupon = (f" {m['c_lo']}%" + (f"-{m['c_hi']}%" if m["c_hi"] else "")) if fixed else ""
        maturity = f"{m_lo}" + (f"-{m_hi}" if m_hi != m_lo else "")
        return {
            "name": f"{m['kind'].title()}-rate{coupon} notes due {maturity} ({currency})",
            "instrument_type": (InstrumentType.FIXED_RATE_NOTE if fixed
                                else InstrumentType.FLOATING_RATE_NOTE),
            "currency": currency,
            "issuance": issuance,
            "principal": float(m["amt"].replace(",", "")),
            "prior_year_principal": float(m["prior"].replace(",", "")),
            "coupon_rate_low": c_lo if fixed else None,
            "coupon_rate_high": c_hi if fixed else None,
            "effective_rate_low": pct(m["r_lo"]),
            "effective_rate_high": pct(m["r_hi"] or m["r_lo"]),
            "maturity_year_start": m_lo,
            "maturity_year_end": m_hi,
        }

    # ------------------------------------------------------------------ prose items
    def _extract_commercial_paper_rate(self) -> float | None:
        """Weighted-average commercial paper rate stated in the liquidity discussion."""
        m = re.search(r"commercial paper outstanding.{0,120}?weighted-average interest rate "
                      rf"of ({_RATE})%", self.flat_text, re.I)
        return pct_to_decimal(m.group(1)) if m else None

    def _extract_effective_tax_rates(self, years: Sequence[int]) -> dict[int, float]:
        """Reported effective tax rates (``Effective tax rate 26.4% 26.1% 26.2%``)."""
        for line in self.lines:
            label, tokens = split_line(line)
            if normalize_label(label) == "effective tax rate":
                rates = [v for v, pct in filter(None, map(parse_number, tokens)) if pct]
                if len(rates) >= len(years):
                    return dict(zip(years, rates[-len(years):], strict=True))
        logger.warning("Effective tax rate table not found")
        return {}

    def _extract_statutory_tax_rate(self) -> float | None:
        """Statutory federal rate quoted in the tax discussion (e.g. 35%)."""
        m = re.search(rf"statutory federal income tax rate (?:of )?\(?({_RATE})%",
                      self.flat_text, re.I)
        return pct_to_decimal(m.group(1)) if m else None

    def _extract_metadata(self) -> dict[str, Any]:
        """Registrant name, fiscal year end and CIK from the cover page / EDGAR URL."""
        fye = re.search(r"for the fiscal year ended (\w+ \d{1,2}, \d{4})", self.flat_text, re.I)
        if not fye:
            raise StatementNotFoundError("Cover page", "For the fiscal year ended <date>")
        idx = next((i for i, line in enumerate(self.lines)
                    if line.lower().startswith("commission file number")), None)
        if idx is None or idx + 1 >= len(self.lines):
            raise StatementNotFoundError("Cover page", "Commission File Number")
        cik = re.search(r"edgar/data/(\d+)/", self.raw_text)
        return {
            "name": self.lines[idx + 1],
            "ticker": self.ticker,
            "cik": cik.group(1) if cik else None,
            "fiscal_year_end": datetime.strptime(fye.group(1), "%B %d, %Y").date(),
            "source_document": self.source,
        }


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point: extract a filing and write the validated JSON.

    Returns:
        Process exit code (0 on success, 1 on any data or validation failure).
    """
    parser = argparse.ArgumentParser(description="Extract a 10-K into a validated schema.")
    parser.add_argument("source", type=Path, help="Path to the 10-K (PDF or text)")
    parser.add_argument("--ticker", help="Exchange ticker, e.g. AAPL")
    parser.add_argument("--out", type=Path, default=Path("outputs/extracted"),
                        help="Output directory for the JSON file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-8s %(name)s: %(message)s")
    try:
        schema = TenKExtractor(args.source, ticker=args.ticker).extract()
    except (FinancialDataError, ValidationError) as exc:
        logger.error("%s", exc)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", schema.metadata.name.lower()).strip("_")
    out_path = args.out / f"{slug}_fy{schema.latest_year}.json"
    out_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

    with_pct = schema.historical_metrics()
    print(f"\n{schema.metadata.name} - historical metrics (USD millions)\n")
    print(with_pct.to_string(float_format=lambda v: f"{v:,.3f}" if abs(v) < 1 else f"{v:,.0f}"))
    if schema.debt_schedule:
        ds = schema.debt_schedule
        print(f"\nTerm debt: {len(ds.tranches)} tranches, face ${ds.total_principal:,.0f}M, "
              f"weighted-avg effective rate {ds.weighted_average_effective_rate:.2%}")
    print(f"\nAll accounting identities verified. Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
