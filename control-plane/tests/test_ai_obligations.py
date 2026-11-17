# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Regulatory obligations — catalog + applicability computation

Expected values are hand-derived from the Act and the Digital Omnibus
(Reg. (EU) 2026/1744), never from the code under test. The honesty
rules get the closest attention:

  * an undeclared fact yields `undetermined`, never a guess — an
    undeclared role cannot rule an obligation out;
  * applicability is DATED (`applies_from` + `in_force`), never a
    compliance boolean;
  * rules the platform cannot evaluate (Annex I embedment, legacy
    placement dates) say so via `needs_review` instead of pretending.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import date

import pytest

from app.services.ai_obligations import (
    EU_AI_ACT,
    Obligation,
    SystemContext,
    evaluate,
)


def get_ob(key: str) -> Obligation:
    return next(o for o in EU_AI_ACT.obligations if o.key == key)


def run(key: str, ctx: SystemContext, today: date = date(2026, 8, 20)):
    return evaluate(get_ob(key), ctx, today=today)


class TestCatalogDates:
    """The Omnibus timeline, pinned. When the next omnibus moves these,
    this test is the checklist of what to re-verify."""

    EXPECTED = {
        "art4-ai-literacy": "2025-02-02",
        "art5-prohibitions": "2025-02-02",
        "art5-prohibitions-2026": "2026-12-02",
        "art9-risk-management": "2027-12-02",
        "art10-data-governance": "2027-12-02",
        "art11-technical-documentation": "2027-12-02",
        "art12-record-keeping": "2027-12-02",
        "art13-transparency-to-deployers": "2027-12-02",
        "art14-human-oversight": "2027-12-02",
        "art15-accuracy-robustness": "2027-12-02",
        "art17-quality-management": "2027-12-02",
        "art19-log-retention-provider": "2027-12-02",
        # Composite classification (Art 6) and supplier agreements (Art 25(4)).
        "art6-composite-classification": "2027-12-02",
        "art25-4-supplier-agreements": "2027-12-02",
        "art26-1-use-per-instructions": "2027-12-02",
        "art26-2-oversight-assignment": "2027-12-02",
        "art26-5-monitoring-and-suspend": "2027-12-02",
        "art26-6-log-retention-deployer": "2027-12-02",
        "art26-7-worker-information": "2027-12-02",
        "art26-11-affected-person-information": "2027-12-02",
        # Checked against the consolidated text.
        "art6-4-non-high-risk-assessment": "2027-12-02",
        "art43-conformity-assessment": "2027-12-02",
        "art47-48-declaration-ce-marking": "2027-12-02",
        "art26-4-input-data": "2027-12-02",
        "art50-3-emotion-biometric-notice": "2026-08-02",
        "art86-right-to-explanation": "2027-12-02",
        "art27-fria": "2027-12-02",
        "art49-registration": "2027-12-02",
        "art50-1-ai-interaction-notice": "2026-08-02",
        "art50-2-synthetic-marking": "2026-08-02",
        "art50-legacy": "2026-12-02",
        "art50-4-deepfake-text-disclosure": "2026-08-02",
        "art72-post-market-monitoring": "2027-12-02",
        "art73-serious-incidents": "2027-12-02",
        "annex1-embedded": "2028-08-02",
        "art53-gpai": "2025-08-02",
        "art55-gpai-systemic": "2025-08-02",
    }

    def test_every_obligation_has_the_omnibus_date(self):
        actual = {o.key: o.applies_from.isoformat() for o in EU_AI_ACT.obligations}
        assert actual == self.EXPECTED

    def test_pre_omnibus_dates_are_kept_for_replay(self):
        # Reg. 2024/1689 as published: Annex III duties from 2 Aug 2026,
        # Annex I from 2 Aug 2027 — what a window before 27 Jul 2026 is
        # evaluated against.
        assert get_ob("art12-record-keeping").applies_from_in("2024-08").isoformat() == "2026-08-02"
        assert get_ob("annex1-embedded").applies_from_in("2024-08").isoformat() == "2027-08-02"
        assert get_ob("art50-1-ai-interaction-notice").applies_from_in("2024-08").isoformat() == "2026-08-02"

    def test_every_obligation_is_fully_described(self):
        for o in EU_AI_ACT.obligations:
            assert o.title and o.reference and o.summary, o.key
            assert o.evidence, f"{o.key} names no evidence statement"

    def test_the_framework_registry_is_extensible_data(self):
        # Jurisdiction neutrality: the catalog is a dict of frameworks,
        # each self-describing — a new regime is an entry, not code.
        # The registry is read through `offering`.
        from app.services.compliance import offering

        assert offering.any_catalog("eu-ai-act") is EU_AI_ACT
        assert EU_AI_ACT.jurisdiction == "EU"
        assert "2026/1744" in EU_AI_ACT.reference

    def test_deadline_rules_and_retention_floor(self):
        assert {k: v.days for k, v in EU_AI_ACT.deadline_rules.items()} == {
            "standard": 15, "death": 10, "widespread": 2,
        }
        assert EU_AI_ACT.retention_floor_for("high") == 183
        assert EU_AI_ACT.retention_floor_for("limited") is None


