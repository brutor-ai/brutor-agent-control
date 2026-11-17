# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a090_ai_assets — ai_assets migration drift suite

Pins the migration file shape: revision chain, table + columns,
JSONB tags, the natural-key unique constraint, the four indexes, and
the symmetric downgrade. Mirrors test_a070_compliance_mapping_migration.py
(textual / regex assertions, not a live-DB run).
"""

from __future__ import annotations





# ---------------------------------------------------------------------------
# File + chain shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Column invariants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Vocabulary documentation parity — the migration's column comments must
# mention the same values the model @validates enforces.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Downgrade symmetry
# ---------------------------------------------------------------------------
