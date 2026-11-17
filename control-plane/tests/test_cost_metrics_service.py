# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
CostMetricsService — FinOps aggregations over usage_logs (Mission Control Cost
tab). Seeds governed + observed ledger rows on SQLite and asserts the
per-dimension breakdown, the governed/observed split, and budget burn-down +
projection.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group import ResourceGroup, resource_group_llm_models
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.usage_log import UsageLog
from app.services.cost_metrics_service import CostMetricsService

TENANT = "t-cost"
REQUIRED = [
    UsageLog.__table__,
    EndUser.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
    resource_group_llm_models,
    ResourceGroupMembership.__table__,
    ResourceGroup.__table__,
]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _log(**kw) -> UsageLog:
    base = dict(
        tenant_id=TENANT,
        resource_type="llm",
        resource_id="r-1",
        request_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        source="gateway",
        currency="USD",
    )
    base.update(kw)
    return UsageLog(**base)


@pytest.fixture()
def svc():
    return CostMetricsService()


@pytest.fixture()
def seeded(db):
    db.add_all([
        # governed
        _log(provider="openai", model_name="gpt-5.2", group_id="g-sales",
             group_name="Sales", user_id=1, ancestry_path="g-sales",
             total_tokens=100, estimated_cost=1.0, source="gateway"),
        _log(provider="openai", model_name="gpt-5.2", group_id="g-sales",
             group_name="Sales", user_id=2, ancestry_path="g-sales",
             total_tokens=200, estimated_cost=2.0, source="gateway"),
        # observed (imported)
        _log(provider="anthropic", model_name="claude-opus-4-6", group_id="g-imp",
             group_name="chat", user_id=3, ancestry_path="g-imp",
             total_tokens=1000, estimated_cost=10.0,
             source="import:anthropic_enterprise_analytics"),
    ])
    db.add(EndUser(id=1, tenant_id=TENANT, username="alice", email="a@x.com",
                   display_name="Alice", password_hash="x"))
    db.commit()
    return db


def test_breakdown_totals_and_source_split(seeded, svc):
    r = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0)
    assert r["totals"]["cost"] == pytest.approx(13.0)
    assert r["totals"]["tokens"] == 1300
    assert r["totals"]["requests"] == 3
    assert r["by_source"]["governed"]["cost"] == pytest.approx(3.0)
    assert r["by_source"]["observed"]["cost"] == pytest.approx(10.0)


def test_breakdown_by_dimensions(seeded, svc):
    r = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0)
    prov = {p["key"]: p["cost"] for p in r["by_provider"]}
    assert prov == {"openai": pytest.approx(3.0), "anthropic": pytest.approx(10.0)}
    teams = {t["key"]: t["name"] for t in r["by_team"]}
    assert teams["g-sales"] == "Sales"
    # user dimension resolves username via the EndUser join
    users = {u["key"]: u["name"] for u in r["by_user"]}
    assert users[1] == "alice"


def test_breakdown_group_filter_scopes_all_figures(seeded, svc):
    # Design Note 002 global filter — scoping to a Resource Group (an AI System is an
    # RG with group_type='ai_system') restricts totals and every dimension to
    # that RG's rows. The observed anthropic row is on g-imp, so it drops out.
    r = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0, group_id="g-sales")
    assert r["totals"]["cost"] == pytest.approx(3.0)
    assert r["totals"]["requests"] == 2
    prov = {p["key"] for p in r["by_provider"]}
    assert prov == {"openai"}  # anthropic (other RG) excluded
    assert r["by_source"]["observed"]["cost"] == pytest.approx(0.0)


def test_breakdown_group_filter_unknown_group_is_empty(seeded, svc):
    r = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0, group_id="g-nope")
    assert r["totals"]["cost"] == pytest.approx(0.0)
    assert r["totals"]["requests"] == 0


