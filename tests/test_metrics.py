"""Tests for the free cash flow and supporting formulas in ``src.metrics``."""

from __future__ import annotations

import math

import pytest

from src import metrics
from src.schemas import FinancialStatementSchema


def test_unlevered_fcf_hand_calculation() -> None:
    # EBIT 100 x (1 - 25%) = 75; + D&A 20 - Capex 30 - dNWC 10 = 55
    assert metrics.unlevered_free_cash_flow(
        ebit=100, tax_rate=0.25, depreciation_and_amortization=20, capex=30, change_in_nwc=10
    ) == pytest.approx(55.0)


def test_nwc_increase_reduces_fcf_and_release_increases_it() -> None:
    kwargs = {"ebit": 100, "tax_rate": 0.25, "depreciation_and_amortization": 20, "capex": 30}
    base = metrics.unlevered_free_cash_flow(change_in_nwc=0, **kwargs)
    assert metrics.unlevered_free_cash_flow(change_in_nwc=15, **kwargs) == base - 15
    assert metrics.unlevered_free_cash_flow(change_in_nwc=-15, **kwargs) == base + 15


def test_fcf_on_apple_fy2015_matches_independent_calculation(
    apple: FinancialStatementSchema,
) -> None:
    # Figures straight from the filing (USD millions).
    ebit, tax, pretax, da, capex = 71_230, 19_121, 72_515, 11_257, 11_247
    wc_lines = [611, -238, -3_735, -179, 5_400, 1_042, 8_746]  # CFS working-capital rows
    change_in_nwc = -sum(wc_lines)                            # cash released => NWC fell
    expected = ebit * (1 - tax / pretax) + da - capex - change_in_nwc

    assert apple.cash_flow_statement(2015).change_in_nwc == pytest.approx(-11_647)
    assert apple.unlevered_fcf(2015) == pytest.approx(expected)
    assert apple.unlevered_fcf(2015) == pytest.approx(64_105, abs=1)


def test_ebitda_combines_income_statement_and_cash_flow(apple: FinancialStatementSchema) -> None:
    assert apple.ebitda(2015) == 71_230 + 11_257


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"capex": -30}, "capex must be passed as a positive outflow"),
        ({"depreciation_and_amortization": -1}, "must be positive"),
        ({"tax_rate": 1.0}, "tax_rate"),
        ({"ebit": math.nan}, "finite"),
    ],
)
def test_fcf_rejects_bad_inputs(kwargs: dict[str, float], message: str) -> None:
    base = {"ebit": 100.0, "tax_rate": 0.25, "depreciation_and_amortization": 20.0,
            "capex": 30.0, "change_in_nwc": 10.0}
    with pytest.raises(ValueError, match=message):
        metrics.unlevered_free_cash_flow(**{**base, **kwargs})


def test_safe_divide_and_effective_tax_rate() -> None:
    assert metrics.safe_divide(1, 0) == 0.0
    assert metrics.effective_tax_rate(19_121, 72_515) == pytest.approx(0.26368, abs=1e-5)


def test_historical_metrics_frame(apple: FinancialStatementSchema) -> None:
    frame = apple.historical_metrics()
    assert list(frame.columns) == [2013, 2014, 2015]
    assert frame.loc["EBITDA margin %", 2015] == pytest.approx(82_487 / 233_715)
    assert frame.loc["Revenue growth", 2015] == pytest.approx(233_715 / 182_795 - 1)
