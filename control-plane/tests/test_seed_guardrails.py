# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the default-guardrails seeding path.

What this covers:
  * `seed_guardrails()` produces a row with the documented defaults
    (PII redact, prompt-injection block @ 0.7, jailbreak block, toxic
    warn @ 0.8, surfaces enabled per the matrix in `seed_guardrails.py`)
  * Idempotency — running it twice does not duplicate the row, and the
    binding to the default resource group is also idempotent
  * Per-check provider columns (per-check provider selectors,
    `pii_categories`, `toxic_content_category_thresholds`,
    `banned_words_match_mode`, `fail_open_overrides`) exist on the seeded
    row and default to None — so a freshly-provisioned tenant gets the
    "all built-in" baseline without any extra configuration
  * The `to_dict()` serialisation surfaces the new fields under
    `builtin.{pii,prompt_injection,jailbreak,toxic_content}.provider`
    and the `fail_open_overrides` top-level key, so the admin UI can
    render the seeded policy correctly on first load

In-memory SQLite + the same REQUIRED_TABLES pattern used elsewhere —
no real DB. Keeps the test fast and CI-safe.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from typing import List

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.guardrail_config import (
    GuardrailConfig,
    resource_group_guardrails,
)
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from app.database.seed_guardrails import seed_guardrails