def test_by_team_resolves_name_from_resource_group_when_group_name_null(db, svc):
    # Imported usage rows don't carry group_name — the By Team breakdown must
    # still show the resource group's real name by joining resource_groups
    # (regression: it previously fell back to the bare group_id).
    db.add(ResourceGroup(
        id="g-prod", tenant_id=TENANT, name="chat",
        display_name="Anthropic · chat",
        hierarchy_level=0, hierarchy_path="g-prod", is_active=True,
    ))
    db.add(_log(provider="anthropic", model_name="claude-opus-4-6",
                group_id="g-prod", group_name=None, ancestry_path="g-prod",
                total_tokens=500, estimated_cost=5.0,
                source="import:anthropic_enterprise_analytics"))
    db.commit()

    r = svc.breakdown(db, tenant_id=TENANT, period_hours=0)
    team = {t["key"]: t["name"] for t in r["by_team"]}
    # display_name wins over the NULL usage_logs.group_name
    assert team["g-prod"] == "Anthropic · chat"


def test_breakdown_plane_filter(seeded, svc):
    gov = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0, plane="governed")
    assert gov["totals"]["cost"] == pytest.approx(3.0)
    assert all(p["key"] == "openai" for p in gov["by_provider"])
    obs = svc.breakdown(seeded, tenant_id=TENANT, period_hours=0, plane="observed")
    assert obs["totals"]["cost"] == pytest.approx(10.0)
    assert all(p["key"] == "anthropic" for p in obs["by_provider"])


def test_budgets_burndown_and_projection(db, svc):
    # RG with a $4 monthly budget; seed $3 spend this month → 75% util.
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    db.add(ResourceGroup(
        id="g-sales", tenant_id=TENANT, name="Sales",
        hierarchy_level=0, hierarchy_path="g-sales", is_active=True,
        llm_global_limits={"budget": {"monthly_limit_usd": 4.0, "monthly_warning_percent": 80}},
    ))
    db.add(_log(provider="openai", model_name="gpt-5.2", group_id="g-sales",
                group_name="Sales", ancestry_path="g-sales",
                total_tokens=300, estimated_cost=3.0, created_at=month_start))
    db.commit()

    r = svc.budgets(db, tenant_id=TENANT)
    monthly = [b for b in r["budgets"] if b["period"] == "monthly"]
    assert len(monthly) == 1
    b = monthly[0]
    assert b["group_id"] == "g-sales"
    assert b["limit_usd"] == 4.0
    assert b["spend_usd"] == pytest.approx(3.0)
    assert b["utilization_pct"] == pytest.approx(75.0, abs=0.5)
    assert b["over_warning"] is False  # 75% < 80% warning
    # projection extrapolates month-to-date spend across the full month
    assert b["projected_usd"] >= b["spend_usd"]


# --------------------------------------------------------------------------- #
# monthly() — the LLM Economics tab's trend series
# --------------------------------------------------------------------------- #

def _month_shift(dt: datetime, months_back: int) -> datetime:
    y, m = dt.year, dt.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return dt.replace(year=y, month=m, day=1)


def test_monthly_series_buckets_and_unit_cost(db, svc):
    now = datetime.now(timezone.utc)
    prev = _month_shift(now, 1)
    db.add_all([
        _log(model_name="gpt-5.5", total_tokens=1000, estimated_cost=2.0,
             created_at=prev),
        _log(model_name="gpt-5.5", total_tokens=1000, estimated_cost=2.0,
             created_at=prev),
        _log(model_name="gpt-5.2", total_tokens=500, estimated_cost=0.5,
             created_at=now),
        # non-LLM rows must not pollute the series
        _log(resource_type="mcp", total_tokens=None, estimated_cost=0.0,
             created_at=now),
    ])
    db.commit()

    out = svc.monthly(db, tenant_id=TENANT, months=3)
    assert out["currency"] == "USD"
    months = {row["month"]: row for row in out["data"]}
    prev_key = prev.strftime("%Y-%m")
    now_key = now.strftime("%Y-%m")

    assert months[prev_key]["requests"] == 2
    assert months[prev_key]["cost_usd"] == pytest.approx(4.0)
    # unit cost = cost / requests * 1000
    assert months[prev_key]["unit_cost_per_1k_usd"] == pytest.approx(2000.0)
    assert months[now_key]["requests"] == 1
    assert months[now_key]["unit_cost_per_1k_usd"] == pytest.approx(500.0)


