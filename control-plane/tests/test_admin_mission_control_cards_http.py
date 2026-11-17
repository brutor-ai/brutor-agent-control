# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Mission Control cards API — HTTP behavioural spec

The RG-card landing page through the wire, on the
shared admin harness: real auth, real permissions, full-schema SQLite.

The two rules the endpoint exists to enforce get the most attention:

  * **windows scale measurements, not judgments** — spend/runs move with
    the window selector; health/verdict/T-class are pinned to the
    materialized health row and must be IDENTICAL across windows;
  * **rollups are sums for additive metrics, worst-of for judgments** —
    an org card over nine healthy systems and one in violation reads
    Violations, never an average.

Plus the level contract (root / drill-down / flat search), pseudo-cards
appearing only on the unfiltered root, tenant isolation, sorting,
pagination, the contract cost-per-task bound annotation, and the
explainers info-box source.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from tests.run_outcome_defaults import work_outcome

from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.usage_log import UsageLog
from app.api.v1.endpoints.admin import mission_control_cards as mcc
from app.api.v1.endpoints.admin.mission_control_cards import (
    create_mission_control_cards_router,
)
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
OTHER = "t2"

ORG = "grp-org"
TEAM = "grp-team"
COPILOT = "sys-copilot"
SCORING = "sys-scoring"
T2_SYS = "sys-t2"


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


def _group(session, gid, tenant, name, *, parent=None, gtype=GroupType.CUSTOM,
           envelope=None, **extra):
    session.add(ResourceGroup(
        id=gid, tenant_id=tenant, name=name, description=name,
        parent_group_id=parent, hierarchy_level=0, inherit_resources=True,
        is_active=True, is_system=False, group_type=gtype,
        operating_envelope=envelope, **extra,
    ))


_run_n = {"n": 0}


def _run(session, tenant, system_id, *, cost=0.01, hours_ago=1.0,
         state="completed", llm_calls=1, tokens=0, in_bytes=0, out_bytes=0,
         rg=None):
    _run_n["n"] += 1
    n = _run_n["n"]
    session.add(AISystemRun(
        id=f"run-{n}", tenant_id=tenant, root_task_id=f"root-{n}",
        ai_system_id=system_id, resource_group_id=rg,
        started_at=_now() - timedelta(hours=hours_ago),
        ended_at=_now() - timedelta(hours=hours_ago),
        terminal_state=state, **work_outcome(state), finalized_by="idle",
        llm_call_count=llm_calls, action_count=1,
        total_cost_usd=cost,
        total_tokens=tokens, input_bytes=in_bytes, output_bytes=out_bytes,
    ))


def _health(session, tenant, system_id, *, state="healthy", verdict="assured",
            t_class="T(n)", k_median=1.0, a_max=0, runs_24h=0):
    session.add(AISystemHealth(
        id=f"health-{system_id}", tenant_id=tenant, ai_system_id=system_id,
        health_state=state, verdict_status=verdict, t_class=t_class,
        k_median=k_median, a_max=a_max, runs_24h=runs_24h,
    ))


def _drift(session, tenant, system_id, *, status="active", n=1):
    for i in range(n):
        session.add(AISystemDriftEvent(
            id=f"drift-{system_id}-{status}-{i}", tenant_id=tenant,
            ai_system_id=system_id, drift_class="cost",
            metric_key="cost_per_run", status=status,
            # A closed finding always carries its kind.
            resolution_kind=None if status in ("active", "acknowledged") else "accepted_change",
        ))


