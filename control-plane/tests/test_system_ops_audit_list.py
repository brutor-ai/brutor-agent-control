# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# System Operations Console — audit list / filter / facets / CSV export

Covers four contracts:

  1. **list_audit filters apply correctly** — action / scope_kind /
     actor_admin_id / actor_type / time-window / free-text.
  2. **list_audit returns (rows, total)** — pagination doesn't leak
     past the total.
  3. **facets** returns the top-N value+count for every facetable
     column, scoped to the same envelope.
  4. **stream_csv_rows / AUDIT_CSV_HEADERS** — CSV columns are stable
     and the `summary` column carries the JSON-stringified payload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.system_operation_log import (
    ACTION_CACHE_FLUSH,
    ACTION_COUNTER_RESET,
    ACTOR_TYPE_SYSTEM_ADMIN,
    ACTOR_TYPE_TENANT_ADMIN,
    SCOPE_CACHE_LAYER,
    SCOPE_GROUP,
    SystemOperationLog,
)
from app.database.models.tenant import Tenant
from app.services.system_ops.audit_list import (
    AUDIT_CSV_HEADERS,
    facetable_fields,
    facets,
    get_audit_row,
    list_audit,
    stream_csv_rows,
)


# ---------------------------------------------------------------------------
# In-memory harness (same shape as test_system_ops_models.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    try:
        sess.add(
            Tenant(
                id="tenant-acme",
                name="tenant-acme",
                display_name="Acme",
                primary_contact_email="ops@example.com",
            )
        )
        sess.add(
            Tenant(
                id="tenant-other",
                name="tenant-other",
                display_name="Other",
                primary_contact_email="ops@example.com",
            )
        )
        sess.commit()
        yield sess
    finally:
        sess.close()
        engine.dispose()