def test_monthly_window_excludes_older_months(db, svc):
    now = datetime.now(timezone.utc)
    old = _month_shift(now, 5)
    db.add_all([
        _log(total_tokens=10, estimated_cost=1.0, created_at=old),
        _log(total_tokens=10, estimated_cost=1.0, created_at=now),
    ])
    db.commit()
    out = svc.monthly(db, tenant_id=TENANT, months=2)
    keys = [r["month"] for r in out["data"]]
    assert old.strftime("%Y-%m") not in keys
    assert now.strftime("%Y-%m") in keys


def test_monthly_group_and_plane_scoping(db, svc):
    now = datetime.now(timezone.utc)
    db.add_all([
        _log(group_id="g-a", estimated_cost=1.0, total_tokens=1, created_at=now),
        _log(group_id="g-b", estimated_cost=7.0, total_tokens=1, created_at=now),
        _log(group_id="g-a", estimated_cost=50.0, total_tokens=1, created_at=now,
             source="import:anthropic_enterprise_analytics"),
    ])
    db.commit()

    scoped = svc.monthly(db, tenant_id=TENANT, months=1, group_id="g-a")
    assert scoped["data"][0]["cost_usd"] == pytest.approx(51.0)

    governed = svc.monthly(db, tenant_id=TENANT, months=1,
                           group_id="g-a", plane="governed")
    assert governed["data"][0]["cost_usd"] == pytest.approx(1.0)

    observed = svc.monthly(db, tenant_id=TENANT, months=1, plane="observed")
    assert observed["data"][0]["cost_usd"] == pytest.approx(50.0)


def test_budgets_tolerates_malformed_llm_global_limits(db, svc):
    # `null::jsonb || object` concatenation produces an ARRAY — one bad
    # group must not 500 the whole burn-down.
    db.add(ResourceGroup(id="g-bad", tenant_id=TENANT, name="bad",
                         hierarchy_path="bad",
                         llm_global_limits=[None, {"budget": {"monthly_limit_usd": 5}}]))
    db.add(ResourceGroup(id="g-ok", tenant_id=TENANT, name="ok",
                         hierarchy_path="ok",
                         llm_global_limits={"budget": {"monthly_limit_usd": 100}}))
    db.commit()
    out = svc.budgets(db, tenant_id=TENANT)
    names = {i["group_id"] for i in out["budgets"]}
    assert "g-ok" in names and "g-bad" not in names


# ---------------------------------------------------------------------------
# Calendar windows (start/end) + previous-window compare — FinOps
# ---------------------------------------------------------------------------

def _at(days_ago: float) -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


@pytest.fixture()
def two_windows(db):
    """Spend in two adjacent 10-day windows: 'recent' (days 0–10) and
    'previous' (days 10–20), with one provider only present previously."""
    db.add_all([
        _log(provider="openai", model_name="m1", created_at=_at(2),
             total_tokens=10, estimated_cost=4.0),
        _log(provider="openai", model_name="m1", created_at=_at(5),
             total_tokens=10, estimated_cost=6.0),
        _log(provider="openai", model_name="m1", created_at=_at(12),
             total_tokens=10, estimated_cost=3.0),
        _log(provider="mistral", model_name="m2", created_at=_at(15),
             total_tokens=10, estimated_cost=7.0),
    ])
    db.commit()
    return db


