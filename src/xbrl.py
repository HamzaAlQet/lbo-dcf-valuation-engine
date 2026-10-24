"""XBRL ingestion: any SEC 10-K -> normalized facts.

Every 10-K filed with the SEC carries its financial statements as XBRL facts tagged
with standard US-GAAP names (``Revenues``, ``Assets``, ``NetIncomeLoss``...). Reading
those facts is far more reliable across companies than parsing statement layouts out
of a PDF. Two sources produce the same :class:`FilingFacts`:

- :func:`load_inline_xbrl`: the 10-K ``.htm`` downloaded from EDGAR. Since 2019-2021
  the XBRL is embedded in the document itself (inline XBRL).
- :func:`load_company_facts`: the SEC's ``companyfacts`` API, looked up by ticker or
  CIK, restricted to the facts of one 10-K filing.

Only consolidated facts are kept: facts tied to a segment or other dimension are
dropped, as are company-specific extension tags.

SEC access requires a User-Agent that identifies the requester. Set the
``SEC_USER_AGENT`` environment variable to something like ``"Your Name you@example.com"``
(see https://www.sec.gov/os/accessing-edgar-data).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from src.exceptions import ExtractionError

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DEFAULT_USER_AGENT = "lbo-valuation-engine contact@example.com"

ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 340, 380
"""A duration fact counts as a fiscal year if it spans this many days (52/53-week years)."""


class OutOfScopeError(ExtractionError):
    """The filing cannot be modelled with an operating-company template (for example
    a bank or insurer with an unclassified balance sheet, or a non-USD filer)."""


# --------------------------------------------------------------------------------------
# Normalized facts
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Fact:
    """One XBRL fact.

    Attributes:
        concept: Local name, e.g. ``"Revenues"`` (us-gaap) or ``"dei:DocumentType"``.
        value: Numeric value in raw units (dollars, shares), or text for dei facts.
        unit: ``"USD"``, ``"shares"``, ``"USD/shares"``, other ISO code, or ``None``.
        start: Period start for duration facts, ``None`` for instants.
        end: Period end (duration) or the instant date.
    """

    concept: str
    value: float | str
    unit: str | None
    start: date | None
    end: date

    @property
    def is_annual(self) -> bool:
        """True for a duration of roughly one fiscal year."""
        return (self.start is not None
                and ANNUAL_MIN_DAYS <= (self.end - self.start).days <= ANNUAL_MAX_DAYS)


@dataclass
class FilingFacts:
    """The consolidated facts of one 10-K, plus identifying information.

    Attributes:
        source: File path or URL the facts came from.
        entity_name: Registrant name.
        cik: SEC Central Index Key (no leading zeros), if known.
        ticker: Trading symbol, if known.
        fiscal_year: The filing's fiscal year label (e.g. 2025), if stated.
        period_end: Balance sheet date of the latest fiscal year.
        facts: us-gaap facts by local concept name.
        dei: Document and entity information facts by local name.
    """

    source: str
    entity_name: str
    cik: str | None
    ticker: str | None
    fiscal_year: int | None
    period_end: date
    facts: dict[str, list[Fact]] = field(default_factory=dict)
    dei: dict[str, list[Fact]] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def annual(self, concept: str, end: date) -> float | None:
        """Value of a fiscal-year duration fact ending on ``end``."""
        for f in self.facts.get(concept, []):
            if f.end == end and f.is_annual and isinstance(f.value, float):
                return f.value
        return None

    def instant(self, concept: str, at: date) -> float | None:
        """Value of an instant fact on date ``at``."""
        for f in self.facts.get(concept, []):
            if f.start is None and f.end == at and isinstance(f.value, float):
                return f.value
        return None

    def annual_periods(self, concepts: Iterable[str]) -> list[tuple[date, date]]:
        """Distinct fiscal-year (start, end) periods reported for the first of
        ``concepts`` that has any, most recent first, ending on or before
        :attr:`period_end`."""
        for concept in concepts:
            periods = {(f.start, f.end) for f in self.facts.get(concept, [])
                       if f.is_annual and f.end <= self.period_end}
            if periods:
                return sorted(periods, key=lambda p: p[1], reverse=True)  # type: ignore[arg-type,return-value]
        return []


def _usd_only(facts: dict[str, list[Fact]]) -> None:
    """Raise if monetary facts are reported in a currency other than USD."""
    currencies = {f.unit for fs in facts.values() for f in fs
                  if f.unit and f.unit.isalpha() and f.unit.isupper() and len(f.unit) == 3}
    if currencies and "USD" not in currencies:
        raise OutOfScopeError(f"Financial statements are reported in {sorted(currencies)}; "
                              "only USD filers are supported")


# --------------------------------------------------------------------------------------
# SEC API (ticker / CIK)
# --------------------------------------------------------------------------------------
def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        logger.warning("SEC_USER_AGENT is not set; using a generic User-Agent. The SEC asks "
                       "for 'Name email@domain' (https://www.sec.gov/os/accessing-edgar-data).")
        return DEFAULT_USER_AGENT
    return ua


def sec_get_json(url: str, timeout: float = 60.0) -> dict:
    """GET a JSON document from SEC EDGAR.

    Raises:
        ExtractionError: On HTTP or network errors, with the reason.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _user_agent(),
                                                   "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ExtractionError(f"SEC returned 404 (not found) for {url}") from exc
        if exc.code == 403:
            raise ExtractionError(
                "SEC refused the request (403). Set SEC_USER_AGENT to 'Your Name "
                "your@email.com' as the SEC requires.") from exc
        raise ExtractionError(f"SEC request failed with HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Could not reach SEC EDGAR: {exc.reason}") from exc


