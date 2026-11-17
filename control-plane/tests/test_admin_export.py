# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for app.services.admin_export — the streaming CSV/JSONL helpers
shared by the admin proxy-log and audit-log export endpoints.

Coverage:
  - metadata header content + schema_version
  - CSV escaping (RFC 4180-style: commas, quotes, newlines)
  - JSONL one-record-per-line + leading _meta envelope
  - 50,000-row sync cap returns HTTP 413
  - filename helper produces deterministic, range-aware names
  - flatteners pull nested ProxyLog/AuditLog fields into flat row dicts

Run: pytest tests/test_admin_export.py -v
"""

import csv
import io
import json
import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import HTTPException

from app.services.admin_export import (
    AUDIT_LOG_CSV_HEADERS,
    EXPORT_ROW_CAP,
    EXPORT_SCHEMA_VERSION,
    PROXY_LOG_CSV_HEADERS,
    assert_within_cap,
    build_meta,
    export_filename,
    flatten_audit_log,
    flatten_proxy_log,
    media_type_for,
    stream_csv,
    stream_jsonl,
)


# ============================================================================
# build_meta + cap
# ============================================================================


class TestMeta:
    def test_meta_includes_schema_version_and_required_fields(self):
        meta = build_meta(
            resource="proxy_logs",
            format_type="csv",
            exported_by="alice (sysadm-1)",
            filters={"tenant_id": "t-a", "success": True, "noise": None},
            row_count=42,
        )
        assert meta["schema_version"] == EXPORT_SCHEMA_VERSION
        assert meta["resource"] == "proxy_logs"
        assert meta["format"] == "csv"
        assert meta["exported_by"] == "alice (sysadm-1)"
        assert meta["row_count"] == 42
        # `exported_at` is ISO-8601 + parses round-trip
        datetime.fromisoformat(meta["exported_at"])
        # Filter scrubbing drops Nones but keeps explicit booleans
        assert meta["filters"]["success"] is True
        assert "noise" not in meta["filters"]

    def test_meta_serialises_datetime_filters(self):
        ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        meta = build_meta(
            resource="audit_logs",
            format_type="jsonl",
            exported_by="bob",
            filters={"start_time": ts},
            row_count=0,
        )
        assert meta["filters"]["start_time"] == ts.isoformat()


class TestCap:
    def test_cap_passes_under_limit(self):
        assert_within_cap(EXPORT_ROW_CAP, resource="rows")
        assert_within_cap(0, resource="rows")
        assert_within_cap(1, resource="rows")

    def test_cap_raises_413_over_limit(self):
        with pytest.raises(HTTPException) as exc:
            assert_within_cap(EXPORT_ROW_CAP + 1, resource="proxy log rows")
        assert exc.value.status_code == 413
        assert "proxy log rows" in exc.value.detail
        assert str(EXPORT_ROW_CAP) in exc.value.detail


# ============================================================================
# CSV streaming
# ============================================================================


def _consume(generator) -> str:
    return b"".join(list(generator)).decode("utf-8")


def _meta_for(format_type="csv"):
    return build_meta(
        resource="proxy_logs",
        format_type=format_type,
        exported_by="t",
        filters={"tenant_id": "t-a"},
        row_count=0,
    )


class TestCsvStream:
    def test_metadata_header_emitted_as_comments(self):
        meta = _meta_for("csv")
        meta["row_count"] = 1
        out = _consume(stream_csv(
            records=[{"id": "1", "x": "y"}],
            headers=["id", "x"],
            meta=meta,
        ))
        first_lines = out.splitlines()
        # First line must be the schema_version sentinel comment
        assert first_lines[0].startswith("# brutor-export schema_version=")
        # Other meta keys also appear as comments
        comment_lines = [ln for ln in first_lines if ln.startswith("#")]
        assert any("exported_by" in c for c in comment_lines)
        assert any("row_count" in c for c in comment_lines)
        # Header row follows the comments
        non_comment = [ln for ln in first_lines if not ln.startswith("#") and ln]
        assert non_comment[0] == "id,x"

    def test_csv_quotes_special_characters(self):
        meta = _meta_for("csv")
        out = _consume(stream_csv(
            records=[
                {"a": "no special", "b": "has,comma"},
                {"a": 'with "quotes"', "b": "line1\nline2"},
            ],
            headers=["a", "b"],
            meta=meta,
        ))
        # Strip comment header to round-trip parse the data
        data_block = "\n".join(
            ln for ln in out.splitlines() if not ln.startswith("#")
        )
        rows = list(csv.DictReader(io.StringIO(data_block)))
        assert rows[0]["b"] == "has,comma"
        assert rows[1]["a"] == 'with "quotes"'
        assert rows[1]["b"] == "line1\nline2"

    def test_csv_renders_bool_and_dict(self):
        meta = _meta_for("csv")
        out = _consume(stream_csv(
            records=[{"flag": True, "obj": {"k": 1}}],
            headers=["flag", "obj"],
            meta=meta,
        ))
        data = [ln for ln in out.splitlines() if not ln.startswith("#")]
        assert data[1].startswith("true,")
        # The dict round-trips as compact JSON in the cell
        assert '"{""k"":1}"' in data[1] or "{\"k\":1}" in data[1]

    def test_csv_row_extractor_flattens(self):
        meta = _meta_for("csv")
        out = _consume(stream_csv(
            records=[{"a": {"b": 5}}],
            headers=["b"],
            meta=meta,
            row_extractor=lambda rec: {"b": rec["a"]["b"]},
        ))
        data = [ln for ln in out.splitlines() if not ln.startswith("#")]
        assert data[1] == "5"


# ============================================================================
# JSONL streaming
# ============================================================================


class TestJsonlStream:
    def test_first_line_is_meta_envelope(self):
        meta = _meta_for("jsonl")
        meta["row_count"] = 2
        out = _consume(stream_jsonl(
            records=[{"id": "1"}, {"id": "2"}],
            meta=meta,
        ))
        lines = out.splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert "_meta" in first
        assert first["_meta"]["schema_version"] == EXPORT_SCHEMA_VERSION
        assert first["_meta"]["row_count"] == 2

    def test_one_record_per_line_each_valid_json(self):
        meta = _meta_for("jsonl")
        records = [{"id": str(i), "v": i} for i in range(5)]
        out = _consume(stream_jsonl(records=records, meta=meta))
        lines = out.splitlines()
        # 1 meta + 5 records
        assert len(lines) == 6
        for line in lines[1:]:
            decoded = json.loads(line)
            assert "id" in decoded
            assert "v" in decoded

    def test_jsonl_round_trip_preserves_complex_values(self):
        meta = _meta_for("jsonl")
        rec = {"nested": {"a": [1, 2]}, "ts": "2026-04-01T00:00:00+00:00"}
        out = _consume(stream_jsonl(records=[rec], meta=meta))
        last = json.loads(out.splitlines()[-1])
        assert last["nested"] == {"a": [1, 2]}


# ============================================================================
# Filename + media type
# ============================================================================


class TestFilename:
    def test_filename_with_range(self):
        s = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        e = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
        name = export_filename("proxy-logs", "csv", start_time=s, end_time=e)
        assert name == "proxy-logs-20260401T000000Z-to-20260426T000000Z.csv"

    def test_filename_jsonl_extension(self):
        name = export_filename("audit-logs", "jsonl")
        assert name.endswith(".jsonl")
        assert name.startswith("audit-logs-")

    def test_media_type_mapping(self):
        assert media_type_for("csv") == "text/csv"
        assert media_type_for("jsonl") == "application/x-ndjson"


# ============================================================================
# Flatteners
# ============================================================================


class TestFlattenProxyLog:
    def test_pulls_nested_request_response_llm_fields(self):
        log = {
            "id": "log-1",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "proxy_type": "llm",
            "request": {
                "method": "POST",
                "endpoint": "/v1/chat/completions",
                "target_url": "https://api.openai.com",
                "size_bytes": 123,
            },
            "response": {
                "status_code": 200,
                "size_bytes": 456,
                "duration_ms": 78.9,
            },
            "llm": {
                "provider": "openai",
                "model_used": "gpt-4o",
                "total_tokens": 1500,
                "estimated_cost_usd": 0.012,
            },
            "success": True,
        }
        flat = flatten_proxy_log(log, tenant_id="t-a")
        assert flat["id"] == "log-1"
        assert flat["tenant_id"] == "t-a"
        assert flat["method"] == "POST"
        assert flat["status_code"] == 200
        assert flat["duration_ms"] == 78.9
        assert flat["llm_provider"] == "openai"
        assert flat["llm_model_used"] == "gpt-4o"
        assert flat["llm_total_tokens"] == 1500
        # All declared CSV headers are addressable on the flat row
        for h in PROXY_LOG_CSV_HEADERS:
            assert h in flat

    def test_pulls_request_and_response_bodies(self):
        """Body previews on the nested request/response sub-objects
        must flow through to the flat row's `request_body` /
        `response_body` columns. Anyone exporting CSV for an audit
        relies on this — the previous schema dropped bodies entirely."""
        log = {
            "id": "log-2",
            "timestamp": "2026-04-28T00:00:00+00:00",
            "proxy_type": "a2a",
            "request": {
                "method": "POST",
                "endpoint": "/v1/proxy/a2a/outbound",
                "size_bytes": 200,
                "body_preview": '{"capability":"hello","message":{"role":"user"}}',
            },
            "response": {
                "status_code": 200,
                "size_bytes": 100,
                "body_preview": '{"jsonrpc":"2.0","result":{"status":"completed"}}',
            },
            "success": True,
        }
        flat = flatten_proxy_log(log, tenant_id="t-a")
        assert flat["request_body"] == '{"capability":"hello","message":{"role":"user"}}'
        assert flat["response_body"] == '{"jsonrpc":"2.0","result":{"status":"completed"}}'
        assert "request_body" in PROXY_LOG_CSV_HEADERS
        assert "response_body" in PROXY_LOG_CSV_HEADERS

    def test_includes_residency_audit_columns(self):
        """Operators producing residency reports
        (e.g. monthly "rows blocked because of data-residency policy")
        depend on `region_routed_to` + `residency_class` landing in the
        CSV export. Pin both columns explicitly so a future PR that
        renames either at the model layer can't silently drop them
        from the report without a test failure."""
        log = {
            "id": "log-residency",
            "timestamp": "2026-06-03T12:00:00+00:00",
            "proxy_type": "llm",
            "region_routed_to": "eu-west-1",
            "residency_class": "allowed",
            "success": True,
        }
        flat = flatten_proxy_log(log, tenant_id="t-eu")
        assert flat["region_routed_to"] == "eu-west-1"
        assert flat["residency_class"] == "allowed"
        assert "region_routed_to" in PROXY_LOG_CSV_HEADERS
        assert "residency_class" in PROXY_LOG_CSV_HEADERS

    def test_residency_columns_pass_through_null(self):
        """Pre-a069 audit rows have NULL on both residency columns.
        Pin that NULL → None survives flattening so CSV doesn't
        explode on legacy data during a multi-month export."""
        log = {
            "id": "log-legacy",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "proxy_type": "llm",
            "success": True,
            # No residency keys — pre-a069 shape.
        }
        flat = flatten_proxy_log(log, tenant_id="t-legacy")
        assert flat["region_routed_to"] is None
        assert flat["residency_class"] is None


class TestFlattenAuditLog:
    def test_returns_full_csv_schema(self):
        log = {
            "id": "a-1",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "admin_id": "sysadm",
            "admin_type": "system",
            "tenant_id": None,
            "affected_tenant_id": "t-a",
            "action": "create_tenant",
            "resource_type": "tenant",
            "resource_id": "t-a",
            "success": True,
            "ip_address": "10.0.0.1",
            "user_agent": "curl/8.0",
            "request_method": "POST",
            "request_path": "/v1/admin/tenants",
            "details": {"name": "Acme"},
        }
        flat = flatten_audit_log(log)
        for h in AUDIT_LOG_CSV_HEADERS:
            assert h in flat
        assert flat["details"] == {"name": "Acme"}

    def test_missing_optional_fields_default_to_none(self):
        flat = flatten_audit_log({
            "id": "a-2",
            "timestamp": "2026-04-01T00:00:00+00:00",
            "action": "x",
            "success": False,
        })
        assert flat["details"] == {}
        assert flat["admin_id"] is None
        assert flat["affected_tenant_id"] is None
