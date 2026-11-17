# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a093_mcp_catalog_global mcp_catalog_global migration drift suite

Pins the migration file shape: revision chain (after a092_llm_catalog), the
tenant_id → nullable alter, the in-place dedup SQL (delete-duplicates +
promote-survivor-to-global), the partial UNIQUE index on (slug) WHERE
tenant_id IS NULL, and the symmetric-ish downgrade (drop index + restore NOT
NULL). Mirrors test_a092_llm_catalog_migration.py (textual / regex
assertions, not a live-DB run — the dedup uses Postgres ctid SQL).
"""

from __future__ import annotations




# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Upgrade invariants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------