def resolve_cik(ticker_or_cik: str) -> tuple[int, str | None]:
    """Map a ticker (or a numeric CIK) to ``(cik, ticker)``.

    Raises:
        ExtractionError: If the ticker is unknown.
    """
    text = ticker_or_cik.strip().upper()
    if text.isdigit():
        return int(text), None
    table = sec_get_json(SEC_TICKERS_URL)
    for row in table.values():
        if row["ticker"].upper() == text:
            return int(row["cik_str"]), row["ticker"].upper()
    raise ExtractionError(f"Ticker '{text}' was not found in the SEC company list")


def _parse_date(text: str) -> date:
    return datetime.strptime(text.strip()[:10], "%Y-%m-%d").date()


def company_facts_to_filing(data: dict, *, fiscal_year: int | None = None,
                            ticker: str | None = None, source: str = "SEC companyfacts"
                            ) -> FilingFacts:
    """Select one 10-K's facts from a ``companyfacts`` JSON document.

    Args:
        data: Parsed ``companyfacts`` JSON.
        fiscal_year: Fiscal year of the 10-K to use; ``None`` = most recently filed.
        ticker: Ticker to record in the metadata.
        source: Description of where the data came from.

    Raises:
        ExtractionError: If no matching 10-K is found.
    """
    gaap = data.get("facts", {}).get("us-gaap", {})
    filings: dict[str, tuple[str, int | None]] = {}   # accn -> (filed, fy)
    for concept in gaap.values():
        for unit_facts in concept["units"].values():
            for x in unit_facts:
                if x.get("form") in ("10-K", "10-K/A") and x.get("fp") == "FY":
                    filings.setdefault(x["accn"], (x["filed"], x.get("fy")))
    if not filings:
        raise ExtractionError("No 10-K filings with XBRL data were found for this company")
    candidates = [(filed, accn, fy) for accn, (filed, fy) in filings.items()
                  if fiscal_year is None or fy == fiscal_year]
    if not candidates:
        years = sorted({fy for _, fy in filings.values() if fy})
        raise ExtractionError(f"No 10-K for fiscal year {fiscal_year}; available: {years}")
    filed, accn, fy = max(candidates)

    def collect(namespace: dict) -> dict[str, list[Fact]]:
        out: dict[str, list[Fact]] = defaultdict(list)
        for name, concept in namespace.items():
            for unit, unit_facts in concept["units"].items():
                for x in unit_facts:
                    if x.get("accn") != accn:
                        continue
                    out[name].append(Fact(
                        concept=name, value=float(x["val"]), unit=unit,
                        start=_parse_date(x["start"]) if "start" in x else None,
                        end=_parse_date(x["end"])))
        return dict(out)

    facts = collect(gaap)
    _usd_only(facts)
    dei = collect(data.get("facts", {}).get("dei", {}))
    ends = [f.end for fs in facts.values() for f in fs if f.is_annual]
    if not ends:
        raise ExtractionError(f"10-K {accn} has no fiscal-year facts")
    return FilingFacts(source=f"{source} (accession {accn}, filed {filed})",
                       entity_name=data.get("entityName", "Unknown registrant"),
                       cik=str(int(data["cik"])) if data.get("cik") else None,
                       ticker=ticker, fiscal_year=fy, period_end=max(ends),
                       facts=facts, dei=dei)