class TestCalendarWindows:
    def test_explicit_range_overrides_period_hours(self, two_windows, svc):
        r = svc.breakdown(
            two_windows, tenant_id=TENANT, period_hours=1,  # would see nothing
            start=_at(10), end=None,
        )
        assert r["totals"]["cost"] == pytest.approx(10.0)
        assert r["window"]["start"] is not None

    def test_bounded_range_excludes_outside_rows(self, two_windows, svc):
        r = svc.breakdown(
            two_windows, tenant_id=TENANT, period_hours=0,
            start=_at(20), end=_at(10),
        )
        assert r["totals"]["cost"] == pytest.approx(10.0)  # only the old window
        assert {x["key"] for x in r["by_provider"]} == {"openai", "mistral"}

    def test_series_respects_range(self, two_windows, svc):
        r = svc.series(
            two_windows, tenant_id=TENANT, dimension="provider",
            period_hours=0, start=_at(10),
        )
        total = sum(v["cost"] for d in r["data"] for v in d["values"].values())
        assert total == pytest.approx(10.0)
        assert r["grain"] == "day"


class TestCompare:
    def test_previous_window_totals_and_per_row_values(self, two_windows, svc):
        r = svc.breakdown(
            two_windows, tenant_id=TENANT, period_hours=240, compare=True,
        )
        assert r["previous"] is not None
        assert r["previous"]["totals"]["cost"] == pytest.approx(10.0)
        openai = next(x for x in r["by_provider"] if x["key"] == "openai")
        assert openai["cost"] == pytest.approx(10.0)
        assert openai["prev_cost"] == pytest.approx(3.0)

    def test_key_absent_previously_reads_zero_not_missing(self, two_windows, svc):
        # openai is the only current-window provider; mistral existed only
        # before. A current row with no previous spend must carry 0.0 —
        # "new spend" IS the delta, not missing data.
        r = svc.breakdown(
            two_windows, tenant_id=TENANT, period_hours=240, compare=True,
        )
        assert all("prev_cost" in row for row in r["by_provider"])

    def test_all_time_has_no_previous(self, two_windows, svc):
        r = svc.breakdown(
            two_windows, tenant_id=TENANT, period_hours=0, compare=True,
        )
        assert r["previous"] is None

    def test_compare_off_adds_nothing(self, two_windows, svc):
        r = svc.breakdown(two_windows, tenant_id=TENANT, period_hours=240)
        assert r["previous"] is None
        assert all("prev_cost" not in row for row in r["by_provider"])


# ---------------------------------------------------------------------------
# Cost composition series
# ---------------------------------------------------------------------------

class TestSeries:
    def test_daily_buckets_by_provider_with_other_fold(self, db, svc):
        for i, (prov, cost) in enumerate([
            ("p1", 50.0), ("p2", 20.0), ("p3", 5.0), ("p4", 2.0),
        ]):
            db.add(_log(provider=prov, model_name="m", created_at=_at(1),
                        total_tokens=1, estimated_cost=cost))
        db.commit()
        r = svc.series(db, tenant_id=TENANT, dimension="provider",
                       period_hours=0, top_n=2)
        keys = [k["key"] for k in r["keys"]]
        assert keys == ["p1", "p2", "other"]
        day = r["data"][0]["values"]
        # The tail folds into "other" so the bands still sum to the truth.
        assert day["other"]["cost"] == pytest.approx(7.0)
        assert sum(v["cost"] for v in day.values()) == pytest.approx(77.0)

    def test_unknown_dimension_raises(self, db, svc):
        with pytest.raises(ValueError):
            svc.series(db, tenant_id=TENANT, dimension="user")

    def test_team_dimension_resolves_names(self, db, svc):
        db.add(ResourceGroup(
            id="g-x", tenant_id=TENANT, name="gx", display_name="Team X",
            group_type="team", hierarchy_level=0, hierarchy_path="/gx",
            inherit_resources=True, is_active=True, is_system=False,
        ))
        db.add(_log(provider="p", model_name="m", group_id="g-x",
                    created_at=_at(1), total_tokens=1, estimated_cost=1.0))
        db.commit()
        r = svc.series(db, tenant_id=TENANT, dimension="team", period_hours=0)
        assert r["keys"][0] == {"key": "g-x", "name": "Team X"}


