# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The registry rows say what the obligation catalogs say.

The seed carried its own copies of each framework's title, reference,
verification flag and deadline rules, and they drifted from the catalogs:
Colorado's row named a repealed law with a 90-day rule the reenacted law does
not have, eight references differed, and the verified flags did not match.
Title, reference and verification are now taken from the
catalog when seeding; deadline rules keep the seed's shape (a classification
key the catalog does not carry) and must equal the catalog's values.
"""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.compliance_framework import FRAMEWORK_SEED
from app.database.seed_compliance_frameworks import _from_catalog
from app.services.compliance import offering

UNIT = {"hours": timedelta(hours=1), "days": timedelta(days=1)}


def _seed_deadlines(rules: dict) -> list:
    out = []
    for per_class in rules.values():
        for name, amount in per_class.items():
            unit = name.rsplit("_", 1)[1]
            out.append(amount * UNIT[unit])
    return sorted(out)


@pytest.mark.parametrize("spec", FRAMEWORK_SEED, ids=lambda s: s["id"])
def test_the_seeded_row_matches_its_catalog(spec):
    catalog = offering.any_catalog(spec["id"])
    if catalog is None:
        pytest.skip("no catalog for this registry row")
    row = _from_catalog(spec)
    assert row["title"] == catalog.title
    assert row["reference"] == catalog.reference
    assert row["verified_against_instrument"] is offering.is_offered(catalog)
    assert _seed_deadlines(spec["deadline_rules"]) == sorted(catalog.deadline_rules.values()), (
        f"{spec['id']}: seed deadline rules differ from the catalog's"
    )
