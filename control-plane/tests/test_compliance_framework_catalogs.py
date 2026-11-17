# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The framework catalogs beyond the EU AI Act

Pinned here: every catalog is registered, its dates and deadline rules are
the ones the RFC names, every statement names a shared resolver that
exists and carries a `limit`, the unverified catalogs say so and use the
crosswalk's language, and no catalog string makes a claim the vocabulary
rule bans. Shared statements have the same identity everywhere, so one
evidence log gives one answer under every framework.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import re
from dataclasses import fields
from datetime import date, timedelta

import pytest

from app.database.models.compliance_framework import FRAMEWORK_SEED
from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import (
    ANY_CLASSIFICATION,
    EVIDENCE_BASES,
    KIND_ATTESTATION,
    KIND_STANDARD,
    KIND_VOLUNTARY,
)
from app.services.compliance.resolvers import resolver_exists

FRAMEWORK_IDS = ("iso-42001", "soc2", "hipaa", "gdpr", "dora", "nis2", "nist-ai-rmf", "colorado", "korea", "brazil")
#: Not checked against the published text: Brazil (a bill, PL 2338/2023).
#: Every other catalog cites its sources (licensed texts by fingerprint).
UNVERIFIED = ("brazil",)

#: The vocabulary rule: a catalog string never claims one of these.
BANNED = re.compile(r"\b(compliant|certified|verified|guaranteed|pass|passed|approved|tamper-proof)\b", re.I)

EXPECTED_COUNTS = {
    "iso-42001": 23, "soc2": 13, "hipaa": 14, "gdpr": 14, "dora": 8, "nis2": 5,
    "nist-ai-rmf": 6, "colorado": 7, "korea": 6, "brazil": 1,
}

PINNED_DATES = {
    "iso-42001": date(2023, 12, 18),
    "soc2": date(2018, 12, 15),
    "hipaa": date(2005, 4, 20),
    "gdpr": date(2018, 5, 25),
    "dora": date(2025, 1, 17),
    "nis2": date(2024, 10, 18),
    "nist-ai-rmf": date(2023, 1, 26),
    # SB 26-189 repealed and reenacted the Act; effective date: 2027-01-01.
    "colorado": date(2027, 1, 1),
    "korea": date(2026, 1, 22),
    "brazil": date(2099, 12, 31),
}


def _strings(framework):
    out = [framework.title, framework.reference]
    out += [v.note or "" for v in framework.versions]
    for o in framework.obligations:
        out += [o.title, o.reference, o.summary, o.review_note or "", o.limit or ""]
        for s in o.evidence:
            out += [s.statement, s.limit or ""]
    return out


@pytest.mark.parametrize("fid", FRAMEWORK_IDS)
def test_catalog_registered_and_in_the_registry_seed(fid):
    assert fid in CATALOGS
    assert fid in {row["id"] for row in FRAMEWORK_SEED}
    assert CATALOGS[fid].current_version == "2026-09"


@pytest.mark.parametrize("fid", FRAMEWORK_IDS)
def test_obligation_counts_and_dates_are_pinned(fid):
    fw = CATALOGS[fid]
    assert len(fw.obligations) == EXPECTED_COUNTS[fid]
    assert {o.applies_from for o in fw.obligations} == {PINNED_DATES[fid]}
    assert fw.versions[0].effective_from == PINNED_DATES[fid]
    keys = [o.key for o in fw.obligations]
    assert len(keys) == len(set(keys)), "obligation keys are unique"


@pytest.mark.parametrize("fid", FRAMEWORK_IDS)
def test_every_statement_resolves_and_states_its_limit(fid):
    for o in CATALOGS[fid].obligations:
        assert o.evidence, f"{fid}/{o.key} has no evidence statement"
        seen = set()
        for spec in o.evidence:
            assert resolver_exists(spec.resolver), f"{fid}/{o.key}: unknown resolver {spec.resolver}"
            assert spec.basis in EVIDENCE_BASES
            assert spec.limit, f"{fid}/{o.key}/{spec.key} has no limit"
            assert spec.key not in seen
            seen.add(spec.key)


@pytest.mark.parametrize("fid", FRAMEWORK_IDS)
def test_no_banned_claim_in_any_catalog_string(fid):
    for text in _strings(CATALOGS[fid]):
        assert not BANNED.search(text), f"{fid}: {text!r}"


def test_kinds():
    assert CATALOGS["iso-42001"].kind == KIND_STANDARD
    assert CATALOGS["soc2"].kind == KIND_ATTESTATION
    assert CATALOGS["nist-ai-rmf"].kind == KIND_VOLUNTARY


