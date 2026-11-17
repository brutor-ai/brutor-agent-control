# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a094_llm_prune_catalog_defaults llm_model prune + llm_catalog import-defaults migration drift suite

Pins the migration file shape: revision chain, the 8 dropped llm_model
columns (+ the idp_id index), the 20 added llm_catalog columns, and the
symmetric downgrade. Mirrors test_a092_llm_catalog_migration.py (textual /
regex assertions, not a live-DB run).
"""

from __future__ import annotations




# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1. Dropped llm_model columns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. Added llm_catalog columns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------