# ---------------------------------------------------------------------------
# Savings — cost avoided, honestly labelled
# ---------------------------------------------------------------------------

class TestSavings:
    @staticmethod
    def _plog(db, **kw):
        from app.database.models.proxy_log import ProxyLog
        base = dict(
            id=str(uuid.uuid4()),
            tenant_id=TENANT,
            proxy_type="llm",
            timestamp=datetime.now(timezone.utc),
            http_method="POST",
            endpoint="/v1/proxy/llm/chat/completions",
            end_user_session_id="sess-1",
            success=True,
        )
        base.update(kw)
        db.add(ProxyLog(**base))

    @pytest.fixture()
    def savings_db(self, db):
        from app.database.models.proxy_log import ProxyLog
        ProxyLog.__table__.create(bind=db.get_bind(), checkfirst=True)
        # 2 non-cached requests at $2 avg, 3 cache hits, one provider
        # prompt-cache discount of $0.50.
        self._plog(db, llm_cache_hit=False, llm_estimated_cost_usd=1.0)
        self._plog(db, llm_cache_hit=False, llm_estimated_cost_usd=3.0,
                   prompt_cache_savings_usd=0.5)
        for _ in range(3):
            self._plog(db, llm_cache_hit=True, llm_estimated_cost_usd=0.0,
                       llm_tokens_saved=100)
        db.commit()
        return db

    def test_cache_savings_estimated_from_noncached_average(self, savings_db, svc):
        r = svc.savings(savings_db, tenant_id=TENANT, period_hours=0)
        g = r["gateway_cache"]
        assert g["hits"] == 3
        assert g["basis_avg_cost_per_noncached_request_usd"] == pytest.approx(2.0)
        assert g["estimated_avoided_usd"] == pytest.approx(6.0)
        assert g["estimated"] is True
        assert g["tokens_saved"] == 300

    def test_prompt_cache_savings_are_measured(self, savings_db, svc):
        r = svc.savings(savings_db, tenant_id=TENANT, period_hours=0)
        assert r["provider_prompt_cache"]["savings_usd"] == pytest.approx(0.5)
        assert r["provider_prompt_cache"]["measured"] is True
        assert r["total_avoided_usd"] == pytest.approx(6.5)

    def test_no_traffic_is_zero_not_error(self, db, svc):
        from app.database.models.proxy_log import ProxyLog
        ProxyLog.__table__.create(bind=db.get_bind(), checkfirst=True)
        r = svc.savings(db, tenant_id=TENANT, period_hours=0)
        assert r["gateway_cache"]["hits"] == 0
        assert r["gateway_cache"]["basis_avg_cost_per_noncached_request_usd"] is None
        assert r["total_avoided_usd"] == 0.0


# ---------------------------------------------------------------------------
# Multi-system scope — comma-separated group ids (portfolio view)
# ---------------------------------------------------------------------------

