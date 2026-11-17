# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Coverage attestation — the population proof under the Assurance Report.

The behaviour these lock down is mostly about what the attestation *refuses*
to say. Every other assurance section can be wrong in a way an operator will
eventually notice; a fabricated coverage percentage is wrong in a way that
looks like success, which is why the unmeasured case gets the most tests here.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.discovery import (
    GOV_GOVERNED,
    GOV_UNGOVERNED,
    GOV_UNKNOWN,
    STATUS_ACTIVE,
    STATUS_DISMISSED,
    STATUS_ONBOARDED,
    CollectorKey,
    DiscoveredAsset,
)
from app.services.ai_system_coverage import (
    BASIS_MEASURED,
    BASIS_STALE,
    BASIS_UNMEASURED,
    STALE_AFTER_DAYS,
    attest_coverage,
    coverage_gaps,
)

TENANT = "t-cov"
OTHER = "t-other"
REQUIRED = [CollectorKey.__table__, DiscoveredAsset.__table__]
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def add_collector(db, *, last_used=NOW, tenant=TENANT, enabled=True, revoked=None):
    k = CollectorKey(
        key_id=f"k-{tenant}-{last_used}-{enabled}-{revoked}",
        tenant_id=tenant,
        collector_id="egress-01",
        collector_type="egress-log",
        public_key="x" * 44,
        trust_tier="first_party",
        enabled=enabled,
        revoked_at=revoked,
        last_used_at=last_used,
    )
    db.add(k)
    db.commit()
    return k


_seq = iter(range(1, 10_000))


def add_asset(
    db,
    *,
    gov=GOV_GOVERNED,
    status=STATUS_ACTIVE,
    asset_type="llm_endpoint",
    tenant=TENANT,
):
    n = next(_seq)
    a = DiscoveredAsset(
        id=f"dasset-{n}",
        tenant_id=tenant,
        asset_key=f"key-{n}",
        asset_type=asset_type,
        governance_status=gov,
        status=status,
        computed_confidence=0.9,
    )
    db.add(a)
    db.commit()
    return a


# ---------------------------------------------------------------------------
# The rule the whole module exists for: silence is never full coverage.
# ---------------------------------------------------------------------------


def test_no_collector_ever_is_unmeasured_not_full_coverage(db):
    """The most dangerous possible bug in this file.

    A tenant that never installed discovery must not be handed 100%. That
    figure would be the most misleading number in the report precisely
    because it looks like the best one.
    """
    add_asset(db, gov=GOV_GOVERNED)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_UNMEASURED
    assert c["coverage_pct"] is None
    assert "UNMEASURED" in c["statement"]


def test_unmeasured_statement_never_implies_completeness(db):
    c = attest_coverage(db, TENANT, now=NOW)
    s = c["statement"].lower()
    assert "100%" not in s
    assert "makes no claim" in s


def test_registered_collector_that_never_reported_is_still_unmeasured(db):
    """Registering a key is intent, not evidence. Only `last_used_at` proves
    something actually looked."""
    add_collector(db, last_used=None)
    add_asset(db)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_UNMEASURED
    assert c["coverage_pct"] is None
    assert c["collectors_registered"] == 1


def test_unmeasured_emits_exactly_one_gap_and_stops(db):
    """When the population is unknown, itemising sub-gaps within it would
    imply the estate numbers mean something. They do not."""
    add_asset(db, gov=GOV_UNGOVERNED)
    gaps = coverage_gaps(attest_coverage(db, TENANT, now=NOW))
    assert len(gaps) == 1
    assert "unmeasured" in gaps[0]


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_coverage_ratio(db):
    add_collector(db)
    for _ in range(3):
        add_asset(db, gov=GOV_GOVERNED)
    add_asset(db, gov=GOV_UNGOVERNED)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_MEASURED
    assert c["coverage_pct"] == pytest.approx(0.75)
    assert (c["governed"], c["ungoverned"], c["total_discovered"]) == (3, 1, 4)


def test_unknown_counts_against_coverage_not_for_it(db):
    """An asset we could not reconcile is not a routed asset. Folding it into
    the numerator would let a flaky reconcile pass inflate the headline."""
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED)
    add_asset(db, gov=GOV_UNKNOWN)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["coverage_pct"] == pytest.approx(0.5)
    assert c["unknown"] == 1


def test_onboarded_assets_count_as_part_of_the_estate(db):
    """`onboarded` is a review state, not a deletion — the asset is still
    real and its governance status still decides the ratio."""
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED, status=STATUS_ONBOARDED)
    add_asset(db, gov=GOV_UNGOVERNED, status=STATUS_ACTIVE)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["total_discovered"] == 2
    assert c["coverage_pct"] == pytest.approx(0.5)


