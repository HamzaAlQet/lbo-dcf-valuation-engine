"""Pure financial formulas shared by the schemas, the modeling engine and the tests.

Every function here is side-effect free and takes plain floats, so each formula has
exactly one definition in the codebase and is unit-tested in isolation.

Sign conventions (applied consistently across the project)
---------------------------------------------------------
- ``capex`` is passed as a **positive** number representing a cash outflow.
- ``change_in_nwc`` is the **increase** in net working capital. A positive value is
  an investment (cash outflow); a negative value is a release of cash.
- Rates (tax, growth, discount) are decimals: ``0.264`` means 26.4%.
"""

from __future__ import annotations

import math


def _require_finite(**values: float) -> None:
    """Raise ``ValueError`` if any argument is NaN or infinite.

    Args:
        **values: Named numeric inputs to check.

    Raises:
        ValueError: If any value is not a finite number.
    """
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"'{name}' must be a finite number, got {value!r}")


def safe_divide(numerator: float, denominator: float) -> float:
    """Divide, returning ``0.0`` instead of raising when the denominator is zero.

    Used for ratios (margins, rates) where a zero base means the ratio is undefined
    and should not crash a reporting pass.

    Args:
        numerator: Dividend.
        denominator: Divisor.

    Returns:
        ``numerator / denominator``, or ``0.0`` when ``denominator == 0``.
    """
    return numerator / denominator if denominator else 0.0


def ebitda(ebit: float, depreciation_and_amortization: float) -> float:
    """Compute EBITDA = EBIT + D&A.

    Args:
        ebit: Earnings before interest and taxes (operating income).
        depreciation_and_amortization: Non-cash D&A charge (positive).

    Returns:
        EBITDA.
    """
    _require_finite(ebit=ebit, depreciation_and_amortization=depreciation_and_amortization)
    return ebit + depreciation_and_amortization


def nopat(ebit: float, tax_rate: float) -> float:
    """Compute net operating profit after tax, NOPAT = EBIT x (1 - t).

    Args:
        ebit: Earnings before interest and taxes.
        tax_rate: Cash/marginal tax rate as a decimal in ``[0, 1)``.

    Returns:
        NOPAT.

    Raises:
        ValueError: If ``tax_rate`` is outside ``[0, 1)``.
    """
    _require_finite(ebit=ebit, tax_rate=tax_rate)
    if not 0.0 <= tax_rate < 1.0:
        raise ValueError(f"tax_rate must be in [0, 1), got {tax_rate}")
    return ebit * (1.0 - tax_rate)


def unlevered_free_cash_flow(
    ebit: float,
    tax_rate: float,
    depreciation_and_amortization: float,
    capex: float,
    change_in_nwc: float,
) -> float:
    """Compute unlevered free cash flow (free cash flow to the firm).

    ``UFCF = EBIT x (1 - t) + D&A - Capex - Change in NWC``

    Args:
        ebit: Earnings before interest and taxes.
        tax_rate: Tax rate as a decimal in ``[0, 1)``.
        depreciation_and_amortization: D&A added back (positive).
        capex: Capital expenditure as a positive outflow.
        change_in_nwc: Increase in net working capital (positive = cash use).

    Returns:
        Unlevered free cash flow.

    Raises:
        ValueError: If ``capex`` or ``depreciation_and_amortization`` is negative
            (a sign-convention error upstream) or the tax rate is out of range.
    """
    _require_finite(capex=capex, change_in_nwc=change_in_nwc)
    if capex < 0:
        raise ValueError(f"capex must be passed as a positive outflow, got {capex}")
    if depreciation_and_amortization < 0:
        raise ValueError(
            f"depreciation_and_amortization must be positive, got {depreciation_and_amortization}"
        )
    return nopat(ebit, tax_rate) + depreciation_and_amortization - capex - change_in_nwc


def effective_tax_rate(income_tax_expense: float, pretax_income: float) -> float:
    """Compute the book effective tax rate = tax expense / pre-tax income.

    Args:
        income_tax_expense: Provision for income taxes.
        pretax_income: Income before taxes.

    Returns:
        Effective tax rate as a decimal (``0.0`` if pre-tax income is zero).
    """
    return safe_divide(income_tax_expense, pretax_income)
