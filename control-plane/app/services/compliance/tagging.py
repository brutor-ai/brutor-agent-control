# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance tagging engine.

Pure function `derive_tags(ctx) -> list[str]` that decides which
compliance regime tags fire for a single call. Called from the
audit-row write path (`proxy_log_service.build_proxy_log`) and
from the Rust proxy via a cached lookup map (so the runtime
doesn't have to talk Python on every call).

Architecture decision: the engine is implemented in Python on the
control plane, NOT in the Rust runtime. Three reasons:

  1. Compliance taxonomies churn. SOC 2 CC numbering, EU AI Act
     risk-tier criteria, HIPAA PHI categories all shift between
     auditor cycles. Python lets us ship updates without a
     gateway-core release.
  2. The set of inputs is small — tenant profile, RG flags, PII
     categories, residency class, request outcome — all of which
     the audit writer already has. No new IO.
  3. Performance is not load-bearing here: tagging runs once per
     audit row, with all inputs already in memory. A pure-Python
     dispatch per row is <50 µs in profiling.

Tag derivation per framework:

  gdpr_art_30 — fires when tenant lists GDPR AND the call carries
                personal data per the PII detector AND the data
                subject is identifiable (gateway_user_id OR
                idp_user_email present).

  eu_ai_act   — fires when tenant lists EU AI Act AND the RG has
                an eu_ai_act_risk_tier set. Tag value carries the
                tier: `eu_ai_act.high`, `eu_ai_act.minimal`, etc.

  soc2        — fires when tenant lists SOC 2 AND the call landed
                an audit-worthy event. Adds the specific control
                numbers per event class:
                  auth events       → soc2.cc6.1
                  guardrail blocks  → soc2.cc7.2
                  policy denies     → soc2.cc6.6
                  residency blocks  → soc2.cc6.7
                  rate-limit hits   → soc2.cc7.1

  hipaa       — fires when tenant lists HIPAA AND either:
                  - PII detector found PHI categories
                  - RG is marked phi_resource=true
                Tag value carries source: `hipaa.phi` or
                `hipaa.rg_marked`.

  iso_42001   — fires whenever tenant lists ISO 42001 (every
                call counts toward the AI management system
                audit log).

Tag values are interned strings, to keep the JSONB column
small. Don't add free-form values; if a new sub-tag is needed,
add it to the per-framework section below.

Inputs come in as a single dataclass `TaggingContext` so callers
don't have to remember the exact set of fields the engine reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set

from app.database.models.compliance_framework import tag_key_for

# ---------------------------------------------------------------------------
# Framework keys — read from the framework registry, never
# re-declared here. The Rust core's `FRAMEWORK_*` constants are pinned to
# the same registry by tests/test_compliance_python_rust_parity.py.
# ---------------------------------------------------------------------------

FRAMEWORK_GDPR = tag_key_for("gdpr")            # gdpr_art_30
FRAMEWORK_EU_AI_ACT = tag_key_for("eu-ai-act")  # eu_ai_act
FRAMEWORK_SOC2 = tag_key_for("soc2")            # soc2
FRAMEWORK_HIPAA = tag_key_for("hipaa")          # hipaa
FRAMEWORK_ISO_42001 = tag_key_for("iso-42001")  # iso_42001


# ---------------------------------------------------------------------------
# Tag value vocabulary
# ---------------------------------------------------------------------------
#
# Tag strings live in this module. The Pydantic ComplianceTagsResponse
# does not validate against this list — operators may see legacy tags
# in old audit rows after we rename — but the engine itself ONLY
# emits values from these constants.

# SOC 2 control coverage tags (Common Criteria series). Numbering
# follows the AICPA's published mapping; each control corresponds
# to a class of audit event in our system.
TAG_SOC2_AUTH_EVENT = "soc2.cc6.1"           # logical access controls
TAG_SOC2_GUARDRAIL_BLOCK = "soc2.cc7.2"      # system monitoring
TAG_SOC2_POLICY_DENY = "soc2.cc6.6"          # authorization
TAG_SOC2_RESIDENCY_BLOCK = "soc2.cc6.7"      # data movement
TAG_SOC2_RATE_LIMIT_HIT = "soc2.cc7.1"       # capacity / threshold


# GDPR Article 30 — Records of Processing Activities. Fires on any
# call carrying personal data with an identifiable data subject.
TAG_GDPR_ART_30 = "gdpr_art_30"


# HIPAA — two sources for the PHI tag (PII-detected vs RG-marked)
# so the audit dashboard can split "we caught it" from "operator
# pre-declared it".
TAG_HIPAA_PHI = "hipaa.phi"
TAG_HIPAA_RG_MARKED = "hipaa.rg_marked"


# ISO 42001 — always-on tag for tenants that declare it.
TAG_ISO_42001 = "iso_42001"


# ---------------------------------------------------------------------------
# Tagging context — the engine's input record
# ---------------------------------------------------------------------------


@dataclass
class TaggingContext:
    """All inputs the tagging engine reads, gathered at audit-write
    time. The audit writer already has every field on hand from
    `ProxyLog.to_dict()` plus the tenant + RG row it just loaded;
    populate this dataclass and call `derive_tags`."""

    # Tenant-level
    frameworks: Set[str] = field(default_factory=set)

    # Resource-group-level
    eu_ai_act_risk_tier: Optional[str] = None  # minimal|limited|high|unacceptable
    rg_phi_resource: bool = False

    # Call-level — derived from ProxyLog row
    has_pii: bool = False
    has_phi: bool = False                       # PHI is a subset of PII
    gateway_user_id: Optional[str] = None
    idp_user_email: Optional[str] = None

    # Audit-worthy event flags. The audit writer sets these based
    # on the request outcome:
    is_auth_event: bool = False                 # login / token issue / api-key auth
    guardrail_blocked: bool = False
    argument_policy_denied: bool = False
    residency_blocked: bool = False
    rate_limit_hit: bool = False


def _data_subject_identifiable(ctx: TaggingContext) -> bool:
    """GDPR Article 30 specifically requires identifying the data
    subject. We treat the gateway_user_id or idp_user_email as
    sufficient — both are stable per-user identifiers logged on
    every authenticated row."""
    return bool(ctx.gateway_user_id or ctx.idp_user_email)


# ---------------------------------------------------------------------------
# Per-framework derivation
# ---------------------------------------------------------------------------


def _tag_gdpr(ctx: TaggingContext) -> List[str]:
    if FRAMEWORK_GDPR not in ctx.frameworks:
        return []
    if not ctx.has_pii:
        return []
    if not _data_subject_identifiable(ctx):
        # No identifiable subject = no Article 30 obligation. The
        # call still happened but doesn't enter the RoPA register.
        return []
    return [TAG_GDPR_ART_30]


def _tag_eu_ai_act(ctx: TaggingContext) -> List[str]:
    if FRAMEWORK_EU_AI_ACT not in ctx.frameworks:
        return []
    if not ctx.eu_ai_act_risk_tier:
        # Tenant declared the regime but the RG hasn't been
        # classified yet — no tag, but the dashboard should
        # surface unclassified RGs (handled in the UI, not here).
        return []
    return [f"eu_ai_act.{ctx.eu_ai_act_risk_tier}"]


def _tag_soc2(ctx: TaggingContext) -> List[str]:
    if FRAMEWORK_SOC2 not in ctx.frameworks:
        return []
    tags: List[str] = []
    # An auth event always counts as CC6.1 evidence.
    if ctx.is_auth_event:
        tags.append(TAG_SOC2_AUTH_EVENT)
    # Each enforcement outcome maps to one control number. A single
    # call can land multiple — a guardrail-blocked auth-failure
    # counts as both CC6.1 (auth) and CC7.2 (monitoring).
    if ctx.guardrail_blocked:
        tags.append(TAG_SOC2_GUARDRAIL_BLOCK)
    if ctx.argument_policy_denied:
        tags.append(TAG_SOC2_POLICY_DENY)
    if ctx.residency_blocked:
        tags.append(TAG_SOC2_RESIDENCY_BLOCK)
    if ctx.rate_limit_hit:
        tags.append(TAG_SOC2_RATE_LIMIT_HIT)
    return tags


def _tag_hipaa(ctx: TaggingContext) -> List[str]:
    if FRAMEWORK_HIPAA not in ctx.frameworks:
        return []
    tags: List[str] = []
    if ctx.has_phi:
        tags.append(TAG_HIPAA_PHI)
    if ctx.rg_phi_resource:
        # RG operator pre-declared the resource group as PHI-bearing.
        # We tag every call through it regardless of what the PII
        # detector saw, because the auditor needs the complete
        # access log, not just hits.
        tags.append(TAG_HIPAA_RG_MARKED)
    return tags


def _tag_iso_42001(ctx: TaggingContext) -> List[str]:
    if FRAMEWORK_ISO_42001 not in ctx.frameworks:
        return []
    # Always-on for tenants under ISO 42001 — the standard requires
    # logging the AI management system's activity, not just events.
    return [TAG_ISO_42001]


# Dispatch order is deterministic so the output array order is
# stable across calls (test-friendly).
_FRAMEWORK_DERIVERS = (
    _tag_gdpr,
    _tag_eu_ai_act,
    _tag_soc2,
    _tag_hipaa,
    _tag_iso_42001,
)


# ---------------------------------------------------------------------------
# Engine entrypoint
# ---------------------------------------------------------------------------


def derive_tags(ctx: TaggingContext) -> Optional[List[str]]:
    """Compute the compliance_tags array for one call.

    Returns:
      * None when the tenant has no compliance profile (no
        frameworks declared). The audit writer should set
        proxy_logs.compliance_tags to NULL — the "engine never ran"
        sentinel.
      * Empty list when frameworks are declared but no tags fired
        for this specific call (e.g. tenant lists SOC 2 but this
        was an unauthenticated GET with no enforcement events).
      * Non-empty list of interned tags when one or more regimes
        applied.

    Distinguishing None vs [] matters for the dashboard: a tenant
    with `frameworks: ["soc2"]` who has zero CC6.1 hits in a month
    is a different state than a tenant with no compliance profile.
    """
    if not ctx.frameworks:
        # Engine no-op for tenants without a declared profile. The
        # audit writer skips touching the column entirely — keeps
        # the JSONB index small (partial index excludes NULL rows).
        return None

    tags: List[str] = []
    for derive in _FRAMEWORK_DERIVERS:
        tags.extend(derive(ctx))
    return tags