def load_company_facts(ticker_or_cik: str, fiscal_year: int | None = None) -> FilingFacts:
    """Fetch a company's 10-K facts from SEC EDGAR by ticker or CIK.

    Args:
        ticker_or_cik: e.g. ``"MSFT"`` or ``"789019"``.
        fiscal_year: Fiscal year of the 10-K to use; ``None`` = most recent.
    """
    cik, ticker = resolve_cik(ticker_or_cik)
    url = SEC_FACTS_URL.format(cik=cik)
    logger.info("Fetching %s", url)
    return company_facts_to_filing(sec_get_json(url), fiscal_year=fiscal_year,
                                   ticker=ticker, source=url)


# --------------------------------------------------------------------------------------
# Inline XBRL (.htm 10-K from EDGAR)
# --------------------------------------------------------------------------------------
_ZERO_TEXT = {"", "-", "—", "–", "none", "no", "nil", "zero"}


@dataclass
class _Context:
    start: date | None = None
    end: date | None = None
    dimensional: bool = False


class _InlineXbrlParser(HTMLParser):
    """Streaming parser for ``ix:nonFraction`` / ``ix:nonNumeric`` facts, contexts and
    units. HTMLParser tolerates the HTML quirks found in real filings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, _Context] = {}
        self.units: dict[str, str] = {}
        self.raw_facts: list[tuple[str, dict[str, str], str]] = []  # (kind, attrs, text)
        self._context: tuple[str, _Context] | None = None
        self._period_tag: str | None = None
        self._unit_id: str | None = None
        self._unit_measures: list[str] = []
        self._in_measure = False
        self._fact_stack: list[tuple[str, dict[str, str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "xbrli:context":
            self._context = (a.get("id", ""), _Context())
        elif self._context and tag in ("xbrli:startdate", "xbrli:enddate", "xbrli:instant"):
            self._period_tag = tag
        elif self._context and tag in ("xbrli:segment", "xbrli:scenario"):
            self._context[1].dimensional = True
        elif tag == "xbrli:unit":
            self._unit_id, self._unit_measures = a.get("id", ""), []
        elif self._unit_id is not None and tag == "xbrli:measure":
            self._in_measure = True
        elif tag in ("ix:nonfraction", "ix:nonnumeric"):
            self._fact_stack.append((tag, a, []))

    def handle_endtag(self, tag: str) -> None:
        if tag == "xbrli:context" and self._context:
            self.contexts[self._context[0]] = self._context[1]
            self._context = None
        elif tag in ("xbrli:startdate", "xbrli:enddate", "xbrli:instant"):
            self._period_tag = None
        elif tag == "xbrli:unit" and self._unit_id is not None:
            m = [x.split(":")[-1] for x in self._unit_measures]
            self.units[self._unit_id] = "/".join(m) if len(m) == 2 else (m[0] if m else "")
            self._unit_id = None
        elif tag == "xbrli:measure":
            self._in_measure = False
        elif tag in ("ix:nonfraction", "ix:nonnumeric") and self._fact_stack:
            kind, attrs, text = self._fact_stack.pop()
            content = "".join(text)
            if self._fact_stack:  # nested facts: the outer one also displays this text
                self._fact_stack[-1][2].append(content)
            self.raw_facts.append((kind, attrs, content))

    def handle_data(self, data: str) -> None:
        if self._period_tag and self._context:
            d = _parse_date(data)
            ctx = self._context[1]
            if self._period_tag == "xbrli:startdate":
                ctx.start = d
            else:
                ctx.end = d
        elif self._in_measure:
            self._unit_measures.append(data.strip())
        elif self._fact_stack:
            self._fact_stack[-1][2].append(data)


def _parse_number(text: str, attrs: dict[str, str]) -> float | None:
    """Apply ix transformation rules: format, scale and sign."""
    fmt = attrs.get("format", "").lower()
    clean = text.strip().lower()
    if "zero" in fmt or clean in _ZERO_TEXT:
        value = 0.0
    else:
        if "comma-decimal" in fmt or "numcommadecimal" in fmt:
            clean = clean.replace(".", "").replace(" ", "").replace(",", ".")
        clean = re.sub(r"[^0-9.]", "", clean)
        if not clean or clean.count(".") > 1:
            return None
        value = float(clean)
    value *= 10 ** int(attrs.get("scale", "0") or 0)
    return -value if attrs.get("sign") == "-" else value


def parse_inline_xbrl(html: str, source: str = "uploaded 10-K") -> FilingFacts:
    """Extract consolidated facts from an inline XBRL 10-K document.

    Args:
        html: Document text.
        source: Description for the metadata.

    Raises:
        ExtractionError: If the document contains no inline XBRL facts (for example a
            pre-2019 filing, a PDF printout, or a non-10-K document).
    """
    parser = _InlineXbrlParser()
    parser.feed(html)
    parser.close()
    if not parser.raw_facts:
        raise ExtractionError(
            "No inline XBRL facts found. Upload the 10-K .htm document from EDGAR (inline "
            "XBRL, filed since 2019-2021), or use the ticker lookup for older filings.")

    facts: dict[str, list[Fact]] = defaultdict(list)
    dei: dict[str, list[Fact]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for kind, attrs, text in parser.raw_facts:
        prefix, _, local = attrs.get("name", "").partition(":")
        if prefix not in ("us-gaap", "dei"):
            continue
        ctx = parser.contexts.get(attrs.get("contextref", ""))
        if ctx is None or ctx.dimensional or ctx.end is None:
            continue
        key = (attrs.get("name", ""), attrs.get("contextref", ""))
        if key in seen:  # the same fact is often displayed more than once
            continue
        seen.add(key)
        if kind == "ix:nonfraction":
            value: float | str | None = _parse_number(text, attrs)
            if value is None:
                continue
        else:
            value = " ".join(text.split())
        fact = Fact(concept=local, value=value,
                    unit=parser.units.get(attrs.get("unitref", "")) or None,
                    start=ctx.start, end=ctx.end)
        (dei if prefix == "dei" else facts)[local].append(fact)

    facts_d, dei_d = dict(facts), dict(dei)
    _usd_only(facts_d)

    def dei_text(name: str) -> str | None:
        vals = [f.value for f in dei_d.get(name, []) if isinstance(f.value, str) and f.value]
        return vals[0] if vals else None

    doc_type = dei_text("DocumentType")
    if doc_type and not doc_type.startswith("10-K"):
        raise OutOfScopeError(f"This document is a {doc_type}, not a 10-K annual report")
    period_text = dei_text("DocumentPeriodEndDate")
    ends = [f.end for fs in facts_d.values() for f in fs if f.is_annual]
    if not ends:
        raise ExtractionError("The document has no fiscal-year (annual) facts")
    period_end = max(ends)
    if period_text:
        try:
            stated = datetime.strptime(period_text, "%B %d, %Y").date()
            if abs((stated - period_end).days) <= 7:
                period_end = max(e for e in ends if abs((e - stated).days) <= 7)
        except ValueError:
            pass
    fy_text = dei_text("DocumentFiscalYearFocus")
    cik_text = dei_text("EntityCentralIndexKey")
    return FilingFacts(
        source=source,
        entity_name=dei_text("EntityRegistrantName") or "Unknown registrant",
        cik=str(int(cik_text)) if cik_text and cik_text.isdigit() else None,
        ticker=(dei_text("TradingSymbol") or "").upper() or None,
        fiscal_year=int(fy_text) if fy_text and fy_text.isdigit() else None,
        period_end=period_end, facts=facts_d, dei=dei_d)


def load_inline_xbrl(path: str | Path) -> FilingFacts:
    """Read an inline XBRL 10-K ``.htm`` file from disk."""
    path = Path(path)
    if not path.is_file():
        raise ExtractionError(f"File not found: {path}")
    html = path.read_bytes().decode("utf-8", errors="replace")
    return parse_inline_xbrl(html, source=str(path))


def previous_day(d: date) -> date:
    """The day before ``d`` (period start - 1 = prior period end)."""
    return d - timedelta(days=1)