class TestKeyAliases:
    def test_old_keys_resolve_to_their_successors(self):
        assert [o.key for o in EU_AI_ACT.resolve_key("art72-post-market")] == ["art72-post-market-monitoring"]
        assert {o.key for o in EU_AI_ACT.resolve_key("art50-transparency")} == {
            "art50-1-ai-interaction-notice", "art50-2-synthetic-marking", "art50-4-deepfake-text-disclosure",
        }
        provider = {o.key for o in EU_AI_ACT.resolve_key("annex3-provider-duties")}
        assert {"art9-risk-management", "art12-record-keeping", "art14-human-oversight",
                "art19-log-retention-provider"} <= provider
        assert "art27-fria" in {o.key for o in EU_AI_ACT.resolve_key("annex3-deployer-duties")}
        assert EU_AI_ACT.resolve_key("no-such-key") == []


class TestApplicabilityMatrix:
    def test_high_risk_provider_carries_the_provider_duties(self):
        r = run("art12-record-keeping", SystemContext("high", "provider"))
        assert r["status"] == "applies"
        assert "risk tier is high" in r["reason"]
        assert "role is provider" in r["reason"]

    def test_a_pure_deployer_does_not_carry_provider_duties(self):
        r = run("art12-record-keeping", SystemContext("high", "deployer"))
        assert r["status"] == "not_applicable"

    def test_a_deployer_carries_art26(self):
        r = run("art26-6-log-retention-deployer", SystemContext("high", "deployer"))
        assert r["status"] == "applies"

    def test_provider_and_deployer_carries_both_duty_sets(self):
        # Art 25's flip lands here: the combined role is in BOTH sets.
        both = SystemContext("high", "provider_and_deployer")
        assert run("art19-log-retention-provider", both)["status"] == "applies"
        assert run("art26-6-log-retention-deployer", both)["status"] == "applies"

    def test_limited_risk_gets_transparency_not_annex3(self):
        ctx = SystemContext("limited", "provider", flags={"interacts_with_natural_persons": True})
        assert run("art50-1-ai-interaction-notice", ctx)["status"] == "applies"
        assert run("art12-record-keeping", ctx)["status"] == "not_applicable"

    def test_art50_1_keys_on_the_interaction_flag_not_the_tier(self):
        ctx = SystemContext("minimal", "provider", flags={"interacts_with_natural_persons": False})
        assert run("art50-1-ai-interaction-notice", ctx)["status"] == "not_applicable"
        ctx.flags["interacts_with_natural_persons"] = True
        assert run("art50-1-ai-interaction-notice", ctx)["status"] == "applies"

    def test_prohibitions_and_literacy_reach_every_system(self):
        ctx = SystemContext("minimal", "provider")
        assert run("art4-ai-literacy", ctx)["status"] == "applies"
        assert run("art5-prohibitions", ctx)["status"] == "applies"
        assert run("art14-human-oversight", ctx)["status"] == "not_applicable"

    def test_serious_incidents_reach_every_high_risk_role(self):
        assert run("art73-serious-incidents", SystemContext("high", "deployer"))["status"] == "applies"
        assert run("art49-registration", SystemContext("high", "provider"))["status"] == "applies"