def test_dismissed_leaves_the_denominator_but_is_disclosed(db):
    """An operator can raise the percentage by dismissing assets. That is
    allowed — but it must be visible, or the number is unauditable."""
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED)
    add_asset(db, gov=GOV_UNGOVERNED, status=STATUS_DISMISSED)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["coverage_pct"] == pytest.approx(1.0)
    assert c["dismissed"] == 1
    assert any("dismissed" in g for g in c["gaps"])


def test_measured_but_empty_estate_gives_no_percentage(db):
    """Discovery ran and found nothing. That is not 100% — 0/0 is not a
    ratio, and pretending otherwise invents a claim from an empty set."""
    add_collector(db)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_MEASURED
    assert c["coverage_pct"] is None
    assert "no coverage ratio can be computed" in c["statement"]


def test_tenant_isolation(db):
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED)
    add_asset(db, gov=GOV_UNGOVERNED, tenant=OTHER)
    add_collector(db, tenant=OTHER)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["total_discovered"] == 1
    assert c["collectors_registered"] == 1


# ---------------------------------------------------------------------------
# Staleness — a real number from a dead collector is still a claim about
# the past, and must say so.
# ---------------------------------------------------------------------------


def test_quiet_collector_degrades_basis_to_stale(db):
    add_collector(db, last_used=NOW - timedelta(days=STALE_AFTER_DAYS + 1))
    add_asset(db, gov=GOV_GOVERNED)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_STALE
    # The ratio is still computed — it was measured, just not recently.
    assert c["coverage_pct"] == pytest.approx(1.0)
    assert "days old" in c["statement"]
    assert any("stale" in g for g in c["gaps"])


def test_just_inside_the_window_is_still_measured(db):
    add_collector(db, last_used=NOW - timedelta(days=STALE_AFTER_DAYS - 1))
    add_asset(db)
    assert attest_coverage(db, TENANT, now=NOW)["basis"] == BASIS_MEASURED


def test_most_recent_collector_wins_the_freshness_call(db):
    """One dead collector alongside a live one is not a stale estate."""
    add_collector(db, last_used=NOW - timedelta(days=90))
    add_collector(db, last_used=NOW - timedelta(hours=1))
    add_asset(db)
    assert attest_coverage(db, TENANT, now=NOW)["basis"] == BASIS_MEASURED


def test_revoked_collector_still_counts_as_having_looked(db):
    """Revocation is a credential decision, not a retraction of what it saw.
    The inventory it produced is still the best evidence available — reporting
    `unmeasured` here would throw away real findings."""
    add_collector(db, last_used=NOW, revoked=NOW, enabled=False)
    add_asset(db, gov=GOV_UNGOVERNED)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["basis"] == BASIS_MEASURED
    assert c["collectors_active"] == 0
    assert c["collectors_registered"] == 1


# ---------------------------------------------------------------------------
# Planes — a number computed over one plane says nothing about the others.
# ---------------------------------------------------------------------------


def test_network_only_estate_flags_the_endpoint_gap(db):
    """The commonest real gap: perfect network visibility, zero laptops."""
    add_collector(db)
    add_asset(db, asset_type="llm_endpoint")
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["endpoint_plane_observed"] is False
    assert any("endpoint-plane" in g for g in c["gaps"])


@pytest.mark.parametrize("t", ["local_ai_client", "local_mcp_server"])
def test_endpoint_assets_mark_the_endpoint_plane_observed(db, t):
    add_collector(db)
    add_asset(db, asset_type=t)
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["endpoint_plane_observed"] is True
    assert "endpoint" in c["planes_observed"]
    assert not any("endpoint-plane" in g for g in c["gaps"])


def test_dismissed_only_endpoint_asset_does_not_claim_the_plane(db):
    """Dismissed rows are out of the estate, so they cannot be used to claim
    a plane was covered."""
    add_collector(db)
    add_asset(db, asset_type="local_ai_client", status=STATUS_DISMISSED)
    add_asset(db, asset_type="llm_endpoint")
    assert attest_coverage(db, TENANT, now=NOW)["endpoint_plane_observed"] is False


# ---------------------------------------------------------------------------
# The statement is quoted verbatim into reports — keep it honest.
# ---------------------------------------------------------------------------


def test_statement_names_the_out_of_scope_count(db):
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED)
    add_asset(db, gov=GOV_UNGOVERNED)
    add_asset(db, gov=GOV_UNKNOWN)
    c = attest_coverage(db, TENANT, now=NOW)
    # 2 not routed: the ungoverned one plus the unreconciled one.
    assert "2 are not" in c["statement"]


def test_fully_covered_estate_reports_no_unrouted_gap(db):
    """The section must shrink as coverage improves, or operators stop
    reading it."""
    add_collector(db)
    add_asset(db, gov=GOV_GOVERNED, asset_type="local_ai_client")
    c = attest_coverage(db, TENANT, now=NOW)
    assert c["coverage_pct"] == pytest.approx(1.0)
    assert c["gaps"] == []