def seed(session):
    """t1: org → team → copilot, plus scoring directly under org.

    Copilot is the troubled system: drifting, in violation, T(n·k),
    2 open findings, cost-per-task over its contract bound. Scoring is
    the healthy counterweight. The org card must read like Copilot.
    """
    _group(session, ORG, TENANT, "Acme AI")
    _group(session, TEAM, TENANT, "Support Team", parent=ORG)
    _group(
        session, COPILOT, TENANT, "Support Copilot", parent=TEAM,
        gtype=GroupType.AI_SYSTEM,
        envelope={"max_avg_cost_per_completed_task_usd": 0.005},
        system_kind="agent", lifecycle_stage="active",
        owner="ops@acme.example", intended_use="support triage",
        eu_ai_act_risk_tier="high",
    )
    _group(
        session, SCORING, TENANT, "Lead Scoring", parent=ORG,
        gtype=GroupType.AI_SYSTEM, system_kind="workflow",
    )
    # These models declare no relationship() to ResourceGroup, so the
    # unit of work will not order the inserts for us — flush the groups
    # before anything that carries an FK to them.
    session.flush()

    _health(session, TENANT, COPILOT, state="drifting", verdict="violations",
            t_class="T(n·k)", k_median=5.0, a_max=0, runs_24h=4)
    _health(session, TENANT, SCORING, state="healthy", verdict="assured",
            t_class="T(n)")

    # Copilot: 4 recent runs (in 24h) + 6 older (5 days ago), $0.01 each.
    for _ in range(4):
        _run(session, TENANT, COPILOT, cost=0.01, hours_ago=2)
    for _ in range(6):
        _run(session, TENANT, COPILOT, cost=0.01, hours_ago=120)
    # Scoring: 5 runs, $0.002 each, all recent.
    for _ in range(5):
        _run(session, TENANT, SCORING, cost=0.002, hours_ago=2)

    # Findings: 2 open + 1 resolved (resolved must not count).
    _drift(session, TENANT, COPILOT, status="active", n=1)
    _drift(session, TENANT, COPILOT, status="acknowledged", n=1)
    _drift(session, TENANT, COPILOT, status="resolved", n=1)

    # Unattributed governed traffic → pseudo-card.
    for _ in range(3):
        _run(session, TENANT, None, cost=0.02, hours_ago=2,
             tokens=400, in_bytes=1000, out_bytes=500)

    # Imported spend attributed to a Resource Group: it belongs on that
    # group's card, and rolls up its ancestors like any other spend.
    session.add(UsageLog(
        tenant_id=TENANT, resource_type="llm", resource_id="gpt-x",
        request_id="req-observed-1", source="import:openai_enterprise",
        group_id=SCORING, estimated_cost=7.5, total_tokens=1200,
    ))
    # Imported spend the resolver could not place — it must still be
    # counted somewhere, so it joins the unattributed card.
    session.add(UsageLog(
        tenant_id=TENANT, resource_type="llm", resource_id="gpt-x",
        request_id="req-observed-orphan", source="import:openai_enterprise",
        estimated_cost=1.25, total_tokens=300,
    ))
    # Governed usage row — must NOT count as observed.
    session.add(UsageLog(
        tenant_id=TENANT, resource_type="llm", resource_id="gpt-x",
        request_id="req-governed-1", source="gateway", estimated_cost=99.0,
    ))

    # t2: one system with traffic — invisible from t1 and vice versa.
    _group(session, T2_SYS, OTHER, "Tenant2 Bot", gtype=GroupType.AI_SYSTEM)
    session.flush()
    _health(session, OTHER, T2_SYS, state="suspended", verdict="violations")
    for _ in range(2):
        _run(session, OTHER, T2_SYS, cost=1.0, hours_ago=2)


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness(
        [create_mission_control_cards_router(BrutorLogger("mc-cards-tests"))]
    )
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The endpoint caches per (tenant, window) for 60s — a lifetime in a
    test module. Every test starts cold so seeds are always visible."""
    mcc._cache.clear()
    yield
    mcc._cache.clear()


def get_cards(harness, tenant=TENANT, **params):
    r = harness.client.get(
        "/v1/admin/mission-control/cards",
        params=params, headers=harness.headers(tenant),
    )
    assert r.status_code == 200, r.text
    return r.json()


def by_id(payload):
    return {c["id"]: c for c in payload["cards"]}


class TestLevels:
    def test_root_returns_only_root_groups_plus_pseudo_cards(self, harness):
        cards = by_id(get_cards(harness))
        assert ORG in cards
        # Children never leak onto the root level.
        assert TEAM not in cards and COPILOT not in cards
        # The harness Default group is a root group too.
        assert f"group-default-{TENANT}" in cards

    def test_drill_down_returns_the_children_of_parent(self, harness):
        cards = by_id(get_cards(harness, parent_id=ORG))
        assert set(cards) == {TEAM, SCORING}

    def test_leaf_level_shows_the_system_card(self, harness):
        cards = by_id(get_cards(harness, parent_id=TEAM))
        assert set(cards) == {COPILOT}
        c = cards[COPILOT]
        assert c["is_group"] is False
        assert c["system_kind"] == "agent"
        assert c["lifecycle_stage"] == "active"
        assert c["eu_ai_act_risk_tier"] == "high"

    def test_unknown_parent_is_404(self, harness):
        r = harness.client.get(
            "/v1/admin/mission-control/cards",
            params={"parent_id": "no-such-group"},
            headers=harness.headers(),
        )
        assert r.status_code == 404

    def test_breadcrumb_is_root_first(self, harness):
        payload = get_cards(harness, parent_id=TEAM)
        assert [p["id"] for p in payload["path"]] == [ORG, TEAM]
        assert payload["path"][0]["name"] == "Acme AI"

    def test_root_has_an_empty_breadcrumb(self, harness):
        assert get_cards(harness)["path"] == []


class TestRollups:
    def test_additive_metrics_sum_over_the_subtree(self, harness):
        org = by_id(get_cards(harness, window="30d"))[ORG]
        # Copilot 10 × $0.01 + Scoring 5 × $0.002 = $0.11 of governed
        # spend, plus Scoring's $7.50 imported — 15 runs either way, since
        # imported usage is spend without a run.
        assert org["spend"] == pytest.approx(0.11 + 7.5)
        assert org["runs"] == 15
        assert org["completed_runs"] == 15
        assert org["findings"] == 2
        assert org["systems"] == 2
        assert org["is_group"] is True

    def test_judgments_are_worst_of_never_averages(self, harness):
        org = by_id(get_cards(harness))[ORG]
        # Scoring is healthy/assured/T(n); Copilot drags everything down.
        assert org["health_state"] == "drifting"
        assert org["verdict_status"] == "violations"
        assert org["t_class"] == "T(n·k)"
        assert org["t_class_contains"] is True

    def test_a_system_card_owns_its_class_rather_than_containing_it(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM))[COPILOT]
        assert c["t_class"] == "T(n·k)"
        assert c["t_class_contains"] is False
        assert c["k_median"] == 5.0

    def test_group_cost_per_task_is_blended_and_says_so(self, harness):
        org = by_id(get_cards(harness, window="30d"))[ORG]
        # $0.11 over 15 completed tasks (rounded to 6 decimals on the wire).
        assert org["cost_per_task"] == pytest.approx(0.11 / 15, abs=1e-6)
        assert org["cost_per_task_blended"] is True

    def test_resolved_findings_never_count(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM))[COPILOT]
        assert c["findings"] == 2

    def test_live_propagates_up_from_a_live_leaf(self, harness):
        org = by_id(get_cards(harness))[ORG]
        assert org["live"] is True


class TestWindows:
    def test_windows_scale_measurements(self, harness):
        month = by_id(get_cards(harness, parent_id=TEAM, window="30d"))[COPILOT]
        day = by_id(get_cards(harness, parent_id=TEAM, window="24h"))[COPILOT]
        assert month["runs"] == 10
        assert day["runs"] == 4
        assert day["spend"] == pytest.approx(0.04)

    def test_windows_never_move_judgments(self, harness):
        month = by_id(get_cards(harness, parent_id=TEAM, window="30d"))[COPILOT]
        day = by_id(get_cards(harness, parent_id=TEAM, window="24h"))[COPILOT]
        alltime = by_id(get_cards(harness, parent_id=TEAM, window="all"))[COPILOT]
        for c in (day, alltime):
            assert c["health_state"] == month["health_state"] == "drifting"
            assert c["verdict_status"] == month["verdict_status"] == "violations"
            assert c["t_class"] == month["t_class"] == "T(n·k)"

    def test_the_pinned_note_travels_with_every_response(self, harness):
        assert "pinned" in get_cards(harness)["judgments_pinned_note"]

    def test_all_time_includes_everything(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM, window="all"))[COPILOT]
        assert c["runs"] == 10

    def test_an_unknown_window_is_422(self, harness):
        r = harness.client.get(
            "/v1/admin/mission-control/cards",
            params={"window": "1000d"}, headers=harness.headers(),
        )
        assert r.status_code == 422


class TestCostPerTaskBound:
    def test_a_breached_contract_bound_is_annotated(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM))[COPILOT]
        # cpt $0.01 vs the declared MEAN bound $0.005. The card figure is
        # an average, so it is compared against the average bound — the
        # per-run ceiling is a different term, enforced at the gateway.
        assert c["cost_per_task"] == pytest.approx(0.01)
        assert c["cpt_bound"] == 0.005
        assert c["cpt_breached"] is True

    def test_a_system_without_a_bound_is_never_breached(self, harness):
        c = by_id(get_cards(harness, parent_id=ORG))[SCORING]
        assert c["cpt_bound"] is None
        assert c["cpt_breached"] is False


class TestObservedSpend:
    """Imported usage is a Resource Group's spend, not a card of its own."""

    def test_imported_spend_lands_on_its_own_group(self, harness):
        c = by_id(get_cards(harness, parent_id=ORG))[SCORING]
        # 5 governed runs at $0.002 plus $7.50 imported.
        assert c["spend"] == pytest.approx(0.01 + 7.5)
        assert c["observed_spend"] == pytest.approx(7.5)
        assert c["tokens"] >= 1200

    def test_imported_spend_never_becomes_a_run_or_a_cost_per_task(self, harness):
        c = by_id(get_cards(harness, parent_id=ORG))[SCORING]
        assert c["runs"] == 5                       # unchanged by the import
        assert c["cost_per_task"] == pytest.approx(0.002)

    def test_imported_spend_rolls_up_the_ancestors(self, harness):
        org = by_id(get_cards(harness))[ORG]
        assert org["observed_spend"] == pytest.approx(7.5)
        assert org["spend"] == pytest.approx(
            by_id(get_cards(harness, parent_id=ORG))[TEAM]["spend"] + 0.01 + 7.5
        )

    def test_governed_usage_rows_are_not_observed(self, harness):
        # The fixture's $99 'gateway' usage row must not appear anywhere.
        assert all(
            c["observed_spend"] < 99
            for c in get_cards(harness)["cards"]
        )

    def test_there_is_no_bought_ai_card(self, harness):
        assert "pseudo-observed" not in by_id(get_cards(harness))