class TestHonestyRules:
    def test_an_undeclared_role_is_undetermined_not_ruled_out(self):
        r = run("art12-record-keeping", SystemContext("high", None))
        assert r["status"] == "undetermined"
        assert "declare" in r["reason"]

    def test_an_undeclared_risk_tier_is_undetermined(self):
        r = run("art14-human-oversight", SystemContext(None, "provider"))
        assert r["status"] == "undetermined"
        assert "classify" in r["reason"]

    def test_an_undeclared_flag_is_undetermined(self):
        r = run("art50-1-ai-interaction-notice", SystemContext("limited", "provider"))
        assert r["status"] == "undetermined"
        assert "natural persons" in r["reason"]

    def test_not_applicable_wins_over_undetermined(self):
        # Risk rules the obligation out before the missing role matters.
        r = run("art12-record-keeping", SystemContext("minimal", None))
        assert r["status"] == "not_applicable"

    def test_annex1_says_it_needs_review_rather_than_guessing(self):
        r = run("annex1-embedded", SystemContext("high", "provider"))
        assert r["needs_review"] is True
        assert "Annex I" in (r["review_note"] or "")


class TestDatedApplicability:
    def test_in_force_follows_the_calendar_not_a_flag(self):
        ctx = SystemContext("limited", "provider", flags={"interacts_with_natural_persons": True})
        before = run("art50-1-ai-interaction-notice", ctx, today=date(2026, 8, 1))
        after = run("art50-1-ai-interaction-notice", ctx, today=date(2026, 8, 2))
        assert before["in_force"] is False and after["in_force"] is True
        # Applicability itself is independent of the date — the duty
        # exists and is coming; only `in_force` moves.
        assert before["status"] == after["status"] == "applies"

    def test_the_omnibus_deferral_keeps_annex3_upcoming_today(self):
        r = run(
            "art12-record-keeping",
            SystemContext("high", "provider"),
            today=date(2026, 8, 20),
        )
        assert r["status"] == "applies" and r["in_force"] is False
        assert r["applies_from"] == "2027-12-02"


class TestGpaiTrack:
    def test_a_system_binding_a_gpai_model_surfaces_art53(self):
        ctx = SystemContext("minimal", "deployer", gpai_models=["GPT-X"])
        r = run("art53-gpai", ctx)
        assert r["status"] == "applies"
        assert "GPT-X" in r["reason"]

    def test_systemic_risk_models_surface_art55_and_art53(self):
        ctx = SystemContext(None, None, gpai_systemic_models=["Frontier-1"])
        assert run("art55-gpai-systemic", ctx)["status"] == "applies"
        # A systemic-risk model is a fortiori a GPAI model.
        assert run("art53-gpai", ctx)["status"] == "applies"

    def test_no_gpai_models_means_not_applicable_not_undetermined(self):
        # GPAI exposure is measured from bindings, not declared — its
        # absence is a fact, so this is a clean not-applicable.
        r = run("art53-gpai", SystemContext(None, None))
        assert r["status"] == "not_applicable"


class TestRoleVocabulary:
    def test_model_validator_pins_the_role_vocabulary(self):
        from app.database.models.resource_group import (
            EU_AI_ACT_ROLES,
            GroupType,
            ResourceGroup,
        )

        assert EU_AI_ACT_ROLES == {
            "provider", "deployer", "provider_and_deployer", "importer",
            "distributor", "product_manufacturer",
            "authorised_representative",
        }
        g = ResourceGroup(
            id="g", tenant_id="t", name="x", description="d",
            hierarchy_level=0, inherit_resources=True, is_active=True,
            is_system=False, group_type=GroupType.AI_SYSTEM,
        )
        g.eu_ai_act_role = "deployer"
        with pytest.raises(ValueError, match="eu_ai_act_role"):
            g.eu_ai_act_role = "bystander"
