# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a081_system_ops_console — system operations console migration

Locks the migration's STRUCTURAL shape against accidental drift:

  * Migration file exists and lives in the linear chain after a080_drop_legacy_llm_surfaces.
  * The three new schema pieces are present in the upgrade text
    (`system_operation_log` table, `scheduler_runtime_state` table,
    `tenants.maintenance_mode` column).
  * Indexes the operation-log query patterns depend on are declared
    (composite (tenant_id, created_at), composite (actor, created_at),
    composite (action, created_at), and the GIN over payload).
  * The downgrade reverses exactly the upgrade — same tables and
    columns dropped, same CHECK constraint name.
  * Revision slug fits `alembic_version.version_num VARCHAR(32)`
    (the a080_drop_legacy_llm_surfaces outage taught us this one — see the slug-shortening
    fix on that revision).

These are textual / regex assertions, NOT a live-DB run. The live-DB
path is exercised by the existing alembic-history test that walks
upgrade→downgrade→upgrade across every revision; the contract here is
"if someone edits this file we catch the obvious mistakes."
"""

from __future__ import annotations




# ---------------------------------------------------------------------------
# Revision chain + naming
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# system_operation_log
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Index coverage — the operation-log query patterns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scheduler_runtime_state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# tenants.maintenance_mode + companions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------