class TestPseudoCards:
    def test_unattributed_appears_at_root(self, harness):
        cards = by_id(get_cards(harness))
        ua = cards["pseudo-unattributed"]
        assert ua["pseudo"] == "unattributed"
        assert ua["runs"] == 3
        # 3 governed runs at $0.02 plus the imported row with no group.
        assert ua["spend"] == pytest.approx(0.06 + 1.25)
        assert ua["observed_spend"] == pytest.approx(1.25)
        assert ua["note"]

    def test_unattributed_carries_tokens_and_bytes(self, harness):
        # The card must not show real spend next to "0 tok · 0 B" by
        # building from runs and spend only. 3 runs × 400 tokens plus
        # the orphan import's 300; bytes come from the runs alone.
        ua = by_id(get_cards(harness))["pseudo-unattributed"]
        assert ua["tokens"] == 3 * 400 + 300
        assert ua["data_bytes"] == 3 * (1000 + 500)

    def test_pseudo_cards_sort_last(self, harness):
        ids = [c["id"] for c in get_cards(harness)["cards"]]
        assert ids[-1] == "pseudo-unattributed"

    def test_pseudo_cards_never_appear_on_drill_down(self, harness):
        cards = by_id(get_cards(harness, parent_id=ORG))
        assert not any(k.startswith("pseudo-") for k in cards)

    def test_pseudo_cards_never_appear_in_search_or_filter(self, harness):
        assert not any(
            c["pseudo"] for c in get_cards(harness, q="pseudo")["cards"]
        )
        assert not any(
            c["pseudo"]
            for c in get_cards(harness, group_type="ai_system")["cards"]
        )

    def test_pseudo_cards_paginate_like_any_card(self, harness):
        # Root is Acme AI + the harness Default group + the pseudo card:
        # total must be stable across offsets, with the pseudo card
        # occupying the tail position.
        full = get_cards(harness, sort="name")
        tail = get_cards(harness, sort="name", offset=2)
        assert tail["total"] == full["total"] == 3
        assert [c["id"] for c in tail["cards"]] == ["pseudo-unattributed"]


