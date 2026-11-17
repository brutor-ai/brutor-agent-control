# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Compliance exports — CSV export shape regression.

The GDPR Article 30 and HIPAA PHI access exports stream CSV so
operators can sample into a spreadsheet. Auditors look at the
header row first — a column rename here would silently break
their templates, so we pin the headers explicitly.

We also cover three correctness invariants:

  * The header row is emitted before any data row (StreamingResponse
    yields the header bytes first).
  * Empty result sets still emit a valid CSV header (auditors
    expect "zero rows last month" to render as a one-line file,
    not a 404 or an empty body).
  * `None`/`null` fields render as empty cells (not the literal
    string "None" from Python `str()`).
"""

from __future__ import annotations

import asyncio
import csv
import io
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.compliance import _stream_csv


def _materialize_stream(response) -> str:
    """Pull the entire body out of a StreamingResponse so we can
    parse it with the stdlib csv module.

    Starlette's StreamingResponse always exposes `body_iterator`
    as an async generator (even when wrapping a sync generator
    via `iterate_in_threadpool`). Drain it via asyncio.
    """
    async def _drain() -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            chunks.append(chunk)
        return "".join(chunks)
    return asyncio.new_event_loop().run_until_complete(_drain())


class TestStreamCsvHelper:
    HEADERS = ["id", "timestamp", "user_id", "purpose"]

    def test_header_first_then_rows(self):
        rows = [
            {"id": "r-1", "timestamp": "2026-06-01T00:00:00Z",
             "user_id": "u-42", "purpose": "LLM inference"},
            {"id": "r-2", "timestamp": "2026-06-02T00:00:00Z",
             "user_id": "u-99", "purpose": "MCP tool"},
        ]
        resp = _stream_csv(self.HEADERS, rows, "out.csv")
        body = _materialize_stream(resp)
        reader = list(csv.reader(io.StringIO(body)))
        # First row is the header — auditors rely on this for their
        # spreadsheet templates.
        assert reader[0] == self.HEADERS
        # Then the data rows in input order.
        assert reader[1] == ["r-1", "2026-06-01T00:00:00Z", "u-42", "LLM inference"]
        assert reader[2] == ["r-2", "2026-06-02T00:00:00Z", "u-99", "MCP tool"]

    def test_empty_result_set_still_emits_header(self):
        # Auditors run an export for "show me PHI access in
        # November 2025"; an empty month must render a single
        # header line, not 0 bytes (which an audit tool would
        # report as a fetch failure).
        resp = _stream_csv(self.HEADERS, [], "out.csv")
        body = _materialize_stream(resp)
        assert body.strip() == ",".join(self.HEADERS)

    def test_none_fields_render_as_empty_cells(self):
        # csv.DictWriter renders None as "" by default; we pin
        # that behaviour explicitly so a refactor doesn't switch
        # to the literal string "None" (which would confuse
        # downstream tools that look for blank-cell semantics).
        rows = [
            {"id": "r-3", "timestamp": "2026-06-03T00:00:00Z",
             "user_id": None, "purpose": None},
        ]
        resp = _stream_csv(self.HEADERS, rows, "out.csv")
        body = _materialize_stream(resp)
        parsed = list(csv.reader(io.StringIO(body)))[1]
        assert parsed == ["r-3", "2026-06-03T00:00:00Z", "", ""], (
            f"None must render as blank cell, got {parsed}"
        )

    def test_extra_fields_in_rows_are_silently_dropped(self):
        # `extrasaction="ignore"` — keeps the CSV header in lockstep
        # with PROXY_LOG_CSV_HEADERS even if upstream Pydantic adds
        # a new field. Drift in the OTHER direction (header
        # mentioning a column the row dict lacks) renders as
        # blank, also acceptable.
        rows = [
            {"id": "r-4", "timestamp": "2026-06-04T00:00:00Z",
             "user_id": "u-5", "purpose": "LLM inference",
             "extra_field_not_in_headers": "should be dropped"},
        ]
        resp = _stream_csv(self.HEADERS, rows, "out.csv")
        body = _materialize_stream(resp)
        # Header still pins the 4 declared columns.
        assert list(csv.reader(io.StringIO(body)))[0] == self.HEADERS
        # `extra_field_not_in_headers` doesn't leak into the body.
        assert "should be dropped" not in body

    def test_content_disposition_carries_filename(self):
        # The download flow uses Content-Disposition to drive
        # save-as. Pin the exact format the browser parses.
        resp = _stream_csv(self.HEADERS, [], "gdpr-art30-tenant-x.csv")
        cd = resp.headers.get("content-disposition")
        assert cd == 'attachment; filename="gdpr-art30-tenant-x.csv"'
        assert resp.media_type == "text/csv"


class TestRowOrderingPreservedFromInput:
    def test_emits_rows_in_input_order(self):
        # The export endpoints emit `ORDER BY timestamp DESC` and
        # rely on _stream_csv preserving input order. Pin that —
        # a future change that batches/sorts inside _stream_csv
        # would break the auditor's chronological scan.
        headers = ["seq"]
        rows = [{"seq": str(i)} for i in range(20)]
        resp = _stream_csv(headers, rows, "ordered.csv")
        body = _materialize_stream(resp)
        parsed = list(csv.reader(io.StringIO(body)))[1:]  # skip header
        assert [r[0] for r in parsed] == [str(i) for i in range(20)]
