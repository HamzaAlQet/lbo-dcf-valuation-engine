"""Shared pytest fixtures.

The Apple FY2015 10-K is parsed once per test session (PDF text extraction takes a
few seconds); tests that need to break an identity work on a deep-copied ``dict``
dump of that schema, so the cached object is never mutated.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from src.extractor import TenKExtractor
from src.schemas import FinancialStatementSchema

ROOT = Path(__file__).resolve().parents[1]
APPLE_10K = ROOT / "data" / "raw" / "apple_10k_fy2015.pdf"
SYNTHETIC_10K = Path(__file__).parent / "fixtures" / "synthetic_10k.txt"


@pytest.fixture(scope="session")
def apple() -> FinancialStatementSchema:
    """Validated schema extracted from Apple's FY2015 10-K."""
    if not APPLE_10K.is_file():
        pytest.skip(f"Sample filing not found at {APPLE_10K}")
    return TenKExtractor(APPLE_10K, ticker="AAPL").extract()


@pytest.fixture
def apple_payload(apple: FinancialStatementSchema) -> dict[str, Any]:
    """A mutable deep copy of the Apple schema as a plain dict."""
    return copy.deepcopy(apple.model_dump())


@pytest.fixture(scope="session")
def synthetic_text() -> str:
    """Text of a small, internally consistent synthetic 10-K."""
    return SYNTHETIC_10K.read_text(encoding="utf-8")
