# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Estate rollup exclusion — behavioural spec

Mission Control takes the worst health/verdict over a subtree, which is the
right policy and also why any real estate reads red forever. An exclusion
lets an operator excuse a system — but it is an ATTESTATION, not a mute:

  * it carries a reason, a note, who set it and (for temporary reasons) an
    expiry the validator refuses to omit;
  * the parent's judgement ignores the system, but the parent COUNTS it, the
    system's own report says so, and the digest lists it;
  * a lapsed exclusion re-enters the rollup by itself and raises an inbox
    item so the decision is re-made rather than the estate quietly going
    red again.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin import mission_control_cards as mcc
from app.api.v1.endpoints.admin.mission_control_cards import (
    create_mission_control_cards_router,
    effective_exclusion,
)
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_response.assurance_scheduler import _raise_if_exclusion_lapsed
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
ORG = "grp-org"
GOOD = "sys-good"
BAD = "sys-bad"


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _group(session, gid, name, *, parent=None, gtype=GroupType.CUSTOM, **extra):
    session.add(ResourceGroup(
        id=gid, tenant_id=TENANT, name=name, description=name,
        parent_group_id=parent, hierarchy_level=0, inherit_resources=True,
        is_active=True, is_system=False, group_type=gtype, **extra,
    ))


def _health(session, system_id, *, state, verdict):
    session.add(AISystemHealth(
        id=f"health-{system_id}", tenant_id=TENANT, ai_system_id=system_id,
        health_state=state, verdict_status=verdict, runs_24h=1,
    ))


def seed(session):
    _group(session, ORG, "Org")
    _group(session, GOOD, "Good", parent=ORG, gtype=GroupType.AI_SYSTEM, lifecycle_stage="active")
    _group(
        session, BAD, "Bad", parent=ORG, gtype=GroupType.AI_SYSTEM, lifecycle_stage="active",
        rollup_exclusion={
            "reason": "no_evidence_acknowledged", "note": "shadow system, onboarding Q4",
            "by": "admin-1", "at": _iso(_now()), "expires_at": _iso(_now() + timedelta(days=30)),
        },
    )
    session.flush()
    _health(session, GOOD, state="healthy", verdict="assured")
    _health(session, BAD, state="silent", verdict="violations")


@pytest.fixture(scope="module")
def harness():
    logger = BrutorLogger("rollup-exclusion-tests")
    h = build_admin_harness([
        create_mission_control_cards_router(logger),
        create_resource_groups_router(UsageLimitEnforcer(logger=logger), logger),
    ], logger_name="rollup-exclusion-tests")
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def _cold_cache():
    mcc._cache.clear()
    yield
    mcc._cache.clear()


def cards(h, **params):
    mcc._cache.clear()  # the estate is cached per tenant; every read here wants fresh truth
    r = h.client.get("/v1/admin/mission-control/cards", params=params, headers=h.headers(TENANT))
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["cards"]}


# --- the pure decision ------------------------------------------------------

class TestEffectiveExclusion:
    def _g(self, **kw):
        g = ResourceGroup(id="g", tenant_id=TENANT, name="g", description="g",
                          hierarchy_level=0, inherit_resources=True, is_active=True,
                          is_system=False, group_type=GroupType.AI_SYSTEM)
        for k, v in kw.items():
            setattr(g, k, v)
        return g

    def test_retired_and_deprecated_are_excluded_by_stage(self):
        for stage in ("retired", "deprecated"):
            assert effective_exclusion(self._g(lifecycle_stage=stage))["reason"] == stage
        assert effective_exclusion(self._g(lifecycle_stage="active")) is None

    def test_a_stored_exclusion_counts_until_it_expires(self):
        live = {"reason": "scheduled_offline", "expires_at": _iso(_now() + timedelta(hours=1))}
        lapsed = {"reason": "scheduled_offline", "expires_at": _iso(_now() - timedelta(hours=1))}
        assert effective_exclusion(self._g(rollup_exclusion=live)) is not None
        assert effective_exclusion(self._g(rollup_exclusion=lapsed)) is None

    def test_malformed_is_included_not_excused(self):
        assert effective_exclusion(self._g(rollup_exclusion={"note": "x"})) is None
        assert effective_exclusion(self._g(rollup_exclusion={"reason": "retired", "expires_at": "bogus"})) is None


# --- the estate ---------------------------------------------------------------

class TestRollup:
    def test_the_parent_ignores_the_excluded_system_but_counts_it(self, harness):
        org = cards(harness)[ORG]
        assert org["health_state"] == "healthy", org
        assert org["verdict_status"] == "assured"
        assert org["excluded_systems"] == 1
        assert org["systems"] == 2  # still part of the estate

    def test_the_excluded_card_still_shows_its_own_judgement(self, harness):
        bad = cards(harness, parent_id=ORG)[BAD]
        assert bad["excluded"] is True
        assert bad["exclusion_reason"] == "no_evidence_acknowledged"
        assert bad["exclusion_expires_at"] is not None
        # Not hidden: the card says silent/violations, the parent just does not read it.
        assert bad["health_state"] == "silent"
        assert bad["verdict_status"] == "violations"

    def test_excluded_cards_sort_after_the_ones_that_need_attention(self, harness):
        r = harness.client.get("/v1/admin/mission-control/cards",
                               params={"parent_id": ORG}, headers=harness.headers(TENANT))
        order = [c["id"] for c in r.json()["cards"]]
        assert order.index(GOOD) < order.index(BAD)


# --- the endpoint ---------------------------------------------------------------

class TestEndpoint:
    def _patch(self, h, gid, body):
        return h.client.patch(f"/v1/admin/resource-groups/{gid}", json=body, headers=h.headers(TENANT))

    def test_temporary_reasons_need_an_expiry(self, harness):
        r = self._patch(harness, GOOD, {"rollup_exclusion": {"reason": "scheduled_offline", "note": "maintenance"}})
        assert r.status_code == 422, r.text

    def test_setting_stamps_who_and_when_and_clearing_sends_null(self, harness):
        r = self._patch(harness, GOOD, {"rollup_exclusion": {
            "reason": "scheduled_offline", "note": "planned downtime",
            "expires_at": _iso(_now() + timedelta(days=2)),
        }})
        assert r.status_code == 200, r.text
        excl = r.json()["rollup_exclusion"]
        assert excl["reason"] == "scheduled_offline"
        assert excl["by"] and excl["at"], "an attestation names its author"
        assert cards(harness)[ORG]["excluded_systems"] == 2

        r = self._patch(harness, GOOD, {"rollup_exclusion": None})
        assert r.status_code == 200, r.text
        assert r.json()["rollup_exclusion"] is None
        assert cards(harness)[ORG]["excluded_systems"] == 1


# --- the lapse ------------------------------------------------------------------

def test_a_lapsed_exclusion_raises_one_inbox_item(harness):
    db = harness.Factory()
    try:
        g = db.query(ResourceGroup).filter(ResourceGroup.id == BAD).one()
        g.rollup_exclusion = {**g.rollup_exclusion, "expires_at": _iso(_now() - timedelta(minutes=5))}
        db.flush()
        _raise_if_exclusion_lapsed(db, g)
        _raise_if_exclusion_lapsed(db, g)  # idempotent on the fingerprint
        db.commit()
        items = db.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.tenant_id == TENANT, AssuranceInboxItem.ai_system_id == BAD,
        ).all()
        assert len(items) == 1
        assert "has lapsed" in items[0].title
        # And the estate reads the system again.
        mcc._cache.clear()
        assert cards(harness)[ORG]["health_state"] == "silent"
    finally:
        db.close()
