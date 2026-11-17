# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a070_compliance_mapping — compliance-mapping migration drift suite

Pins the migration file shape, column types, default values, and
the framework-key vocabulary that the migration's column comment
promises operators. Mirror of the a067-a069 drift suites.

Specifically guards:

  * Migration file exists at the expected path.
  * Revision chain follows a069_data_residency.
  * All four columns are added (and dropped on downgrade):
    tenants.compliance_profile,
    resource_groups.eu_ai_act_risk_tier,
    resource_groups.phi_resource,
    proxy_logs.compliance_tags.
  * compliance_profile is JSONB (not JSON), so tag lists stay compact and
    indexable, and carries an empty-frameworks server_default.
  * eu_ai_act_risk_tier has a CHECK constraint matching the four
    Pydantic-enforced values.
  * phi_resource defaults to false (operator opt-in).
  * Two indexes are created on proxy_logs.compliance_tags: a GIN
    index for JSONB-containment queries, and a
    (tenant_id, timestamp) partial index for dashboard time-range
    scans.
  * Pydantic FRAMEWORK_KEYS + EU_AI_ACT_RISK_TIERS match the
    migration comment's vocabulary — drift between Python and
    DB-level CHECK would mean API-accepted input rejected by SQL.
"""

from __future__ import annotations



from app.api.v1.models.admin.compliance import FRAMEWORK_KEYS


# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Column type / default invariants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Python ↔ SQL vocabulary parity
# ---------------------------------------------------------------------------


class TestPythonSqlVocabularyParity:


    def test_known_pydantic_framework_set_pinned(self):
        # Pin the five-framework set explicitly so adding a regime
        # is a deliberate three-place change (this test, the
        # tagging engine, the export endpoints).
        assert FRAMEWORK_KEYS == {
            "gdpr_art_30",
            "soc2",
            "hipaa",
            "eu_ai_act",
            "iso_42001",
        }, (
            f"FRAMEWORK_KEYS drift: {FRAMEWORK_KEYS}. Adding a "
            f"regime requires: (1) updating this set, (2) adding "
            f"a derivation function in app/services/compliance/"
            f"tagging.py, (3) adding an export endpoint."
        )