class TestSearchAndFilter:
    def test_search_is_flat_across_all_levels(self, harness):
        cards = by_id(get_cards(harness, q="copilot"))
        assert set(cards) == {COPILOT}

    def test_search_is_case_insensitive(self, harness):
        assert COPILOT in by_id(get_cards(harness, q="SUPPORT CO"))

    def test_search_matches_nothing_gracefully(self, harness):
        payload = get_cards(harness, q="zzz-no-match")
        assert payload["cards"] == [] and payload["total"] == 0

    def test_group_type_filter_narrows_root(self, harness):
        cards = by_id(get_cards(harness, q="e", group_type="ai_system"))
        assert all(c["group_type"] == "ai_system" for c in cards.values())


class TestSorting:
    def test_attention_puts_the_violation_first(self, harness):
        ids = [c["id"] for c in get_cards(harness, sort="attention")["cards"]]
        assert ids[0] == ORG  # carries Copilot's violations verdict

    def test_spend_sort_is_descending(self, harness):
        cards = [c for c in get_cards(harness, sort="spend")["cards"]
                 if not c["pseudo"]]
        spends = [c["spend"] for c in cards]
        assert spends == sorted(spends, reverse=True)

    def test_name_sort_is_alphabetical(self, harness):
        cards = [c for c in get_cards(harness, sort="name")["cards"]
                 if not c["pseudo"]]
        names = [c["name"].lower() for c in cards]
        assert names == sorted(names)

    def test_an_unknown_sort_is_422(self, harness):
        r = harness.client.get(
            "/v1/admin/mission-control/cards",
            params={"sort": "vibes"}, headers=harness.headers(),
        )
        assert r.status_code == 422


