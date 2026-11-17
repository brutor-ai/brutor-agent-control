# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Splunk HEC sink for audit-export

Locks down the HEC backend (`send_splunk_hec` in
`app.services.audit_export.backends`) and the dispatcher wiring
inside `AuditExportService`. Two concerns:

  1. **Wire format conversion** — gzipped JSONL → HEC event envelope:
     each non-metadata line becomes `{"event": <obj>, "time": <unix>,
     "sourcetype": "..."}`. The `_meta` line is skipped because
     it's bookkeeping, not an event the operator wants in Splunk.

  2. **Dispatcher routing** — when an `audit_export_destinations`
     row has `destination_type='splunk_hec'`, the service calls
     `_dispatch_splunk_hec`, which decrypts the HEC token and
     hands off to `send_splunk_hec`. End-to-end test below.

We don't test against a live Splunk HEC endpoint; we stub
`httpx.Client.post` and assert the request shape.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.audit_export.backends import send_splunk_hec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gzip_jsonl(*objs: dict) -> bytes:
    """Build a gzipped JSONL payload matching the audit-export
    service's output shape — first line is `{"_meta": {...}}`,
    remaining lines are real events."""
    lines = [json.dumps(o, separators=(",", ":")) for o in objs]
    text = "\n".join(lines) + "\n"
    return gzip.compress(text.encode("utf-8"))


def _make_mock_response(status_code: int = 200, text: str = "ok"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# 1. Wire format conversion
# ---------------------------------------------------------------------------


class TestEventEnvelope:
    def test_each_event_wrapped_in_hec_envelope(self):
        payload = _gzip_jsonl(
            {"_meta": {"row_count": 2}},
            {"id": "log-1", "timestamp": "2026-05-13T09:00:00Z", "proxy_type": "mcp"},
            {"id": "log-2", "timestamp": "2026-05-13T09:00:01Z", "proxy_type": "llm"},
        )
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200, "ok")
            ok, err = send_splunk_hec(
                payload, url="https://splunk.example.com:8088/services/collector/event",
                token="HEC-TOKEN-123",
            )
        assert ok is True, err

        # Inspect the POST body
        call = ctx.post.call_args
        body_bytes: bytes = call.kwargs["content"]
        lines = body_bytes.decode("utf-8").splitlines()
        assert len(lines) == 2  # two events, _meta line skipped
        for line in lines:
            env = json.loads(line)
            assert "event" in env
            assert "time" in env
            assert env["sourcetype"] == "brutor:proxy_log"

    def test_metadata_only_payload_returns_success_with_no_post(self):
        # Edge case: the export window had no rows, so the gzip
        # payload only contains the metadata line. Splunk has
        # nothing to receive — we MUST treat this as success so
        # the scheduler advances the high-water mark.
        payload = _gzip_jsonl({"_meta": {"row_count": 0}})
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ok, err = send_splunk_hec(
                payload, url="https://x.example.com",
                token="HEC-TOKEN",
            )
        assert ok is True
        assert err is None
        # NO HTTP call made at all — saves a round-trip + 200
        # on an empty body on the Splunk side.
        assert ctx.post.call_count == 0

    def test_event_time_pulled_from_row_timestamp(self):
        # Splunk's `_time` field aligns with the actual request
        # time (not the export time) when we forward the
        # ISO-8601 `timestamp` field from the row.
        payload = _gzip_jsonl(
            {"_meta": {}},
            {"id": "log-1", "timestamp": "2026-05-13T09:00:00+00:00"},
        )
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200)
            send_splunk_hec(payload, url="https://x", token="T")

        body = ctx.post.call_args.kwargs["content"].decode("utf-8")
        env = json.loads(body.splitlines()[0])
        # 2026-05-13T09:00:00+00:00 → 1778979600.0 (UTC)
        expected = datetime(
            2026, 5, 13, 9, 0, 0, tzinfo=timezone.utc,
        ).timestamp()
        assert abs(env["time"] - expected) < 0.01

    def test_event_time_falls_back_to_now_when_timestamp_unparseable(self):
        # Defensive against rows without a timestamp or with a
        # corrupt one — we ship the event with "now" rather than
        # dropping it.
        payload = _gzip_jsonl(
            {"_meta": {}},
            {"id": "log-1", "timestamp": "not-a-date"},
        )
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200)
            before = time.time()
            send_splunk_hec(payload, url="https://x", token="T")
            after = time.time()

        body = ctx.post.call_args.kwargs["content"].decode("utf-8")
        env = json.loads(body.splitlines()[0])
        # Falls within (before, after) — i.e. "now-ish".
        assert before <= env["time"] <= after + 1

    def test_optional_index_source_host_round_trip(self):
        payload = _gzip_jsonl(
            {"_meta": {}},
            {"id": "log-1"},
        )
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200)
            send_splunk_hec(
                payload, url="https://x", token="T",
                index="audit_prod", source="brutor-tenant-1",
                host="gw-01", sourcetype="brutor:custom",
            )
        body = ctx.post.call_args.kwargs["content"].decode("utf-8")
        env = json.loads(body.splitlines()[0])
        assert env["index"] == "audit_prod"
        assert env["source"] == "brutor-tenant-1"
        assert env["host"] == "gw-01"
        assert env["sourcetype"] == "brutor:custom"


# ---------------------------------------------------------------------------
# 2. Auth + transport
# ---------------------------------------------------------------------------


class TestAuthAndTransport:
    def test_authorization_header_uses_splunk_scheme(self):
        # Critical: HEC uses `Authorization: Splunk <token>`, NOT
        # `Authorization: Bearer <token>`. If we send Bearer the
        # Splunk side returns 401 with no useful error message.
        payload = _gzip_jsonl({"_meta": {}}, {"id": "x"})
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200)
            send_splunk_hec(payload, url="https://x", token="HEC-ABC")
        headers = ctx.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Splunk HEC-ABC"
        # Definitely NOT bearer.
        assert "Bearer" not in headers["Authorization"]

    def test_content_type_is_application_json(self):
        # HEC ignores `application/x-ndjson` and complains about
        # missing event envelopes. application/json works for
        # newline-delimited HEC events.
        payload = _gzip_jsonl({"_meta": {}}, {"id": "x"})
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(200)
            send_splunk_hec(payload, url="https://x", token="T")
        headers = ctx.post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_http_4xx_returns_failure_with_preview(self):
        # The Splunk HEC error body contains useful diagnostics
        # (e.g. "Token is disabled"). We surface up to 200 chars
        # so operators see the cause in the UI status badge.
        payload = _gzip_jsonl({"_meta": {}}, {"id": "x"})
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.return_value = _make_mock_response(
                403, '{"text":"Token is disabled","code":1}',
            )
            ok, err = send_splunk_hec(
                payload, url="https://x", token="REVOKED",
            )
        assert ok is False
        assert err is not None
        assert "403" in err
        assert "Token is disabled" in err

    def test_request_timeout_returns_clear_message(self):
        import httpx
        payload = _gzip_jsonl({"_meta": {}}, {"id": "x"})
        with patch("httpx.Client") as MockClient:
            ctx = MockClient.return_value.__enter__.return_value
            ctx.post.side_effect = httpx.TimeoutException("timed out")
            ok, err = send_splunk_hec(
                payload, url="https://x", token="T",
                timeout_seconds=5,
            )
        assert ok is False
        assert "timed out" in (err or "").lower()
