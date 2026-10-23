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


def npv(rate: float, cash_flows: list[float]) -> float:
    """Net present value of annual cash flows, the first at t = 0.

    Args:
        rate: Discount rate per period as a decimal (> -1).
        cash_flows: Cash flows at t = 0, 1, 2, ...

    Returns:
        NPV at ``rate``.
    """
    return sum(cf / (1.0 + rate) ** t for t, cf in enumerate(cash_flows))


def irr(cash_flows: list[float], low: float = -0.99, high: float = 10.0,
        tolerance: float = 1e-10, max_iterations: int = 500) -> float:
    """Internal rate of return by bisection (same convention as Excel ``IRR``).

    Bisection is slower than Newton's method but cannot diverge, which matters when
    sensitivity tables push returns towards -100%.

    Args:
        cash_flows: Annual cash flows from t = 0; needs at least one sign change.
        low: Lower bound of the search interval.
        high: Upper bound of the search interval.
        tolerance: Convergence tolerance on the rate.
        max_iterations: Iteration cap.

    Returns:
        The IRR as a decimal.

    Raises:
        ValueError: If the cash flows have no sign change, or no root lies in the
            interval ``[low, high]``.
    """
    if not (any(cf < 0 for cf in cash_flows) and any(cf > 0 for cf in cash_flows)):
        raise ValueError("IRR needs at least one negative and one positive cash flow")
    f_low = npv(low, cash_flows)
    if f_low * npv(high, cash_flows) > 0:
        raise ValueError(f"No IRR in [{low:.0%}, {high:.0%}] for cash flows {cash_flows}")
    for _ in range(max_iterations):
        mid = (low + high) / 2.0
        f_mid = npv(mid, cash_flows)
        if abs(high - low) < tolerance or f_mid == 0.0:
            return mid
        if f_low * f_mid < 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2.0


def effective_tax_rate(income_tax_expense: float, pretax_income: float) -> float:
    """Compute the book effective tax rate = tax expense / pre-tax income.

    Args:
        income_tax_expense: Provision for income taxes.
        pretax_income: Income before taxes.

    Returns:
        Effective tax rate as a decimal (``0.0`` if pre-tax income is zero).
    """
    return safe_divide(income_tax_expense, pretax_income)
