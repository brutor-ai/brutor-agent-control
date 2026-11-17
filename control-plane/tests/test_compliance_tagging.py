# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance tagging engine regression suite.

Pins five derivation contracts:

  * No profile → None (engine no-op).
  * Declared profile + no applicable event → empty list (different
    audit state from None — the dashboard surfaces "compliant but
    quiet" tenants).
  * GDPR Art 30 needs both PII + identifiable subject.
  * EU AI Act tag carries the per-RG risk tier.
  * SOC 2 maps each enforcement outcome to its specific control
    number (CC6.1 / CC6.6 / CC6.7 / CC7.1 / CC7.2).
  * HIPAA distinguishes PII-detected PHI from RG-marked PHI.
  * ISO 42001 fires on every call when declared.

Plus the cross-regime case: a single call can land 4+ tags.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.services.compliance.tagging import (
    TAG_GDPR_ART_30,
    TAG_HIPAA_PHI,
    TAG_HIPAA_RG_MARKED,
    TAG_ISO_42001,
    TAG_SOC2_AUTH_EVENT,
    TAG_SOC2_GUARDRAIL_BLOCK,
    TAG_SOC2_POLICY_DENY,
    TAG_SOC2_RATE_LIMIT_HIT,
    TAG_SOC2_RESIDENCY_BLOCK,
    TaggingContext,
    derive_tags,
)


# ---------------------------------------------------------------------------
# Engine no-op / empty-list distinction
# ---------------------------------------------------------------------------


class TestEngineNoOpVsEmpty:
    def test_no_frameworks_returns_none(self):
        """A tenant with no compliance profile gets None, not []. The
        audit writer maps None → NULL in proxy_logs.compliance_tags,
        which keeps the partial GIN index empty for these tenants."""
        ctx = TaggingContext(frameworks=set())
        assert derive_tags(ctx) is None

    def test_declared_profile_but_quiet_call_returns_empty_list(self):
        """A tenant lists SOC 2 but this specific call had no
        enforcement events — engine returns [], NOT None. The
        dashboard reads this as 'engine ran, found nothing to tag'
        which is distinct from 'engine never ran'."""
        ctx = TaggingContext(frameworks={"soc2"})
        result = derive_tags(ctx)
        assert result == [], (
            f"declared profile + quiet call should return empty list, "
            f"got {result!r}"
        )


# ---------------------------------------------------------------------------
# Per-framework derivation
# ---------------------------------------------------------------------------


class TestGdprArt30:
    def test_fires_with_pii_and_identifiable_subject(self):
        ctx = TaggingContext(
            frameworks={"gdpr_art_30"},
            has_pii=True,
            gateway_user_id="u-42",
        )
        assert TAG_GDPR_ART_30 in derive_tags(ctx)

    def test_silent_without_pii(self):
        # Article 30 doesn't apply when no personal data is
        # processed. The call still happens but doesn't enter
        # the RoPA register.
        ctx = TaggingContext(
            frameworks={"gdpr_art_30"},
            has_pii=False,
            gateway_user_id="u-42",
        )
        assert derive_tags(ctx) == []

    def test_silent_without_identifiable_subject(self):
        # An anonymous PII-laden call (no gateway_user_id, no
        # idp_user_email) doesn't qualify — Article 30 requires
        # identifying the subject.
        ctx = TaggingContext(
            frameworks={"gdpr_art_30"},
            has_pii=True,
            gateway_user_id=None,
            idp_user_email=None,
        )
        assert derive_tags(ctx) == []

    def test_idp_email_satisfies_identifiability(self):
        # SAML / OIDC flows may carry email but not gateway_user_id.
        # Both count as identifying.
        ctx = TaggingContext(
            frameworks={"gdpr_art_30"},
            has_pii=True,
            idp_user_email="user@enterprise.example",
        )
        assert TAG_GDPR_ART_30 in derive_tags(ctx)


class TestEuAiAct:
    def test_emits_tier_suffix(self):
        # Tag value carries the per-RG tier so dashboard queries
        # can filter "show me every call through high-risk RGs".
        ctx = TaggingContext(
            frameworks={"eu_ai_act"},
            eu_ai_act_risk_tier="high",
        )
        assert "eu_ai_act.high" in derive_tags(ctx)

    def test_silent_when_rg_unclassified(self):
        # Tenant declared the regime but the RG hasn't been
        # assigned a tier — no tag. The dashboard surfaces these
        # unclassified RGs separately so operators know to fix it.
        ctx = TaggingContext(
            frameworks={"eu_ai_act"},
            eu_ai_act_risk_tier=None,
        )
        assert "eu_ai_act.high" not in derive_tags(ctx)


class TestSoc2:
    def test_auth_event_maps_to_cc61(self):
        ctx = TaggingContext(frameworks={"soc2"}, is_auth_event=True)
        assert TAG_SOC2_AUTH_EVENT in derive_tags(ctx)

    def test_guardrail_block_maps_to_cc72(self):
        ctx = TaggingContext(frameworks={"soc2"}, guardrail_blocked=True)
        assert TAG_SOC2_GUARDRAIL_BLOCK in derive_tags(ctx)

    def test_policy_deny_maps_to_cc66(self):
        ctx = TaggingContext(frameworks={"soc2"}, argument_policy_denied=True)
        assert TAG_SOC2_POLICY_DENY in derive_tags(ctx)

    def test_residency_block_maps_to_cc67(self):
        ctx = TaggingContext(frameworks={"soc2"}, residency_blocked=True)
        assert TAG_SOC2_RESIDENCY_BLOCK in derive_tags(ctx)

    def test_rate_limit_maps_to_cc71(self):
        ctx = TaggingContext(frameworks={"soc2"}, rate_limit_hit=True)
        assert TAG_SOC2_RATE_LIMIT_HIT in derive_tags(ctx)

    def test_multiple_outcomes_land_multiple_controls(self):
        # An auth event that was guardrail-blocked accumulates both
        # CC6.1 (auth) and CC7.2 (monitoring) evidence.
        ctx = TaggingContext(
            frameworks={"soc2"},
            is_auth_event=True,
            guardrail_blocked=True,
        )
        tags = derive_tags(ctx)
        assert TAG_SOC2_AUTH_EVENT in tags
        assert TAG_SOC2_GUARDRAIL_BLOCK in tags


class TestHipaa:
    def test_phi_detected_fires_phi_tag(self):
        ctx = TaggingContext(frameworks={"hipaa"}, has_phi=True)
        assert TAG_HIPAA_PHI in derive_tags(ctx)

    def test_rg_marked_phi_fires_rg_marked_tag(self):
        ctx = TaggingContext(
            frameworks={"hipaa"},
            rg_phi_resource=True,
        )
        assert TAG_HIPAA_RG_MARKED in derive_tags(ctx)

    def test_both_sources_emit_both_tags(self):
        # Auditor needs to distinguish "we caught PHI on the wire"
        # (hipaa.phi) from "operator pre-declared this RG"
        # (hipaa.rg_marked). A call hitting both should emit both.
        ctx = TaggingContext(
            frameworks={"hipaa"},
            has_phi=True,
            rg_phi_resource=True,
        )
        tags = derive_tags(ctx)
        assert TAG_HIPAA_PHI in tags
        assert TAG_HIPAA_RG_MARKED in tags

    def test_no_tag_when_clean_call_through_non_phi_rg(self):
        ctx = TaggingContext(
            frameworks={"hipaa"},
            has_phi=False,
            rg_phi_resource=False,
        )
        assert derive_tags(ctx) == []


class TestIso42001:
    def test_always_fires_when_declared(self):
        # ISO 42001 requires logging the AI management system's
        # *activity*, not just events — so every call lands the tag.
        ctx = TaggingContext(frameworks={"iso_42001"})
        assert TAG_ISO_42001 in derive_tags(ctx)


# ---------------------------------------------------------------------------
# Cross-regime composition
# ---------------------------------------------------------------------------


class TestMultiRegimeCall:
    def test_single_call_can_land_tags_from_every_declared_regime(self):
        """A user call that processes PII through a high-risk RG
        marked phi_resource, with a guardrail block during auth,
        under a tenant with all five frameworks declared. This is
        the "kitchen sink" cross-regime case the dashboard's
        per-framework summary cards need to render correctly."""
        ctx = TaggingContext(
            frameworks={"gdpr_art_30", "soc2", "hipaa", "eu_ai_act", "iso_42001"},
            eu_ai_act_risk_tier="high",
            rg_phi_resource=True,
            has_pii=True,
            has_phi=True,
            gateway_user_id="u-42",
            is_auth_event=True,
            guardrail_blocked=True,
        )
        tags = derive_tags(ctx)
        assert TAG_GDPR_ART_30 in tags
        assert "eu_ai_act.high" in tags
        assert TAG_SOC2_AUTH_EVENT in tags
        assert TAG_SOC2_GUARDRAIL_BLOCK in tags
        assert TAG_HIPAA_PHI in tags
        assert TAG_HIPAA_RG_MARKED in tags
        assert TAG_ISO_42001 in tags
        # Sanity: we expect 7 distinct tags above; the engine
        # should never duplicate.
        assert len(tags) == len(set(tags)), (
            f"engine emitted duplicate tags: {tags}"
        )

    def test_output_order_is_deterministic(self):
        # Repeated calls with the same input must produce the same
        # tag order — tests downstream of the audit row sometimes
        # compare arrays directly.
        ctx = TaggingContext(
            frameworks={"soc2", "iso_42001"},
            is_auth_event=True,
        )
        first = derive_tags(ctx)
        second = derive_tags(ctx)
        assert first == second