class TestMultiGroupScope:
    @pytest.fixture()
    def three_groups(self, db):
        for gid, cost in (("g-a", 1.0), ("g-b", 2.0), ("g-c", 4.0)):
            db.add(_log(provider="p", model_name="m", group_id=gid,
                        ancestry_path=gid, total_tokens=10, estimated_cost=cost))
        db.commit()
        return db

    def test_two_of_three_systems(self, three_groups, svc):
        r = svc.breakdown(three_groups, tenant_id=TENANT, period_hours=0,
                          group_id="g-a,g-b")
        assert r["totals"]["cost"] == pytest.approx(3.0)
        assert {x["key"] for x in r["by_team"]} == {"g-a", "g-b"}

    def test_single_id_still_works(self, three_groups, svc):
        r = svc.breakdown(three_groups, tenant_id=TENANT, period_hours=0,
                          group_id="g-c")
        assert r["totals"]["cost"] == pytest.approx(4.0)

    def test_series_accepts_the_set(self, three_groups, svc):
        r = svc.series(three_groups, tenant_id=TENANT, dimension="provider",
                       period_hours=0, group_id="g-a,g-c")
        total = sum(v["cost"] for d in r["data"] for v in d["values"].values())
        assert total == pytest.approx(5.0)

    def test_blank_and_trailing_commas_are_harmless(self, three_groups, svc):
        r = svc.breakdown(three_groups, tenant_id=TENANT, period_hours=0,
                          group_id="g-a,, ")
        assert r["totals"]["cost"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# FOCUS export — daily-grain rows in spec columns
# ---------------------------------------------------------------------------

class TestFocusExport:
    @pytest.fixture()
    def focus_db(self, db):
        from datetime import timedelta
        base = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
        db.add(ResourceGroup(
            id="g-sys", tenant_id=TENANT, name="copilot", display_name="Copilot",
            group_type="team", hierarchy_level=0, hierarchy_path="/copilot",
            inherit_resources=True, is_active=True, is_system=False,
        ))
        # Two LLM rows same day/model/group → one FOCUS row; one MCP row.
        db.add_all([
            _log(provider="openai", model_name="gpt-5.2", group_id="g-sys",
                 created_at=base, total_tokens=100, estimated_cost=1.0),
            _log(provider="openai", model_name="gpt-5.2", group_id="g-sys",
                 created_at=base + timedelta(hours=2), total_tokens=200,
                 estimated_cost=2.0),
            _log(provider="openai", model_name=None, group_id="g-sys",
                 resource_type="mcp", created_at=base, total_tokens=0,
                 estimated_cost=0.0),
        ])
        db.commit()
        return db

    def test_rows_aggregate_per_day_and_carry_spec_columns(self, focus_db, svc):
        rows = list(svc.focus_rows(focus_db, tenant_id=TENANT, period_hours=0))
        assert all(set(r.keys()) == set(svc.FOCUS_COLUMNS) for r in rows)
        llm = next(r for r in rows if r["ResourceType"] == "llm")
        assert llm["BilledCost"] == pytest.approx(3.0)      # 2 rows summed
        assert llm["EffectiveCost"] == pytest.approx(3.0)
        assert llm["ConsumedQuantity"] == 300
        assert llm["ConsumedUnit"] == "tokens"
        assert llm["ChargePeriodStart"] == "2026-03-10T00:00:00Z"
        assert llm["BillingPeriodStart"] == "2026-03-01T00:00:00Z"
        assert llm["BillingPeriodEnd"].startswith("2026-03-31")
        assert llm["SubAccountId"] == "g-sys"
        assert llm["SubAccountName"] == "Copilot"
        assert llm["ProviderName"] == "openai"
        assert llm["ServiceCategory"] == "AI and Machine Learning"

    def test_non_llm_rows_bill_in_calls_not_tokens(self, focus_db, svc):
        rows = list(svc.focus_rows(focus_db, tenant_id=TENANT, period_hours=0))
        mcp = next(r for r in rows if r["ResourceType"] == "mcp")
        assert mcp["ConsumedUnit"] == "calls"
        assert mcp["ConsumedQuantity"] == 1

    def test_plane_tag_reflects_provenance(self, focus_db, svc):
        rows = list(svc.focus_rows(focus_db, tenant_id=TENANT, period_hours=0))
        import json as _json
        llm = next(r for r in rows if r["ResourceType"] == "llm")
        assert _json.loads(llm["Tags"]) == {"brutor/plane": "governed"}

    def test_window_bounds_are_respected(self, focus_db, svc):
        rows = list(svc.focus_rows(
            focus_db, tenant_id=TENANT, period_hours=0,
            start=datetime(2026, 4, 1, tzinfo=timezone.utc)))
        assert rows == []
