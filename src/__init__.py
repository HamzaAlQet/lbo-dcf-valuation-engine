"""Automated M&A / Private Equity LBO & DCF Valuation Engine.

Modules
-------
- ``schemas``          Pydantic data contracts with built-in accounting identity checks.
- ``extractor``        Ingests SEC 10-K filings and emits a validated ``FinancialStatementSchema``.
- ``metrics``          Pure, unit-tested financial formulas (FCF, NWC, margins).
- ``modeling_engine``  3-statement projection, DCF and LBO (Phase 2).
- ``analyzer``         Sensitivities, scenarios and IC memo generation (Phase 3).
"""

__version__ = "0.1.0"