# Minimum table set for the seed code to run — Tenant + ResourceGroup
# + GuardrailConfig + the join. Plus the three EndUserGroup tables
# because of the lazy=selectin relationship on
# ResourceGroup.end_user_groups; loading any RG triggers a SELECT
# against them.
REQUIRED_TABLES = [
    Tenant.__table__,
    ResourceGroup.__table__,
    GuardrailConfig.__table__,
    resource_group_guardrails,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # Don't enable SQLite FK enforcement here — the model graph
    # references tables we don't load in this seed-focused harness
    # (admin tables, end_user, etc.) and the seed function never
    # exercises cascades, so strict FK checking would only manufacture
    # noise. Production is Postgres which validates FKs natively.

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    db: Session = Factory()
    logger = BrutorLogger("test-seed-guardrails")

    # Minimum fixture data: a tenant + the default resource group the
    # seed binds the policy to.
    tenant = Tenant(
        id="t-test",
        name="test-tenant",
        display_name="Test Tenant",
        primary_contact_email="ops@example.com",
        is_default=True,
    )
    db.add(tenant)
    group = ResourceGroup(
        id="rg-default",
        tenant_id="t-test",
        name="Default",
        description="Default group",
    )
    db.add(group)
    db.commit()

    yield db, logger
    db.close()


# ---------------------------------------------------------------------------
# Defaults — documented matrix from seed_guardrails.py
# ---------------------------------------------------------------------------


class TestSeedDefaults:
    def test_creates_row_with_default_config(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()

        config = (
            db.query(GuardrailConfig)
            .filter(GuardrailConfig.tenant_id == "t-test")
            .one()
        )
        assert config.id == "guardrail-default-t-test"
        assert config.name == "Default Guardrails"
        assert config.is_default is True
        assert config.priority == 0
        assert config.enabled is True

    def test_pii_default_is_redact_with_built_in_provider(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.pii_detection_enabled is True
        assert c.pii_action == "redact"

    def test_prompt_injection_default_is_block_at_07_sensitivity(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.prompt_injection_enabled is True
        assert c.prompt_injection_action == "block"
        assert c.prompt_injection_sensitivity == pytest.approx(0.7)

    def test_jailbreak_default_is_block(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.jailbreak_detection_enabled is True
        assert c.jailbreak_action == "block"

    def test_toxic_content_default_is_warn_at_08_threshold(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.toxic_content_enabled is True
        assert c.toxic_content_action == "warn"
        assert c.toxic_content_threshold == pytest.approx(0.8)

    def test_secrets_default_is_redact_with_low_fp_categories(self, harness):
        # Secrets detection MUST ship on for new tenants. It's the
        # highest-value defensive check (catches AWS / GitHub / OpenAI
        # / JWT / PEM key leakage) and the first thing any security
        # auditor asks about. `redact` is the chosen action so an
        # operator who quotes a key while triaging an incident isn't
        # blocked outright — the audit row still records the hit.
        # Categories default to DEFAULT_SECRETS_CATEGORIES, which
        # excludes the noisy `generic_high_entropy` matcher.
        from app.database.models.guardrail_config import DEFAULT_SECRETS_CATEGORIES

        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.secrets_detection_enabled is True, (
            "Default Guardrails MUST seed secrets detection ON — it's the "
            "audit-recommended posture and the most-asked-about defensive check"
        )
        assert c.secrets_action == "redact"
        # generic_high_entropy is explicitly excluded by default — it's
        # the false-positive-prone category. If it ever sneaks into the
        # default set, this test catches it.
        assert "generic_high_entropy" not in (c.secrets_categories or []), (
            "generic_high_entropy must NOT be in default secrets categories — "
            "it false-positives on legitimate hashes / IDs"
        )
        # Sanity: the seeded categories match DEFAULT_SECRETS_CATEGORIES
        # exactly. Drift here would mean the seed picked a custom set
        # that the docs / admin UI haven't been updated to match.
        assert set(c.secrets_categories or []) == set(DEFAULT_SECRETS_CATEGORIES)

    def test_banned_words_and_patterns_start_empty(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.banned_words == []
        assert c.banned_patterns == []
        assert c.providers == []

    def test_surface_matrix_matches_documented_defaults(self, harness):
        # Per-mode LLM surfaces: every input mode on by default for
        # new tenants; chat_output stays off (operators opt into
        # output scanning). MCP / Skill input + output on. A2A off
        # by default (operator opts in once they want delegation
        # traffic scanned).
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.chat_input_enabled is True
        assert c.chat_output_enabled is False
        assert c.embeddings_input_enabled is True
        assert c.mcp_input_enabled is True
        assert c.mcp_output_enabled is True
        assert c.skill_input_enabled is True
        assert c.skill_output_enabled is True
        # A2A surfaces default false — the seed shouldn't enable them.
        assert c.a2a_inbound_enabled is False
        assert c.a2a_outbound_enabled is False

    def test_performance_defaults(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.max_check_timeout_ms == pytest.approx(2000.0)
        # Seeded configs fail CLOSED. A default that ships fail-open
        # means every new tenant starts with governance that stops enforcing
        # the moment a provider hiccups, and nothing says so.
        assert c.fail_open is False
        assert c.log_all_checks is False


# ---------------------------------------------------------------------------
# Per-check provider selectors + tuning columns
# ---------------------------------------------------------------------------


class TestSeedP9Columns:
    """The default seed should leave the new selector / tuning columns
    unset (None) so the runtime falls back to the documented defaults
    (built_in for prompt-injection / jailbreak / pii, openai_moderation
    for toxic). Operators opt in via the admin UI; the seed shouldn't
    pre-pin them."""

    def test_pii_provider_and_categories_default_to_none(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.pii_provider is None, (
            "pii_provider should be None at seed time so the runtime "
            "uses built_in by default (admin can pin it explicitly later)"
        )
        assert c.pii_categories is None, (
            "Null pii_categories runs all 12 built-in categories"
        )

    def test_pi_and_jailbreak_provider_selectors_default_to_none(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.prompt_injection_provider is None
        assert c.jailbreak_provider is None

    def test_toxic_content_provider_and_thresholds_default(self, harness):
        # Seed sets toxic_content_provider="built_in" explicitly so
        # tenants ship with a working profanity wordlist with no
        # external API key. category_thresholds stays None — falls
        # back to the global toxic_content_threshold.
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.toxic_content_provider == "built_in"
        assert c.toxic_content_category_thresholds is None

    def test_banned_match_mode_and_fail_open_overrides_default_to_substring_and_none(
        self, harness
    ):
        # `banned_words_match_mode` carries a column-level default of
        # 'substring' (back-compat for installs predating word_boundary).
        # `fail_open_overrides` has no column default → starts as None.
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        assert c.banned_words_match_mode == "substring"
        assert c.fail_open_overrides is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestSeedIdempotent:
    def test_second_call_does_not_create_duplicate(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        rows: List[GuardrailConfig] = (
            db.query(GuardrailConfig)
            .filter(GuardrailConfig.tenant_id == "t-test")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == "guardrail-default-t-test"

    def test_second_call_does_not_duplicate_group_binding(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        group = db.query(ResourceGroup).filter(ResourceGroup.id == "rg-default").one()
        # `guardrail_configs` is `lazy="dynamic"` → AppenderQuery, not
        # a list. Use `.count()` rather than `len()`.
        assert group.guardrail_configs.count() == 1

    def test_reseed_does_not_stomp_operator_edits(self, harness):
        # Seeds are create-once: provisioning seeds must never overwrite a
        # row the operator has since edited via the admin UI. This pins
        # that invariant — a regression that turns the seed into an upsert
        # would silently revert tenant policy on every restart, which is a
        # governance-critical data-loss bug.
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()

        cfg = (
            db.query(GuardrailConfig)
            .filter(GuardrailConfig.tenant_id == "t-test")
            .one()
        )
        # Operator narrows the policy after provisioning: flip PII to block,
        # turn chat_output scanning off, raise the toxic threshold, rename.
        cfg.pii_action = "block"
        cfg.chat_output_enabled = False
        cfg.toxic_content_threshold = 0.95
        cfg.name = "Edited by operator"
        db.commit()

        # Re-provision (e.g. container restart re-runs seeding).
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()

        reloaded = (
            db.query(GuardrailConfig)
            .filter(GuardrailConfig.tenant_id == "t-test")
            .one()
        )
        assert reloaded.pii_action == "block", "re-seed reverted pii_action"
        assert reloaded.chat_output_enabled is False, "re-seed re-enabled chat_output"
        assert reloaded.toxic_content_threshold == 0.95, "re-seed reverted threshold"
        assert reloaded.name == "Edited by operator", "re-seed reverted name"


# ---------------------------------------------------------------------------
# Group binding
# ---------------------------------------------------------------------------


class TestSeedGroupBinding:
    def test_seeded_config_is_bound_to_default_group(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        group = db.query(ResourceGroup).filter(ResourceGroup.id == "rg-default").one()
        # `guardrail_configs` is `lazy="dynamic"` → AppenderQuery, not
        # a list. Use `.count()` rather than `len()`.
        configs = group.guardrail_configs.all()
        assert len(configs) == 1
        assert configs[0].id == "guardrail-default-t-test"

    def test_seed_with_missing_group_id_still_creates_config(self, harness):
        # The seed currently swallows the missing-group case (the
        # branch is `elif default_group:` — neither arm runs). Pin the
        # observable behaviour: the policy row is still created so the
        # admin UI can show it; the operator can wire the binding by
        # hand later.
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-does-not-exist", logger)
        db.commit()
        c = (
            db.query(GuardrailConfig)
            .filter(GuardrailConfig.tenant_id == "t-test")
            .one()
        )
        assert c.id == "guardrail-default-t-test"


# ---------------------------------------------------------------------------
# Serialisation surface — to_dict() must surface the provider fields the UI reads
# ---------------------------------------------------------------------------


class TestSeedToDictSurfacesP9Fields:
    """Pydantic-shape sanity. The admin UI reads `builtin.<check>.provider`
    and `fail_open_overrides` from `to_dict()` to render selectors and
    overrides. Confirm the seeded row produces a dict where those keys
    exist (with default values) so a freshly-provisioned tenant doesn't
    break the form."""

    def test_to_dict_contains_provider_keys_for_each_check(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        d = c.to_dict()
        builtin = d["builtin"]
        assert builtin["pii"]["provider"] == "built_in"
        assert builtin["prompt_injection"]["provider"] == "built_in"
        assert builtin["jailbreak"]["provider"] == "built_in"
        # Seeded default is built_in — no API key
        # required out-of-the-box, profanity wordlist bundled with the
        # detector. Operators can switch to openai_moderation via UI.
        assert builtin["toxic_content"]["provider"] == "built_in"

    def test_to_dict_contains_pii_categories_null_default(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        d = c.to_dict()
        assert d["builtin"]["pii"]["categories"] is None

    def test_to_dict_contains_banned_match_mode_substring_default(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        d = c.to_dict()
        assert d["builtin"]["banned_words_match_mode"] == "substring"

    def test_to_dict_contains_fail_open_overrides_key(self, harness):
        db, logger = harness
        seed_guardrails(db, "t-test", "rg-default", logger)
        db.commit()
        c = db.query(GuardrailConfig).one()
        d = c.to_dict()
        # The key MUST exist even when null so the UI form binding doesn't
        # have to do a `key in dict` dance on every load.
        assert "fail_open_overrides" in d
        assert d["fail_open_overrides"] is None