@pytest.fixture
def seed_rows(session):
    """Seed a handful of audit rows spanning different actions, scopes,
    actors, and tenants so filter tests have a meaningful sample."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    base_rows = [
        # acme + cache_flush + system_admin
        SystemOperationLog(
            id="sol-acme-cache-1",
            tenant_id="tenant-acme",
            actor_admin_id="sysadmin-1",
            actor_type=ACTOR_TYPE_SYSTEM_ADMIN,
            action=ACTION_CACHE_FLUSH,
            scope_kind=SCOPE_CACHE_LAYER,
            scope_id="semantic",
            payload={"reason": "incident-1234"},
            before_state={"entries": 1024},
            after_state={"entries": 0},
            created_at=now - timedelta(hours=1),
        ),
        # acme + counter_reset + tenant_admin
        SystemOperationLog(
            id="sol-acme-reset-1",
            tenant_id="tenant-acme",
            actor_admin_id="tadmin-1",
            actor_type=ACTOR_TYPE_TENANT_ADMIN,
            action=ACTION_COUNTER_RESET,
            scope_kind=SCOPE_GROUP,
            scope_id="grp-alpha",
            payload={"reason": "promo-rollout"},
            created_at=now - timedelta(hours=2),
        ),
        # other tenant — must not leak into acme queries
        SystemOperationLog(
            id="sol-other-cache-1",
            tenant_id="tenant-other",
            actor_admin_id="sysadmin-1",
            actor_type=ACTOR_TYPE_SYSTEM_ADMIN,
            action=ACTION_CACHE_FLUSH,
            scope_kind=SCOPE_CACHE_LAYER,
            scope_id="policy",
            created_at=now - timedelta(hours=3),
        ),
        # acme + outside the default 24h window
        SystemOperationLog(
            id="sol-acme-old-1",
            tenant_id="tenant-acme",
            actor_admin_id="sysadmin-1",
            actor_type=ACTOR_TYPE_SYSTEM_ADMIN,
            action=ACTION_COUNTER_RESET,
            scope_kind=SCOPE_GROUP,
            scope_id="grp-beta",
            created_at=now - timedelta(days=30),
        ),
    ]
    for r in base_rows:
        session.add(r)
    session.commit()
    return base_rows


# ---------------------------------------------------------------------------
# list_audit
# ---------------------------------------------------------------------------


class TestListAudit:
    def test_returns_rows_and_total_for_tenant_scope(self, session, seed_rows):
        rows, total = list_audit(session, "tenant-acme", limit=50)
        ids = {r["id"] for r in rows}
        assert ids == {
            "sol-acme-cache-1",
            "sol-acme-reset-1",
            "sol-acme-old-1",
        }
        assert total == 3
        # Other tenant's row must NOT leak in.
        assert "sol-other-cache-1" not in ids

    def test_orders_newest_first_by_default(self, session, seed_rows):
        rows, _ = list_audit(session, "tenant-acme", limit=50)
        # Default sort_order='desc' → newest created_at first.
        # sol-acme-cache-1 is now-1h, sol-acme-reset-1 is now-2h,
        # sol-acme-old-1 is now-30d.
        assert [r["id"] for r in rows] == [
            "sol-acme-cache-1",
            "sol-acme-reset-1",
            "sol-acme-old-1",
        ]

    def test_action_filter_or_within(self, session, seed_rows):
        rows, total = list_audit(
            session, "tenant-acme", actions=[ACTION_CACHE_FLUSH]
        )
        assert total == 1
        assert rows[0]["id"] == "sol-acme-cache-1"

    def test_scope_kind_filter(self, session, seed_rows):
        rows, total = list_audit(
            session, "tenant-acme", scope_kinds=[SCOPE_GROUP]
        )
        assert total == 2
        assert {r["id"] for r in rows} == {
            "sol-acme-reset-1",
            "sol-acme-old-1",
        }

    def test_actor_filter(self, session, seed_rows):
        rows, _ = list_audit(
            session, "tenant-acme", actor_admin_ids=["tadmin-1"]
        )
        assert {r["id"] for r in rows} == {"sol-acme-reset-1"}

    def test_actor_type_filter(self, session, seed_rows):
        rows, _ = list_audit(
            session,
            "tenant-acme",
            actor_types=[ACTOR_TYPE_TENANT_ADMIN],
        )
        assert {r["id"] for r in rows} == {"sol-acme-reset-1"}

    def test_time_window_excludes_out_of_range_rows(self, session, seed_rows):
        # Default window: last 24h.
        end = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        start = end - timedelta(hours=24)
        rows, total = list_audit(session, "tenant-acme", start=start, end=end)
        # sol-acme-old-1 (30 days ago) must NOT be in the result.
        ids = {r["id"] for r in rows}
        assert "sol-acme-old-1" not in ids
        assert total == 2

    def test_free_text_q_searches_payload(self, session, seed_rows):
        rows, total = list_audit(
            session, "tenant-acme", q="incident-1234"
        )
        # Only the cache_flush row carries that string in payload.
        assert total == 1
        assert rows[0]["id"] == "sol-acme-cache-1"

    def test_free_text_q_searches_scope_id(self, session, seed_rows):
        rows, _ = list_audit(session, "tenant-acme", q="grp-alpha")
        assert {r["id"] for r in rows} == {"sol-acme-reset-1"}

    def test_limit_and_offset(self, session, seed_rows):
        rows, total = list_audit(session, "tenant-acme", limit=1, offset=0)
        assert total == 3
        assert len(rows) == 1
        rows2, _ = list_audit(session, "tenant-acme", limit=1, offset=1)
        assert rows2[0]["id"] != rows[0]["id"]

    def test_tenant_none_returns_cross_tenant(self, session, seed_rows):
        rows, total = list_audit(session, None)
        # Both tenants' rows surfaced — sysadmin path.
        assert total == 4


# ---------------------------------------------------------------------------
# facets
# ---------------------------------------------------------------------------


class TestFacets:
    def test_facetable_fields_includes_canonical_set(self):
        f = facetable_fields()
        assert {"action", "scope_kind", "actor_admin_id", "actor_type"}.issubset(
            f.keys()
        )

    def test_facets_returns_value_count_per_field(self, session, seed_rows):
        out = facets(session, "tenant-acme")
        assert "action" in out
        action_values = {v["value"]: v["count"] for v in out["action"]}
        assert action_values[ACTION_CACHE_FLUSH] == 1
        assert action_values[ACTION_COUNTER_RESET] == 2

    def test_facets_respect_filter_envelope(self, session, seed_rows):
        # Narrow to just cache_flush — actor facet should drop the
        # tenant_admin entry that only appears on counter_reset rows.
        out = facets(
            session, "tenant-acme", actions=[ACTION_CACHE_FLUSH]
        )
        actor_values = {v["value"] for v in out["actor_admin_id"]}
        assert "sysadmin-1" in actor_values
        assert "tadmin-1" not in actor_values


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


class TestCsvExport:
    def test_headers_are_stable(self):
        assert AUDIT_CSV_HEADERS[0] == "id"
        assert AUDIT_CSV_HEADERS[-1] == "summary"
        assert "tenant_id" in AUDIT_CSV_HEADERS
        assert "action" in AUDIT_CSV_HEADERS

    def test_stream_csv_rows_yields_per_row_dicts(self, session, seed_rows):
        rows = list(stream_csv_rows(session, "tenant-acme"))
        assert len(rows) == 3
        # Newest first ordering preserved into CSV stream — the
        # auditor's spreadsheet matches the table.
        assert rows[0]["id"] == "sol-acme-cache-1"
        # Summary carries JSON-stringified payload.
        assert "incident-1234" in rows[0]["summary"]

    def test_stream_csv_rows_handles_none_payload(self, session, seed_rows):
        # sol-other-cache-1 has no payload — summary should be empty.
        rows = list(stream_csv_rows(session, "tenant-other"))
        assert rows[0]["summary"] == ""


# ---------------------------------------------------------------------------
# Single-row fetch (drawer detail)
# ---------------------------------------------------------------------------


class TestGetAuditRow:
    def test_returns_row_when_scope_matches(self, session, seed_rows):
        row = get_audit_row(session, "tenant-acme", "sol-acme-cache-1")
        assert row is not None
        assert row.action == ACTION_CACHE_FLUSH

    def test_returns_none_when_tenant_mismatch(self, session, seed_rows):
        # Cross-tenant id lookup must NOT leak — defence in depth.
        row = get_audit_row(session, "tenant-acme", "sol-other-cache-1")
        assert row is None

    def test_returns_none_for_unknown_id(self, session, seed_rows):
        row = get_audit_row(session, "tenant-acme", "sol-nonexistent")
        assert row is None