class TestPagination:
    def test_limit_and_offset_page_through_stable_ordering(self, harness):
        first = get_cards(harness, sort="name", limit=1, offset=0)
        second = get_cards(harness, sort="name", limit=1, offset=1)
        assert first["total"] == second["total"] >= 2
        assert first["cards"][0]["id"] != second["cards"][0]["id"]

    def test_total_counts_beyond_the_page(self, harness):
        payload = get_cards(harness, limit=1)
        assert payload["total"] > len(payload["cards"])


class TestTenantIsolation:
    def test_t2_sees_only_its_own_estate(self, harness):
        cards = by_id(get_cards(harness, tenant=OTHER))
        assert T2_SYS in cards
        assert ORG not in cards
        assert "pseudo-observed" not in cards  # t1's observed spend

    def test_t1_never_sees_t2(self, harness):
        assert T2_SYS not in by_id(get_cards(harness))
        assert get_cards(harness, q="Tenant2")["cards"] == []


    def test_anonymous_is_rejected(self, harness):
        r = harness.client.get("/v1/admin/mission-control/cards")
        assert r.status_code in (401, 403)


class TestGovernanceGaps:
    def test_a_fully_described_system_has_no_gaps(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM))[COPILOT]
        assert c["governance_gaps"] == 0

    def test_missing_declarations_are_counted(self, harness):
        # Scoring declares no owner/intended_use/lifecycle/risk tier.
        c = by_id(get_cards(harness, parent_id=ORG))[SCORING]
        assert c["governance_gaps"] == 4

    def test_org_gaps_roll_up_as_sums(self, harness):
        org = by_id(get_cards(harness))[ORG]
        assert org["governance_gaps"] == 4


class TestTrend:
    def test_a_system_with_traffic_has_a_trend(self, harness):
        c = by_id(get_cards(harness, parent_id=TEAM, window="30d"))[COPILOT]
        assert c["trend"] and sum(c["trend"]) == pytest.approx(0.10)

    def test_group_trends_merge_child_series(self, harness):
        org = by_id(get_cards(harness, window="30d"))[ORG]
        # The merged series carries both planes: governed runs and the
        # imported row attributed to Scoring.
        assert sum(org["trend"]) == pytest.approx(0.11 + 7.5)