@pytest.mark.parametrize("fid", UNVERIFIED)
def test_unverified_catalogs_are_flagged_and_use_crosswalk_language(fid):
    fw = CATALOGS[fid]
    assert fw.verified_against_instrument is False
    for o in fw.obligations:
        assert "published text" in o.summary, f"{fid}/{o.key} lacks the verify line"
        assert "satisf" not in o.summary.lower()
    if fid in ("nist-ai-rmf", "colorado", "korea", "brazil"):
        assert all("Can serve as evidence toward" in o.summary for o in fw.obligations)


def test_brazil_is_a_placeholder_never_in_force():
    fw = CATALOGS["brazil"]
    (ob,) = fw.obligations
    assert ob.needs_review and ob.applies_from > date(2090, 1, 1)
    assert "not law" in ob.summary


class TestDeadlineRules:
    def test_eu_alternatives(self):
        fw = CATALOGS["eu-ai-act"]
        assert fw.deadline_for(None) == timedelta(days=15)
        assert fw.deadline_for("death") == timedelta(days=10)
        assert fw.deadline_for("widespread") == timedelta(days=2)

    def test_gdpr_hipaa_colorado(self):
        assert CATALOGS["gdpr"].deadline_for("breach") == timedelta(hours=72)
        assert CATALOGS["hipaa"].deadline_for(None) == timedelta(days=60)
        # The reenacted Colorado law has no Attorney General notice deadline.
        assert CATALOGS["colorado"].deadline_for(None) is None

    def test_dora_and_nis2_are_staged(self):
        assert dict(CATALOGS["dora"].deadline_rules) == {
            "initial": timedelta(hours=4), "intermediate": timedelta(hours=72), "final": timedelta(days=30),
        }
        assert dict(CATALOGS["nis2"].deadline_rules) == {
            "early_warning": timedelta(hours=24), "notification": timedelta(hours=72), "final": timedelta(days=30),
        }
        # The reporting deadline (what the resolver compares against) is the first stage.
        assert CATALOGS["dora"].deadline_for("major_incident") == timedelta(hours=4)
        assert CATALOGS["nis2"].deadline_for("significant_incident") == timedelta(hours=24)

    def test_soc2_has_no_statutory_deadline(self):
        assert not CATALOGS["soc2"].deadline_rules


class TestRetentionFloors:
    def test_soc2_attestation_period(self):
        fw = CATALOGS["soc2"]
        assert fw.retention_floors == {ANY_CLASSIFICATION: 365}
        spec = next(s for o in fw.obligations for s in o.evidence if s.resolver == "retention.floor")
        assert spec.params["floor_days"] == 365

    def test_hipaa_floor_is_documentation_scoped_only(self):
        fw = CATALOGS["hipaa"]
        assert not fw.retention_floors, "six years must never become a floor on every record"
        spec = next(s for o in fw.obligations for s in o.evidence if s.resolver == "retention.floor")
        assert spec.params == {"floor_days": 2190, "scope": "documentation"}
        assert "documentation" in spec.limit

    def test_iso_mandates_none(self):
        assert not CATALOGS["iso-42001"].retention_floors


def test_shared_statements_have_one_identity_across_catalogs():
    """A statement cited by several frameworks must be the same
    `(resolver, params)` everywhere (framework-parameterised resolvers
    excepted), so one set of records renders one result per framework."""
    framework_scoped = {"docs.technical_documentation", "incidents.reported_within_deadline",
                        "docs.registration_recorded", "profile.field_declared",
                        "evidence.pointer_on_file", "access.sensitive_reads_recorded", "retention.floor"}
    by_resolver = {}
    for fid in ("eu-ai-act", *FRAMEWORK_IDS):
        for o in CATALOGS[fid].obligations:
            for s in o.evidence:
                if s.resolver in framework_scoped or s.resolver.startswith("judge."):
                    continue
                by_resolver.setdefault(s.resolver, set()).add(s.identity)
    for resolver_id, identities in by_resolver.items():
        assert len(identities) == 1, f"{resolver_id} has divergent params: {identities}"


def test_evidence_spec_shape_unchanged():
    # The catalogs are data; the type belongs to the catalog base module. Guard
    # the fields we rely on.
    from app.services.compliance.catalogs.base import EvidenceSpec

    assert {f.name for f in fields(EvidenceSpec)} >= {"key", "statement", "basis", "resolver", "params", "limit"}
