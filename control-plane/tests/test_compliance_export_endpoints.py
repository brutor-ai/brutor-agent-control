# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance export acceptance — one regression test per compliance
regime that drives the export endpoint end-to-end and asserts the
auditor-visible JSON shape.

The five tests cover the five export surfaces:

  * GDPR Article 30 — Records of Processing Activities
  * SOC 2 — control-coverage with sample row IDs per CC
  * HIPAA — PHI access log with `source` distinguishing detected
    PHI from RG-marked PHI
  * EU AI Act — per-tier call counts + per-RG classification map
  * ISO 42001 — total activity + per-proxy-type breakdown

Each test mounts the compliance router on a stand-alone FastAPI
app, overrides the `get_db` and `get_current_admin` dependencies
with a `MagicMock` that returns deterministic rows, and hits the
endpoint via `TestClient`. The assertions pin:

  * the HTTP envelope shape (top-level keys, count, breakdown)
  * the per-row shape the export emits
  * the framework-specific fields the auditor's spreadsheet
    template relies on (e.g. GDPR's `idp_user_email`, HIPAA's
    `source`, EU AI Act's tier names)

This is the auditor's contract — any drift here means an export
template breaks silently.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.admin.compliance import create_compliance_admin_router
from app.constants.admin import AdminType
from app.core.admin_user.dependencies import get_current_admin
from app.database import get_db


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _make_admin():
    """A tenant-scoped admin that resolves to tenant 'export-t'. These routes
    need COMPLIANCE_READ; this admin holds it."""
    admin = SimpleNamespace(tenant_id="export-t", has_permission=lambda _p: True)
    return admin, AdminType.TENANT_ADMIN_TYPE


def _build_client(mock_db: MagicMock) -> TestClient:
    """Mount the compliance router on a stand-alone FastAPI app and
    override the DB + auth dependencies with the supplied mock."""
    app = FastAPI()
    router = create_compliance_admin_router(logging.getLogger("test"))
    # The router is mounted at "/admin/compliance" by its own prefix.
    app.include_router(router, prefix="/api/v1")

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_admin] = _make_admin
    return TestClient(app)


def _stub_execute(rows_by_query: dict[str, list[tuple]]):
    """Build a `db.execute(text(...), params)` side-effect that
    matches the substring of the SQL to the right pre-fixed rows.

    The endpoints all build their SQL via `text("SELECT ...")` —
    we key on a distinguishing substring (e.g. `compliance_tags @>`
    + the regime tag literal) so multiple queries inside one
    handler get distinct results."""
    def _side_effect(query, params=None, *args, **kwargs):
        sql = str(query).strip()
        # _count_rows_by_tag uses the :tag_json parameter to carry
        # the regime tag (not in the SQL text). Build a combined
        # haystack so a needle like 'iso_42001' matches even when
        # the SQL is the regime-agnostic COUNT(*) helper.
        haystack = sql
        if isinstance(params, dict):
            for v in params.values():
                if isinstance(v, str):
                    haystack += " " + v
        for needle, rows in rows_by_query.items():
            if needle in haystack:
                cursor = MagicMock()
                cursor.fetchall.return_value = rows
                # _count_rows_by_tag calls `.first()`; other handlers
                # use `.fetchall()`. Some call `.scalar()`. Expose
                # all three so the same stub services every regime.
                if rows:
                    cursor.fetchone.return_value = rows[0]
                    cursor.first.return_value = rows[0]
                    cursor.scalar.return_value = (
                        rows[0][0] if rows[0] else None
                    )
                else:
                    cursor.fetchone.return_value = None
                    cursor.first.return_value = None
                    cursor.scalar.return_value = 0
                return cursor
        # No match — return an empty cursor so the handler renders
        # the "no rows" branch rather than NoneType-attribute-error.
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = None
        cursor.first.return_value = None
        cursor.scalar.return_value = 0
        return cursor

    return _side_effect


# ---------------------------------------------------------------------------
# GDPR Article 30
# ---------------------------------------------------------------------------