class TestExplainers:
    def test_explainers_are_served_to_any_admin(self, harness):
        r = harness.client.get(
            "/v1/admin/mission-control/explainers",
            headers=harness.headers(),
        )
        assert r.status_code == 200
        ex = r.json()["explainers"]
        for key in ("t_class", "cost_per_task", "worst_of", "unattributed",
                    "observed_plane", "two_projections", "findings"):
            assert ex[key]["title"] and ex[key]["body"]

    def test_explainers_require_auth(self, harness):
        r = harness.client.get("/v1/admin/mission-control/explainers")
        assert r.status_code in (401, 403)


class TestTokensAndDataTransfer:
    """Tokens + payload bytes on the cards.

    Tokens are the native tokenomics unit; data_bytes is the run
    ledger's input+output payload mass. Both are additive metrics: they
    sum over subtrees exactly like spend, and their trend series merge
    the same way.
    """

    def test_system_card_carries_tokens_and_bytes(self, harness):
        harness.seed(lambda s: (
            _run(s, TENANT, COPILOT, cost=0.02, tokens=1200,
                 in_bytes=4000, out_bytes=6000),
            _run(s, TENANT, COPILOT, cost=0.03, tokens=1800,
                 in_bytes=1000, out_bytes=2000),
        ))
        card = by_id(get_cards(harness, parent_id=TEAM))[COPILOT]
        assert card["tokens"] == 3000
        assert card["data_bytes"] == 13000
        assert isinstance(card["trend_tokens"], list)
        assert sum(card["trend_tokens"]) == 3000
        assert sum(card["trend_bytes"]) == 13000

    def test_group_rollup_sums_tokens_and_bytes_over_subtree(self, harness):
        # Delta-based: the module-scoped harness accumulates seeds across
        # tests, so assert what THIS seeding adds to the org rollup.
        before = by_id(get_cards(harness))[ORG]
        mcc._cache.clear()  # the endpoint memoizes per (tenant, window)
        harness.seed(lambda s: (
            _run(s, TENANT, COPILOT, tokens=1000, in_bytes=500, out_bytes=500),
            _run(s, TENANT, SCORING, tokens=2500, in_bytes=100, out_bytes=400),
        ))
        org = by_id(get_cards(harness))[ORG]
        assert org["tokens"] - before["tokens"] == 3500
        assert org["data_bytes"] - before["data_bytes"] == 1500
        assert sum(org["trend_tokens"]) - sum(before["trend_tokens"]) == 3500


# ---------------------------------------------------------------------------
# Direct gateway traffic on groups that are not AI Systems.
#
# Defect: the E2ETests custom group's own Usage tab read 288 requests /
# $1.41 / 297k tokens while its Mission Control card read
# "$0.00 · 0 tok · 0 B" — same for the system Unbound group. Card
# measurements came only from the run ledger keyed by `ai_system_id`
# (plus imported usage), and gateway traffic routed under a group that
# is not an AI System carries no `ai_system_id`, so its money never
# reached its card. The Usage tab reads `usage_logs.group_id`; the card
# must account for the same traffic — without double counting an AI
# System's runs, whose gateway rows mirror them.
# ---------------------------------------------------------------------------

D_E2E = "grp-e2e"
D_TEAM = "grp-direct-team"
D_SYS = "sys-direct"
D_DEFAULT = f"group-default-{TENANT}"   # the harness's system group

_ulog_n = {"n": 0}


def _ulog(session, group_id, *, cost=None, tokens=None, mcp_bytes=None,
          resource_type="llm", hours_ago=1.0, source="gateway"):
    _ulog_n["n"] += 1
    session.add(UsageLog(
        tenant_id=TENANT, group_id=group_id, resource_type=resource_type,
        resource_id="gpt-x" if resource_type == "llm" else "srv-x",
        request_id=f"req-direct-{_ulog_n['n']}", source=source,
        estimated_cost=cost, total_tokens=tokens,
        mcp_data_size_bytes=mcp_bytes, status_code=200,
        created_at=_now() - timedelta(hours=hours_ago),
    ))


