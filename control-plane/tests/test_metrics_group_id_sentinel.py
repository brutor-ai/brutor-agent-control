# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression: Mission Control's "All AI Systems" selector sends the sentinel
"all" (and can leak "undefined"/"" from the client). Those are not real
Resource-Group ids; treating them as a scope filter matched nothing and
silently zeroed the Cost / Governance / Agents tabs. `_clean_group_id` must
normalise every sentinel to None (no scope) while passing real ids through.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.metrics import _clean_group_id


@pytest.mark.parametrize(
    "sentinel",
    ["all", "All", "ALL", "  all  ", "", "   ", "undefined", "null", "none", "*", None],
)
def test_sentinels_normalise_to_none(sentinel):
    assert _clean_group_id(sentinel) is None


@pytest.mark.parametrize(
    "real",
    ["group-01KX85SVQZZHE6DTK0FWJ1ZHQ2", "rg-x", "ai_system-123", "Sales"],
)
def test_real_group_ids_pass_through(real):
    assert _clean_group_id(real) == real
