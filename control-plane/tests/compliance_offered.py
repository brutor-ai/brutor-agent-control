# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Test helper: treat named catalogs as offered for one test.

Some tests exercise mechanics (report generation, documentation templates,
profile validation) using a framework that is not offered in the product
(SOC 2 and ISO 42001 are licensed texts not yet checked). The
offering gate itself is pinned by `tests/test_compliance_offering.py`; these
tests mark their sample frameworks offered so they keep testing what they
were written for.
"""

from __future__ import annotations

from dataclasses import replace

from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import INSTRUMENT_FINAL


def offer(monkeypatch, *framework_ids: str) -> None:
    for fid in framework_ids:
        monkeypatch.setitem(
            CATALOGS, fid,
            replace(CATALOGS[fid], verified_against_instrument=True, instrument_status=INSTRUMENT_FINAL),
        )