class TestGdprArticle30Export:
    def test_returns_envelope_and_per_row_pii_fields(self):
        """Auditors filter on `idp_user_email` first — losing that
        column would silently break their RoPA template."""
        mock_db = MagicMock()
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_db.execute.side_effect = _stub_execute({
            'gdpr_art_30': [
                ("log-1", ts, "llm", "chat_completion",
                 "gateway-u-1", "idp-u-1", "alice@example.com"),
                ("log-2", ts, "mcp", "tools_call",
                 None, "idp-u-2", "bob@example.com"),
            ],
        })
        client = _build_client(mock_db)
        resp = client.get(
            "/api/v1/admin/compliance/gdpr/article-30",
            params={"from": "2026-05-01T00:00:00Z",
                    "to": "2026-06-30T23:59:59Z"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "export-t"
        assert body["count"] == 2
        records = body["records"]
        assert len(records) == 2

        # Pin the per-row field set — every key here is one an
        # auditor's RoPA template depends on.
        first = records[0]
        for key in (
            "id", "timestamp", "proxy_type", "proxy_subtype",
            "gateway_user_id", "idp_user_id", "idp_user_email",
        ):
            assert key in first, f"GDPR export row missing {key!r}"
        assert first["idp_user_email"] == "alice@example.com"
        # Second row's gateway_user_id is None — must surface as
        # null, not the string "None".
        assert records[1]["gateway_user_id"] is None


# ---------------------------------------------------------------------------
# SOC 2 control coverage
# ---------------------------------------------------------------------------


class TestSoc2ControlCoverageExport:
    def test_returns_per_control_counts_and_sample_ids(self):
        """The auditor samples N row IDs per CC. The contract:
        five CCs are reported (CC6.1, CC6.6, CC6.7, CC7.1, CC7.2)
        and each carries `evidence_count` + `sample_row_ids` —
        even if zero/empty."""
        mock_db = MagicMock()
        # Different stub rows for each tag. Use the tag literal as
        # the disambiguator — both count and sample queries embed
        # it via the CAST(:tag_json AS jsonb) param. The
        # _count_rows_by_tag helper executes a raw COUNT(*) — we
        # cheat and return the same rowset; the helper uses
        # .scalar().
        mock_db.execute.side_effect = _stub_execute({
            'soc2.cc6.1': [(7,), ("log-cc6-1-a",), ("log-cc6-1-b",)],
            'soc2.cc6.6': [(3,), ("log-cc6-6-a",)],
            'soc2.cc6.7': [(0,)],
            'soc2.cc7.1': [(1,), ("log-cc7-1-a",)],
            'soc2.cc7.2': [(5,), ("log-cc7-2-a",)],
        })
        client = _build_client(mock_db)
        resp = client.get(
            "/api/v1/admin/compliance/soc2/control-coverage",
            params={"sample_size": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        controls = {c["control"]: c for c in body["controls"]}
        # Every CC in the SOC 2 mapping table must show up — a
        # missing entry would mean the auditor's "did we cover
        # CC6.7?" question has no answer.
        for cc in (
            "soc2.cc6.1", "soc2.cc6.6", "soc2.cc6.7",
            "soc2.cc7.1", "soc2.cc7.2",
        ):
            assert cc in controls, f"SOC 2 export missing {cc}"
            assert "evidence_count" in controls[cc]
            assert "sample_row_ids" in controls[cc]


# ---------------------------------------------------------------------------
# HIPAA PHI access
# ---------------------------------------------------------------------------


class TestHipaaPhiAccessExport:
    def test_distinguishes_detected_vs_rg_marked_source(self):
        """The export's `source` field separates "we caught PHI on
        the wire" (`detected`) from "operator pre-declared the RG"
        (`rg_marked`). Auditors care about which is which — the
        two have different remediation paths."""
        mock_db = MagicMock()
        ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_db.execute.side_effect = _stub_execute({
            # hipaa.phi appears as a literal in the WHERE clause.
            "hipaa.phi": [
                # detected (compliance_tags contains "hipaa.phi")
                ("log-d-1", ts, "llm", "gw-u-1",
                 "idp-u-1", "x@example.com", "rg-1",
                 ["hipaa.phi", "gdpr_art_30"]),
                # rg_marked (compliance_tags contains
                # "hipaa.rg_marked" only — handler classifies by
                # absence of "hipaa.phi" in the tags list).
                ("log-r-1", ts, "mcp", "gw-u-2",
                 "idp-u-2", "y@example.com", "rg-2",
                 ["hipaa.rg_marked"]),
            ],
        })
        client = _build_client(mock_db)
        resp = client.get("/api/v1/admin/compliance/hipaa/phi-access")
        assert resp.status_code == 200, resp.text
        records = resp.json()["records"]
        assert len(records) == 2

        # Pin per-row shape.
        for key in (
            "id", "timestamp", "proxy_type",
            "gateway_user_id", "idp_user_id", "idp_user_email",
            "resource_group_id", "source",
        ):
            assert key in records[0]

        # The auditor uses `source` to split detection-driven from
        # operator-declared PHI.
        sources = {r["id"]: r["source"] for r in records}
        assert sources["log-d-1"] == "detected"
        assert sources["log-r-1"] == "rg_marked"


# ---------------------------------------------------------------------------
# EU AI Act risk classification
# ---------------------------------------------------------------------------


class TestEuAiActRiskClassificationExport:
    def test_emits_calls_by_tier_and_rg_classifications(self):
        """The auditor wants both the per-tier call count AND the
        per-RG tier map. The export envelope's two fields are the
        contract."""
        mock_db = MagicMock()
        mock_db.execute.side_effect = _stub_execute({
            "eu_ai_act.minimal": [(2,)],
            "eu_ai_act.limited": [(0,)],
            "eu_ai_act.high": [(11,)],
            "eu_ai_act.unacceptable": [(0,)],
            "FROM resource_groups": [
                ("rg-a", "Customer Support", "limited"),
                ("rg-b", "Hiring Decisions", "high"),
            ],
        })
        client = _build_client(mock_db)
        resp = client.get(
            "/api/v1/admin/compliance/eu-ai-act/risk-classification",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "export-t"
        # All four tiers reported, even when zero — empty buckets
        # are evidence the engine ran, not silent gaps.
        by_tier = body["calls_by_tier"]
        for tier in ("minimal", "limited", "high", "unacceptable"):
            assert tier in by_tier, f"tier {tier} missing from export"
        # Per-RG map carries `id`, `name`, `tier`.
        rgs = {rg["id"]: rg for rg in body["rg_classifications"]}
        assert rgs["rg-b"]["tier"] == "high"
        assert rgs["rg-b"]["name"] == "Hiring Decisions"


# ---------------------------------------------------------------------------
# ISO 42001 activity
# ---------------------------------------------------------------------------


class TestIso42001ActivityExport:
    def test_returns_total_and_per_proxy_type_breakdown(self):
        """ISO 42001 requires AI management system activity
        logging. The export's contract: a single total plus a
        per-proxy-type breakdown so the auditor can see which
        surfaces (LLM / MCP / Skill) carried the load."""
        mock_db = MagicMock()
        # Order matters — both SQL statements embed the tag literal,
        # so the GROUP BY (more specific) must be matched first.
        mock_db.execute.side_effect = _stub_execute({
            # Breakdown — GROUP BY proxy_type. Match first because
            # the GROUP BY string is unique to the breakdown query.
            "GROUP BY proxy_type": [
                ("llm", 28),
                ("mcp", 11),
                ("skill", 3),
            ],
            # Total scalar — _count_rows_by_tag fires COUNT(*).
            "iso_42001": [(42,)],
        })
        client = _build_client(mock_db)
        resp = client.get("/api/v1/admin/compliance/iso-42001/activity")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "export-t"
        assert body["total_activity_rows"] == 42
        by = body["by_proxy_type"]
        assert by == {"llm": 28, "mcp": 11, "skill": 3}, (
            "ISO 42001 export must report the full proxy-type "
            "breakdown; a missing surface means the auditor can't "
            "explain a gap in the total."
        )
