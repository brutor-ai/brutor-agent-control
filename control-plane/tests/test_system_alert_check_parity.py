# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# `system_alerts` CHECK parity — the fresh-install trap

Fresh deployments build their schema with `Base.metadata.create_all()`,
**not** by running the migration chain. So there are two independent
definitions of the `ck_system_alerts_category` constraint: the one in the
model, and the one each migration writes.

When `a132_liveness_expectations` widened the constraint to admit `ai_system_liveness`, the
migration was updated and the model's hard-coded literal was not. The
consequence would have been invisible in every way that matters: existing
databases worked, CI worked (its fixture came from a migrated DB), and
only a *brand-new* install would reject every liveness alert — from inside
a background sweeper, where nothing surfaces the failure.

The model now derives its CHECK from the `CATEGORIES` tuple. These tests
make sure it stays derived, and that both halves keep agreeing.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import re
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.system_alert import CATEGORIES, SEVERITIES, STATUSES, SystemAlert


def _constraint(name: str) -> CheckConstraint:
    for c in SystemAlert.__table__.constraints:
        if getattr(c, "name", None) == name:
            return c
    raise AssertionError(f"constraint {name} not found on the model")


class TestModelCheckMatchesTheTuples:
    @pytest.mark.parametrize("category", CATEGORIES)
    def test_every_category_is_permitted_by_the_model_check(self, category: str):
        assert f"'{category}'" in str(_constraint("ck_system_alerts_category").sqltext)

    def test_check_admits_nothing_the_tuple_omits(self):
        text = str(_constraint("ck_system_alerts_category").sqltext)
        listed = set(re.findall(r"'([a-z_]+)'", text))
        assert listed == set(CATEGORIES)

    @pytest.mark.parametrize("severity", SEVERITIES)
    def test_severities_match(self, severity: str):
        assert f"'{severity}'" in str(_constraint("ck_system_alerts_severity").sqltext)

    @pytest.mark.parametrize("status", STATUSES)
    def test_statuses_match(self, status: str):
        assert f"'{status}'" in str(_constraint("ck_system_alerts_status").sqltext)

    def test_check_is_derived_not_restated(self):
        # A literal list is what caused the liveness divergence. If someone
        # replaces the comprehension with a hard-coded string, the next
        # category added will break fresh installs only.
        src = (
            Path(__file__).resolve().parents[1]
            / "app" / "database" / "models" / "system_alert.py"
        ).read_text()
        assert 'name="ck_system_alerts_category"' in src
        assert 'f"\'{c}\'" for c in CATEGORIES' in src


class TestWorkloadCategoriesArePresent:
    """The AI System workload categories. Each is written by a Rust background
    task; a rejection there is silent."""

    @pytest.mark.parametrize(
        "category",
        [
            "ai_system_liveness",
            "ai_system_behaviour",
            "ai_system_cost",
            "ai_system_dependency",
        ],
    )
    def test_present(self, category: str):
        assert category in CATEGORIES


class TestFreshInstallAcceptsEveryCategory:
    """The end-to-end proof: build the table the way a fresh install does
    and insert one alert per category."""

    def test_create_all_then_insert_each_category(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        Base.metadata.create_all(bind=engine, tables=[SystemAlert.__table__])
        Factory = sessionmaker(bind=engine, future=True)
        s = Factory()
        try:
            for i, category in enumerate(CATEGORIES):
                s.add(
                    SystemAlert(
                        id=f"sysalert-{i}",
                        tenant_id="t1",
                        severity="medium",
                        category=category,
                        source="test",
                        title=f"t{i}",
                        message="m",
                        status="active",
                        occurrence_count=1,
                        dedup_key=f"t1|{category}|test|none",
                    )
                )
            s.commit()
            assert s.query(SystemAlert).count() == len(CATEGORIES)
        finally:
            s.close()
            engine.dispose()
