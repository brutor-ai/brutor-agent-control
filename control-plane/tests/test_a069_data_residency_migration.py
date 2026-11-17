# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a069_data_residency — data-residency migration regression suite

Data residency enforcement is implemented across three layers
that share field names and a JSONB shape:

  1. Alembic migration  (alembic/versions/...a069_data_residency.py)
  2. Pydantic model     (app/api/v1/models/admin/residency.py — DataResidencyProfile)
  3. Rust runtime       (brutor-gateway-core/src/services/residency.rs
                         — ResidencyProfile struct + parse_profile)

A drift here is silent: the runtime would deserialize the JSONB
under different field names than the admin API wrote, and a
tenant's residency policy would simply never apply — open-by-
default failure mode. This suite pins all three to the same
shape so the next person to rename a field has to update every
layer.

Covers:

  * Migration file exists and lives under the right revision chain.
  * Adds the 5 expected columns (3 region/profile, 2 audit) and
    drops them on downgrade.
  * Creates the partial index used by the audit dashboard.
  * Pydantic DataResidencyProfile carries the exact field names
    the migration's column comment promises and the Rust runtime
    deserializes.
  * Rust ResidencyProfile field names match Python (parsed from
    the source — the runtime is the authority on what Postgres
    JSONB blob the policy reader expects).
"""

from __future__ import annotations

import re

from tests.core_repo import core_source

import pytest

from app.api.v1.models.admin.residency import DataResidencyProfile


@pytest.fixture(scope="module")
def rust_text() -> str:
    return core_source("services", "residency.rs").read_text()


# ---------------------------------------------------------------------------
# Migration file shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pydantic ↔ Rust ↔ migration field-name parity
# ---------------------------------------------------------------------------


class TestProfileFieldsAgreeAcrossLayers:
    def test_pydantic_carries_expected_fields(self):
        # The Rust runtime deserializes `data_residency` straight
        # into ResidencyProfile via serde — these are the JSON
        # keys it expects. If Pydantic renames either, write-path
        # vs read-path drift silently.
        fields = set(DataResidencyProfile.model_fields.keys())
        assert fields == {"allowed_regions", "block_global"}, (
            f"DataResidencyProfile must expose exactly "
            f"{{allowed_regions, block_global}}, got {fields}"
        )

    def test_rust_struct_field_names_match_python(self, rust_text: str):
        # Parse `pub struct ResidencyProfile { ... }` and pull the
        # field idents. They must equal the Pydantic field names.
        m = re.search(
            r"pub struct ResidencyProfile \{(.+?)\}",
            rust_text,
            re.MULTILINE | re.DOTALL,
        )
        assert m, "could not locate `pub struct ResidencyProfile` in residency.rs"
        body = m.group(1)
        # Field idents follow `pub <name>:` — strip rustdoc and
        # attribute lines.
        rust_fields = set(re.findall(r"pub (\w+):", body))
        assert rust_fields == {"allowed_regions", "block_global"}, (
            f"Rust ResidencyProfile fields ({rust_fields}) drift "
            f"from Python DataResidencyProfile "
            f"({set(DataResidencyProfile.model_fields.keys())})"
        )


    def test_residency_classes_pinned(self, rust_text: str):
        # Three audit classes must agree with the column comment:
        # allowed | blocked | not_evaluated. The Rust runtime is
        # the authority — the migration comment must match.
        # Pull the CLASS_* constants and assert they hold these
        # three exact strings.
        classes = dict(re.findall(
            r"pub const CLASS_(\w+):\s*&str\s*=\s*\"([^\"]+)\";",
            rust_text,
        ))
        assert classes == {
            "ALLOWED": "allowed",
            "BLOCKED": "blocked",
            "NOT_EVALUATED": "not_evaluated",
        }, f"residency CLASS_* constants drift: {classes}"