def seed_direct(session):
    """A custom root group with only gateway traffic, a team with direct
    traffic over an AI System whose gateway rows mirror its runs, and
    traffic on the system Default group."""
    _group(session, D_E2E, TENANT, "E2ETests")
    _group(session, D_TEAM, TENANT, "Direct Team", gtype=GroupType.TEAM)
    _group(session, D_SYS, TENANT, "Direct Bot", parent=D_TEAM,
           gtype=GroupType.AI_SYSTEM, system_kind="agent")
    session.flush()

    # E2ETests: two LLM calls + one MCP call today, one LLM call 5 days ago.
    _ulog(session, D_E2E, cost=0.5, tokens=1000)
    _ulog(session, D_E2E, cost=0.25, tokens=500)
    _ulog(session, D_E2E, resource_type="mcp", mcp_bytes=2048)
    _ulog(session, D_E2E, cost=1.0, tokens=100, hours_ago=120)

    # The system Default group carries traffic too (e.g. health checks).
    _ulog(session, D_DEFAULT, cost=0.1, tokens=10)

    # The team's own direct traffic.
    _ulog(session, D_TEAM, cost=0.3, tokens=300)

    # The AI System: 2 runs, and the gateway rows those runs produced.
    for _ in range(2):
        _run(session, TENANT, D_SYS, cost=0.01, tokens=100)
        _ulog(session, D_SYS, cost=0.01, tokens=100)


@pytest.fixture(scope="module")
def direct_harness():
    h = build_admin_harness(
        [create_mission_control_cards_router(BrutorLogger("mc-cards-direct"))]
    )
    h.seed(seed_direct)
    yield h
    h.close()


