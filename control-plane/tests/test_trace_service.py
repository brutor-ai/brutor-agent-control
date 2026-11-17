# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
TraceService unit tests.

Tests the read-side aggregation logic with hand-built ProxyLog
objects (no DB needed). The service's SQL paths are exercised
indirectly via the existing live-DB integration tests; here we
focus on the in-Python summary + sort + ordering invariants
that the trace-list and trace-detail UIs depend on.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.services.trace_service import (
    KNOWN_CONTINUITY,
    KNOWN_ROOT_KINDS,
    TraceService,
)


def _row(**kwargs) -> SimpleNamespace:
    """Build a minimal proxy-log-like object that the service
    code can read. The service only touches a handful of fields,
    so SimpleNamespace is enough — we don't need the full
    SQLAlchemy mapper machinery for these in-Python tests."""
    defaults = dict(
        id="row-1",
        tenant_id="t-1",
        timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        proxy_type="llm",
        proxy_subtype=None,
        request_id=None,
        parent_task_id=None,
        current_task_id=None,
        delegation_depth=0,
        trace_continuity=None,
        trace_root_kind=None,
        endpoint=None,
        http_method=None,
        http_status_code=None,
        duration_ms=None,
        success=True,
        error_message=None,
        gateway_user_id=None,
        idp_user_email=None,
        server_id=None,
        mcp_capability_name=None,
        llm_model_used=None,
        llm_provider=None,
        root_task_id="root-A",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Known-set integrity
# ---------------------------------------------------------------------------


class TestKnownSets:
    def test_root_kinds_match_rust_enum(self):
        # The Rust runtime stamps one of these five values on every
        # root span; the API validates filters against the same set.
        assert KNOWN_ROOT_KINDS == {
            "llm_user",
            "agent_invocation",
            "skill_execution",
            "scheduled_job",
            "external_api",
        }

    def test_continuity_set_pinned(self):
        """Every `trace_continuity` the runtime writes.

        XREPO-13 — this test pinned four values and passed for months while
        the runtime grew to six. That is the failure mode of pinning ONE side
        of a two-sided contract: `TraceContinuity` gained `client_asserted`
        (a2a_delegation.rs) and the OTel inlet added `external`
        (otel_ingest.rs), and neither had to touch this file to do it. The
        test stayed green, the constant stayed wrong, and the comment claiming
        "exactly these four states" made it look deliberate.

        The other half now lives in the core:
        `a2a_delegation::tests::trace_continuity_values_are_pinned`. Adding a
        variant there fails that test; adding it there and here without
        telling the other side is what this pair exists to prevent.
        """
        assert KNOWN_CONTINUITY == {
            "minted",
            "verified",
            "client_dropped",
            "sig_failed",
            "client_asserted",
            "external",
        }


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------


class TestSummariseHelper:
    def test_root_kind_taken_from_root_span(self):
        # The root span (depth 0) carries trace_root_kind. The
        # summary should pull that even if later spans have a
        # different non-null value (which would be a bug — but the
        # summary should still prefer the root).
        spans = [
            _row(delegation_depth=0, trace_root_kind="llm_user",
                 timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)),
            _row(delegation_depth=1, trace_root_kind=None,
                 timestamp=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["root_kind"] == "llm_user"
        assert out["span_count"] == 2
        assert out["max_depth"] == 1

    def test_started_and_ended_at_span_window(self):
        # started_at = min timestamp; ended_at = max. A trace where
        # the root completes first (rare but possible: cached
        # LLM returns instantly while MCP tool runs) must still
        # surface the full window.
        spans = [
            _row(timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)),
            _row(timestamp=datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc)),
            _row(timestamp=datetime(2026, 6, 1, 12, 3, tzinfo=timezone.utc)),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["started_at"] == "2026-06-01T12:00:00+00:00"
        assert out["ended_at"] == "2026-06-01T12:05:00+00:00"

    def test_overall_success_false_when_any_span_failed(self):
        # Mixed outcomes — at least one failure flips the
        # whole-trace summary to failed. The UI uses this for the
        # red/green badge on each list row.
        spans = [
            _row(success=True),
            _row(success=True),
            _row(success=False, error_message="upstream 502"),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["overall_success"] is False

    def test_overall_success_true_when_all_spans_succeeded(self):
        spans = [_row(success=True), _row(success=True)]
        out = TraceService._summarise("root-A", spans)
        assert out["overall_success"] is True

    def test_worst_continuity_prefers_sig_failed(self):
        # If any span carries `sig_failed`, the summary surfaces
        # it — that's a forensically-defensible flag the auditor
        # wants visible on the list row, not buried inside the
        # timeline.
        spans = [
            _row(trace_continuity="verified", delegation_depth=0),
            _row(trace_continuity="sig_failed", delegation_depth=1),
            _row(trace_continuity="client_dropped", delegation_depth=2),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["worst_continuity"] == "sig_failed"

    def test_worst_continuity_falls_back_to_client_dropped(self):
        # Without sig_failed, client_dropped wins. The list row
        # then shows a "fragmented trace" warning, distinct from
        # both fully-verified and signature-failed.
        spans = [
            _row(trace_continuity="verified"),
            _row(trace_continuity="minted"),
            _row(trace_continuity="client_dropped"),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["worst_continuity"] == "client_dropped"

    def test_worst_continuity_verified_when_all_clean(self):
        spans = [
            _row(trace_continuity="verified"),
            _row(trace_continuity="verified"),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["worst_continuity"] == "verified"

    def test_proxy_types_are_distinct_and_sorted(self):
        # Cross-proxy trace example: LLM emitted tool_calls, MCP
        # ran the tool, LLM continued. proxy_types must list both
        # so the UI can render protocol badges.
        spans = [
            _row(proxy_type="llm"),
            _row(proxy_type="mcp"),
            _row(proxy_type="llm"),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["proxy_types"] == ["llm", "mcp"]

    def test_gateway_user_id_pulled_from_first_non_null(self):
        # Some spans (e.g. MCP tool execution by an internal
        # service identity) may lack gateway_user_id. The summary
        # surfaces the first non-null one — typically the root.
        spans = [
            _row(delegation_depth=0, gateway_user_id="u-42",
                 timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)),
            _row(delegation_depth=1, gateway_user_id=None,
                 timestamp=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc)),
        ]
        out = TraceService._summarise("root-A", spans)
        assert out["gateway_user_id"] == "u-42"


# ---------------------------------------------------------------------------
# Span row → dict (UI-facing detail rendering)
# ---------------------------------------------------------------------------


class TestSpanRowToDict:
    def test_handles_null_timestamp_gracefully(self):
        # Defensive — defensive code paths in the writer can produce
        # a NULL timestamp in test fixtures. The serialiser should
        # emit None rather than raise.
        row = _row(timestamp=None)
        out = TraceService._span_row_to_dict(row)
        assert out["timestamp"] is None

    def test_carries_protocol_specific_fields(self):
        # The timeline detail panel uses proxy_type-specific fields
        # to render the right card layout. Verify they pass through.
        row = _row(
            proxy_type="mcp",
            mcp_capability_name="search_web",
            llm_model_used=None,
        )
        out = TraceService._span_row_to_dict(row)
        assert out["mcp_capability_name"] == "search_web"
        assert out["llm_model_used"] is None

    def test_carries_continuity_and_root_kind(self):
        # The audit dashboard's "HMAC-verification status per step"
        # icon needs both fields. Pin them.
        row = _row(
            trace_continuity="sig_failed",
            trace_root_kind="llm_user",
            delegation_depth=2,
        )
        out = TraceService._span_row_to_dict(row)
        assert out["trace_continuity"] == "sig_failed"
        assert out["trace_root_kind"] == "llm_user"
        assert out["delegation_depth"] == 2


# ---------------------------------------------------------------------------
# Pagination invariants
# ---------------------------------------------------------------------------


class TestListSorting:
    def test_summaries_sort_newest_first(self):
        # The list endpoint is timeline-style — newest at top.
        # The service does an in-Python sort because cross-span
        # aggregates make a SQL ORDER BY tricky to express.
        # Verify the order is correct by feeding the helper a
        # batch of mixed-window spans grouped by root.
        svc = TraceService(logger=logging.getLogger("t"))
        # Two traces with overlapping windows
        traces = [
            svc._summarise("root-A", [
                _row(timestamp=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)),
            ]),
            svc._summarise("root-B", [
                _row(timestamp=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)),
            ]),
            svc._summarise("root-C", [
                _row(timestamp=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)),
            ]),
        ]
        # Mimic the sort the service applies before pagination
        traces.sort(key=lambda s: s["started_at"], reverse=True)
        assert [t["root_task_id"] for t in traces] == ["root-C", "root-A", "root-B"]
