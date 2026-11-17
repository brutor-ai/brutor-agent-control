# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The badge carries its basis (plan item "qualify the badge")

"Assured" alone read as a verdict on the system. What is asserted is envelope
conformance over observed runs, and the badge is what gets screenshotted, so
the label says so and a one-sentence basis note travels with it.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.services.ai_system_assurance import _assurance_status


@pytest.mark.parametrize(
    "status,basis,code,label_has",
    [
        ("assured", "signed_envelope", "assured", "operating envelope"),
        ("assured_with_exceptions", "signed_envelope", "assured_with_exceptions", "operating envelope"),
        ("assured", "observed_history", "conditionally_assured", "observed history"),
        ("violations", "signed_envelope", "violations", "Not assured"),
        ("insufficient_evidence", "signed_envelope", "insufficient_evidence", "insufficient"),
    ],
)
def test_label_names_its_basis(status, basis, code, label_has):
    out = _assurance_status({"status": status, "basis": basis})
    assert out["code"] == code
    assert label_has.lower() in out["label"].lower()


def test_every_status_carries_a_basis_note_that_disclaims_correctness():
    for status in ("assured", "assured_with_exceptions"):
        out = _assurance_status({"status": status, "basis": "signed_envelope"})
        assert "Correctness is not evaluated" in out["basis_note"]
    out = _assurance_status({"status": "assured", "basis": "observed_history"})
    assert "not a signed envelope" in out["basis_note"]
