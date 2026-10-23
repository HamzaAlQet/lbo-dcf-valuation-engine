"""Domain-specific exception hierarchy.

All errors raised by the engine derive from :class:`FinancialDataError`, so callers
(CLI, notebooks, CI) can catch one base type while still distinguishing between
*bad source data* (extraction problems) and *broken accounting* (identity failures).

Design note
-----------
These exceptions deliberately do **not** inherit from ``ValueError``. Pydantic wraps
``ValueError`` / ``AssertionError`` raised inside validators into a generic
``ValidationError``; by using a separate base class, an accounting breach surfaces
as-is, with its own type and a message an analyst can read at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass


class FinancialDataError(Exception):
    """Base class for every error raised by the valuation engine."""


# --------------------------------------------------------------------------------------
# Accounting integrity
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityBreach:
    """A single failed accounting identity.

    Attributes:
        identity: Human-readable statement of the identity, e.g. ``"Assets = L + E"``.
        lhs: Value of the left-hand side.
        rhs: Value of the right-hand side.
        tolerance: Absolute tolerance that was applied.
    """

    identity: str
    lhs: float
    rhs: float
    tolerance: float

    @property
    def delta(self) -> float:
        """Signed difference ``lhs - rhs``."""
        return self.lhs - self.rhs

    def __str__(self) -> str:
        return (
            f"{self.identity}: {self.lhs:,.2f} != {self.rhs:,.2f} "
            f"(delta = {self.delta:+,.2f}, tolerance = {self.tolerance:,.2f})"
        )


class AccountingIdentityError(FinancialDataError):
    """Raised when one or more accounting identities do not hold.

    Every breach found during a validation pass is collected and reported together,
    so a single run shows the analyst the full extent of the problem.
    """

    def __init__(self, context: str, breaches: list[IdentityBreach]) -> None:
        self.context = context
        self.breaches = breaches
        lines = "\n".join(f"  - {b}" for b in breaches)
        super().__init__(
            f"[ACCOUNTING IDENTITY FAILURE] {context}: "
            f"{len(breaches)} identity check(s) failed\n{lines}"
        )


class AssumptionError(FinancialDataError):
    """Raised when assumptions are individually valid but jointly infeasible,
    e.g. acquisition debt plus balance-sheet cash exceeding total uses of funds."""


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------
class ExtractionError(FinancialDataError):
    """Raised when a source document cannot be parsed into the schema."""


class StatementNotFoundError(ExtractionError):
    """Raised when a required financial statement or note cannot be located."""

    def __init__(self, statement: str, anchor: str) -> None:
        self.statement = statement
        self.anchor = anchor
        super().__init__(
            f"[STATEMENT NOT FOUND] Could not locate '{statement}' "
            f"(searched for anchor: {anchor!r})"
        )


class MissingLineItemError(ExtractionError):
    """Raised when a required line item is absent from a located statement."""

    def __init__(self, statement: str, field: str, patterns: list[str]) -> None:
        self.statement = statement
        self.field = field
        self.patterns = patterns
        super().__init__(
            f"[MISSING LINE ITEM] {statement}: required field '{field}' not found. "
            f"Label patterns tried: {patterns}"
        )


class AmbiguousLineItemError(ExtractionError):
    """Raised when more than one row in a statement matches a single field."""

    def __init__(self, statement: str, field: str, labels: list[str]) -> None:
        self.statement = statement
        self.field = field
        self.labels = labels
        super().__init__(
            f"[AMBIGUOUS LINE ITEM] {statement}: field '{field}' matched "
            f"{len(labels)} rows: {labels}"
        )