class TestDirectGroupTraffic:
    def test_a_group_with_only_gateway_traffic_shows_its_usage(self, direct_harness):
        c = by_id(get_cards(direct_harness, window="24h"))[D_E2E]
        assert c["spend"] == pytest.approx(0.75)
        assert c["tokens"] == 1500
        assert c["data_bytes"] == 2048
        assert sum(c["trend"]) == pytest.approx(0.75)
        assert sum(c["trend_tokens"]) == 1500

    def test_direct_traffic_is_governed_and_never_a_run(self, direct_harness):
        c = by_id(get_cards(direct_harness, window="24h"))[D_E2E]
        assert c["observed_spend"] == 0
        assert c["runs"] == 0
        assert c["cost_per_task"] is None

    def test_the_window_scales_direct_traffic(self, direct_harness):
        c = by_id(get_cards(direct_harness, window="30d"))[D_E2E]
        assert c["spend"] == pytest.approx(1.75)
        assert c["tokens"] == 1600

    def test_the_system_default_group_shows_its_usage(self, direct_harness):
        c = by_id(get_cards(direct_harness, window="24h"))[D_DEFAULT]
        assert c["spend"] == pytest.approx(0.1)
        assert c["tokens"] == 10

    def test_an_ai_system_is_not_double_counted(self, direct_harness):
        # Its gateway rows mirror its runs: the run ledger stays the one
        # source for a system card.
        c = by_id(get_cards(direct_harness, parent_id=D_TEAM, window="24h"))[D_SYS]
        assert c["spend"] == pytest.approx(0.02)
        assert c["tokens"] == 200
        assert c["runs"] == 2

    def test_direct_traffic_rolls_up_with_the_subtree(self, direct_harness):
        team = by_id(get_cards(direct_harness, window="24h"))[D_TEAM]
        assert team["spend"] == pytest.approx(0.3 + 0.02)
        assert team["tokens"] == 300 + 200
        assert team["runs"] == 2
        # Cost per task stays a run measure: direct traffic never dilutes it.
        assert team["cost_per_task"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# The Unattributed card as a to-do list, and system groups.
#
# a198_run_resource_group stamps every run with the group it started in. Runs started in the
# Unbound system group are platform calls (health checks, judges,
# embeddings) that can never belong to an AI System, so they must not sit
# on the to-do list. The rest are broken down by group so "attribute it"
# names where to go. System groups read as "system", not "custom".
# ---------------------------------------------------------------------------

U_UNBOUND = "grp-unbound"
U_E2E = "grp-ua-e2e"
U_TEAM = "grp-ua-team"
U_SYS = "sys-ua"
UNBOUND_PURPOSE = "Platform calls: health checks, judges, embeddings"


def seed_unattributed(session):
    # The tenant's Unbound system group, as provisioning creates it.
    session.add(ResourceGroup(
        id=U_UNBOUND, tenant_id=TENANT, name="Unbound", description="Unbound",
        hierarchy_level=0, inherit_resources=True, is_active=True,
        is_system=True, group_type=GroupType.CUSTOM,
    ))
    _group(session, U_E2E, TENANT, "E2ETests")
    _group(session, U_TEAM, TENANT, "Sales", gtype=GroupType.TEAM)
    _group(session, U_SYS, TENANT, "Sales Bot", parent=U_TEAM,
           gtype=GroupType.AI_SYSTEM)
    session.flush()

    # E2ETests: 3 unattributed runs today + 1 five days ago.
    for _ in range(3):
        _run(session, TENANT, None, cost=0.01, hours_ago=2, rg=U_E2E)
    _run(session, TENANT, None, cost=0.01, hours_ago=120, rg=U_E2E)
    # Sales: 1 unattributed run today.
    _run(session, TENANT, None, cost=0.02, hours_ago=2, rg=U_TEAM)
    # Unbound: platform calls — never on the to-do list.
    for _ in range(5):
        _run(session, TENANT, None, cost=0.001, hours_ago=2, rg=U_UNBOUND)
    # Pre-a198 / unbackfilled: group unknown, still counted.
    for _ in range(2):
        _run(session, TENANT, None, cost=0.05, hours_ago=2)
    # Attributed to a system: never unattributed, whatever group it started in.
    _run(session, TENANT, U_SYS, cost=0.5, hours_ago=2, rg=U_TEAM)


@pytest.fixture(scope="module")
def ua_harness():
    h = build_admin_harness(
        [create_mission_control_cards_router(BrutorLogger("mc-cards-ua"))]
    )
    h.seed(seed_unattributed)
    yield h
    h.close()


class TestUnattributedToDoList:
    def test_unbound_platform_runs_are_not_unattributed(self, ua_harness):
        ua = by_id(get_cards(ua_harness, window="24h"))["pseudo-unattributed"]
        # 3 E2ETests + 1 Sales + 2 unknown-group; the 5 Unbound runs excluded.
        assert ua["runs"] == 6
        assert ua["spend"] == pytest.approx(0.03 + 0.02 + 0.10)

    def test_runs_with_no_recorded_group_stay_counted(self, ua_harness):
        ua = by_id(get_cards(ua_harness, window="24h"))["pseudo-unattributed"]
        unknown = [r for r in ua["unattributed_by_group"] if r["group_id"] is None]
        assert len(unknown) == 1
        assert unknown[0]["runs"] == 2
        assert unknown[0]["spend"] == pytest.approx(0.10)

    def test_breakdown_is_per_group_sorted_by_runs(self, ua_harness):
        ua = by_id(get_cards(ua_harness, window="24h"))["pseudo-unattributed"]
        rows = ua["unattributed_by_group"]
        assert [(r["group_id"], r["runs"]) for r in rows] == [
            (U_E2E, 3), (None, 2), (U_TEAM, 1),
        ]
        e2e = rows[0]
        assert e2e["group_name"] == "E2ETests"
        assert e2e["spend"] == pytest.approx(0.03)
        assert sum(r["runs"] for r in rows) == ua["runs"]
        assert all(r["group_id"] != U_UNBOUND for r in rows)

    def test_breakdown_follows_the_window(self, ua_harness):
        ua = by_id(get_cards(ua_harness, window="30d"))["pseudo-unattributed"]
        rows = {r["group_id"]: r for r in ua["unattributed_by_group"]}
        assert rows[U_E2E]["runs"] == 4

    def test_real_group_cards_carry_no_breakdown(self, ua_harness):
        c = by_id(get_cards(ua_harness))[U_E2E]
        assert not c.get("unattributed_by_group")


class TestSystemGroups:
    def test_system_groups_say_so(self, ua_harness):
        cards = by_id(get_cards(ua_harness))
        assert cards[U_UNBOUND]["is_system"] is True
        assert cards[f"group-default-{TENANT}"]["is_system"] is True
        assert cards[U_E2E]["is_system"] is False

    def test_unbound_states_its_purpose(self, ua_harness):
        cards = by_id(get_cards(ua_harness))
        assert cards[U_UNBOUND]["purpose"] == UNBOUND_PURPOSE
        assert cards[f"group-default-{TENANT}"]["purpose"] is None
        assert cards[U_E2E]["purpose"] is None
