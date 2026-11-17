# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the LLM retirement metadata flow:
  1. Seed pass-through — provider data with `retire_date` lands on
     the LLModel row, and the reminder defaults to retire_date-14d.
  2. Retirement warner — emits the right alert at the right time,
     is idempotent, and raises severity once the date is past.

Live e2e is out of scope; this file targets the deterministic
schema + service behaviour.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.init_llms import _retirement_reminder_default
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.resource_group import ResourceGroup, resource_group_llm_models
from app.database.models.system_alert import SystemAlert
from app.database.models.tenant import Tenant
from app.services.llm.retirement_warner import run_once


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    # `SystemAlert.context` is JSONB on production Postgres but
    # SQLite can't render that. Swap it to plain JSON at
    # create-table time and restore on exit — matches the pattern
    # in `tests/test_capability_diff.py`. The column behaves the
    # same for our purposes (write a dict, read a dict).
    import sqlalchemy as sa

    original_context_type = SystemAlert.__table__.c.context.type
    SystemAlert.__table__.c.context.type = sa.JSON()
    try:
        engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(
            engine,
            tables=[
                Tenant.__table__,
                LlmProviderCatalog.__table__,
                LlmProvider.__table__,
                # ResourceGroup + M2M needed because `to_response`
                # lazy-loads `model.resource_groups` to compute
                # `is_global`. Without the table the lazy load
                # raises and the surface test can't run.
                ResourceGroup.__table__,
                LLModel.__table__,
                resource_group_llm_models,
                SystemAlert.__table__,
            ],
        )
    finally:
        SystemAlert.__table__.c.context.type = original_context_type
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(
        Tenant(
            id="t1",
            name="t1",
            display_name="t1",
            primary_contact_email="t1@example.invalid",
        )
    )
    session.add(LlmProvider(id="p-openai", tenant_id="t1", key="openai", display_name="OpenAI"))
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_model(
    db,
    *,
    model_id: str = "llm-test",
    model_name: str = "test-model",
    enabled: bool = True,
    retire_date: date | None = None,
    retirement_reminder_date: date | None = None,
    retirement_replacement_model_name: str | None = None,
) -> LLModel:
    m = LLModel(
        id=model_id,
        tenant_id="t1",
        name=f"Test {model_name}",
        description="test",
        provider_id="p-openai",
        model_name=model_name,
        mode="chat",
        api_url_auto_discovered=True,
        auth_type="api_key",
        default_temperature=0.7,
        default_max_tokens=4096,
        supports_streaming=True,
        supports_function_calling=False,
        supports_vision=False,
        supports_batch=False,
        timeout=60.0,
        enabled=enabled,
        retire_date=retire_date,
        retirement_reminder_date=retirement_reminder_date,
        retirement_replacement_model_name=retirement_replacement_model_name,
    )
    db.add(m)
    db.flush()
    return m


# ---------------------------------------------------------------------------
# 1. Reminder-date defaulting helper
# ---------------------------------------------------------------------------


class TestRetirementReminderDefault:
    """`_retirement_reminder_default` is the seed-pipeline helper
    that picks the operator's warning window. Critical invariants:
      * Explicit value wins.
      * Falls back to retire_date - 14d.
      * Returns None when no retire_date is set."""

    def test_explicit_reminder_wins(self):
        retire = date(2026, 12, 1)
        reminder = date(2026, 10, 1)  # operator overrode to 2-month lead
        out = _retirement_reminder_default({
            "retire_date": retire,
            "retirement_reminder_date": reminder,
        })
        assert out == reminder

    def test_defaults_to_two_weeks_before_retirement(self):
        retire = date(2026, 12, 1)
        out = _retirement_reminder_default({"retire_date": retire})
        assert out == retire - timedelta(days=14)

    def test_returns_none_without_retire_date(self):
        assert _retirement_reminder_default({}) is None
        assert _retirement_reminder_default({"retire_date": None}) is None


# ---------------------------------------------------------------------------
# 2. Retirement warner — alert generation
# ---------------------------------------------------------------------------


