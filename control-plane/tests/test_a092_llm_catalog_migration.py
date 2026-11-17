# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a092_llm_catalog llm_catalog migration drift suite

Pins the migration file shape: revision chain, the llm_catalog table +
columns, JSONB capabilities, the (provider_key, model_name) unique
constraint, the three indexes, the llm_model.catalog_id + ai_assets
presentation columns, and the symmetric downgrade. Mirrors
test_a091_ai_asset_factsheets_migration.py (textual / regex assertions,
not a live-DB run).
"""

from __future__ import annotations




# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Column invariants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-table additions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------