class TestRetirementWarner:
    """The warner is the operator-facing safety net. The risk this
    test protects against: a model retires next month, the operator
    doesn't notice, end users start hitting 404s. Each test case
    pins one rung of that ladder."""

    def test_emits_warning_when_reminder_passed(self, db):
        today = date(2026, 6, 1)
        # retire 10 days from "today"; reminder 14 days ago.
        retire = today + timedelta(days=10)
        reminder = today - timedelta(days=14)
        _seed_model(
            db,
            model_id="llm-retiring-soon",
            model_name="dall-e-3",
            retire_date=retire,
            retirement_reminder_date=reminder,
            retirement_replacement_model_name="gpt-image-1-mini",
        )

        inserted = run_once(db, today=today)

        assert len(inserted) == 1
        alert = inserted[0]
        assert alert.severity == "medium"
        assert alert.category == "resource_health"
        assert alert.resource_id == "llm-retiring-soon"
        assert "10 day" in alert.title  # "retires in 10 day(s)"
        assert "gpt-image-1-mini" in alert.message
        assert alert.context["kind"] == "retirement_warning"
        assert alert.context["days_remaining"] == 10

    def test_emits_overdue_when_retire_date_in_past(self, db):
        today = date(2026, 6, 1)
        retire = today - timedelta(days=3)  # retired 3 days ago
        reminder = today - timedelta(days=20)
        _seed_model(
            db,
            model_id="llm-already-retired",
            model_name="dall-e-3",
            retire_date=retire,
            retirement_reminder_date=reminder,
            retirement_replacement_model_name="gpt-image-1-mini",
        )

        inserted = run_once(db, today=today)

        assert len(inserted) == 1
        alert = inserted[0]
        assert alert.severity == "high"
        assert "retired 3 day(s) ago" in alert.title
        assert alert.context["kind"] == "retirement_overdue"
        assert alert.context["days_overdue"] == 3

    def test_no_alert_before_reminder_date(self, db):
        # Retirement is real and scheduled, but the reminder window
        # hasn't opened yet — the warner stays silent. Avoids
        # alert-fatigue on long-lead-time announcements.
        today = date(2026, 6, 1)
        retire = today + timedelta(days=90)
        reminder = today + timedelta(days=10)  # not yet
        _seed_model(
            db,
            model_id="llm-far-future",
            retire_date=retire,
            retirement_reminder_date=reminder,
        )
        assert run_once(db, today=today) == []

    def test_ignores_disabled_models(self, db):
        # A retired model that the operator has already disabled is
        # already handled — no point spamming alerts.
        today = date(2026, 6, 1)
        _seed_model(
            db,
            model_id="llm-disabled",
            enabled=False,
            retire_date=today - timedelta(days=1),
            retirement_reminder_date=today - timedelta(days=15),
        )
        assert run_once(db, today=today) == []

    def test_ignores_models_without_retire_date(self, db):
        _seed_model(db, model_id="llm-no-retirement")
        assert run_once(db, today=date(2026, 6, 1)) == []

    def test_idempotent_within_active_window(self, db):
        # Second sweep on the same day must not produce a second
        # row — the partial-unique index on dedup_key catches it.
        # The warner's job is to keep alerts current, not to spam.
        today = date(2026, 6, 1)
        _seed_model(
            db,
            model_id="llm-dedup",
            retire_date=today + timedelta(days=5),
            retirement_reminder_date=today - timedelta(days=10),
            retirement_replacement_model_name="successor",
        )

        first = run_once(db, today=today)
        second = run_once(db, today=today)

        assert len(first) == 1
        assert second == []
        # Exactly one active alert on the table.
        active = db.query(SystemAlert).filter(SystemAlert.status == "active").all()
        assert len(active) == 1

    def test_handles_multiple_models_in_one_sweep(self, db):
        today = date(2026, 6, 1)
        _seed_model(
            db,
            model_id="llm-a",
            model_name="model-a",
            retire_date=today + timedelta(days=5),
            retirement_reminder_date=today - timedelta(days=2),
        )
        _seed_model(
            db,
            model_id="llm-b",
            model_name="model-b",
            retire_date=today - timedelta(days=1),
            retirement_reminder_date=today - timedelta(days=15),
        )
        inserted = run_once(db, today=today)
        kinds = {a.context["kind"] for a in inserted}
        ids = {a.resource_id for a in inserted}
        assert ids == {"llm-a", "llm-b"}
        assert kinds == {"retirement_warning", "retirement_overdue"}


# ---------------------------------------------------------------------------
# 3. Read path round-trip
# ---------------------------------------------------------------------------


class TestToResponseSurface:
    """Regression for the bug where retirement fields were written
    to the DB but the admin API didn't return them — admin UI showed
    empty Retirement-tab fields for a model with retirement data
    persisted. The cause: `LLModelsManager.to_response()` builds
    the response dict by hand and didn't include the retirement
    keys. This test pins the keys to the dict shape."""

    def test_to_response_includes_all_six_retirement_fields(self, db):
        # Late import — `LLModelsManager` pulls in the encryption
        # service and other heavy deps; isolating import to this
        # test keeps the rest of the file lightweight.
        from unittest.mock import MagicMock
        from app.services.llm.model.llm_models_manager import LLModelsManager

        model = _seed_model(
            db,
            model_id="llm-retired-surface",
            model_name="dall-e-3",
            retire_date=date(2026, 5, 12),
            retirement_reminder_date=date(2026, 4, 28),
            retirement_replacement_model_name="gpt-image-1-mini",
        )
        # Set the other three by hand — `_seed_model` doesn't.
        model.retirement_announced_at = date(2025, 11, 14)
        model.retirement_replacement_provider_key = "openai"
        model.retirement_notes = "Retired per provider announcement."
        db.flush()

        manager = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())
        resp = manager.to_response(model)

        # All six keys present, dates serialised to ISO strings,
        # strings round-tripped intact, None where no value.
        assert resp["retire_date"] == "2026-05-12"
        assert resp["retirement_announced_at"] == "2025-11-14"
        assert resp["retirement_reminder_date"] == "2026-04-28"
        assert resp["retirement_replacement_model_name"] == "gpt-image-1-mini"
        assert resp["retirement_replacement_provider_key"] == "openai"
        assert resp["retirement_notes"] == "Retired per provider announcement."

    def test_to_response_returns_null_when_unset(self, db):
        from unittest.mock import MagicMock
        from app.services.llm.model.llm_models_manager import LLModelsManager
        model = _seed_model(db, model_id="llm-no-retirement-surface")
        manager = LLModelsManager(logger=MagicMock(), encryption_service=MagicMock())
        resp = manager.to_response(model)
        # All six keys present even when the model has no retirement
        # set. Returning the keys (with None) is what the admin UI
        # relies on — missing keys would surface as `undefined` in
        # the form state and confuse the empty-state detection.
        for key in (
            "retire_date",
            "retirement_announced_at",
            "retirement_reminder_date",
            "retirement_replacement_model_name",
            "retirement_replacement_provider_key",
            "retirement_notes",
        ):
            assert key in resp, f"to_response() must always include {key}"
            assert resp[key] is None
