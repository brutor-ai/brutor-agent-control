# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit-export service — single-shot run of one configured destination.

# Flow

  1. Compute the window: rows the gateway wrote after the position
     (`created_at > last_exported_at`), up to `COMMIT_LAG_SECONDS`
     before now.
  2. Fetch rows in batches (up to `MAX_ROWS_PER_RUN`).
  3. Flatten via `flatten_proxy_log` (same projection used by the
     interactive admin export — single source of truth for "what a
     proxy log looks like as a flat record").
  4. Apply the destination's field allowlist if set.
  5. Encode as JSONL, gzip, hand to the backend.
  6. On success, advance `last_exported_at` to the end of what was
     exported and mark the destination `success`: the window end when
     the run took every row of the window, the write time of the last
     row exported when the window held more than one run takes. On
     failure, leave the position alone — the next run retries the same
     window (at-least-once delivery; SIEM dedupes by `proxy_logs.id`).

# The position

A row's `timestamp` is when the call started; the gateway writes the row
afterwards (at once, after a long stream, after an approval that took
hours, after a database outage). A position kept in `timestamp` passes
over every row written after a run for a call that started before it.
The position is therefore kept in `created_at`, which the database sets
when the row is inserted (the gateway does not send it).

`created_at` is the start of the inserting transaction, so a row becomes
visible a moment after the time it carries. A run takes no row younger
than `COMMIT_LAG_SECONDS`: by then the transaction that wrote a row of
that moment has ended. An insert that stayed open longer than that would
still be passed over; the gateway's batch inserts take milliseconds.

# At-least-once semantics

Deliberate. The alternative — advance the cutoff BEFORE the upload
to S3 succeeds — gives at-most-once and silently drops audit rows on
the first network blip. For compliance, "the operator gets the row
twice" is acceptable; "the operator never gets the row" isn't.

# Row cap

`MAX_ROWS_PER_RUN` (default 50k) bounds memory and S3 upload size.
If a backlog builds up — destination disabled for a week, then
re-enabled — the catch-up walks the backlog in 50k-row chunks across
consecutive runs. The position advances after each chunk, to
the last row of the chunk and no further, so no row is skipped. Rows
written at the same instant as a chunk's last row (one batch of the
gateway) go with that chunk, which is the only way a run exceeds
the cap.

# Hot-path concerns

This service runs in the background scheduler, NOT in the request
path. The proxy_logs SELECT can be heavy (timestamp index, but the
result set is large) — but it never blocks a user request. We use
the gateway's existing SessionLocal so the same connection pool is
shared with the API; the work happens on the same DB but doesn't
contend with hot writes (Postgres MVCC).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.database.models.audit_export_destination import (
    AuditExportDestination,
    DEST_TYPE_S3,
    DEST_TYPE_WEBHOOK,
    DEST_TYPE_SPLUNK_HEC,
    STATUS_FAILURE,
    STATUS_NO_DATA,
    STATUS_RUNNING,
    STATUS_SUCCESS,
)
from app.database.models.proxy_log import ProxyLog
from app.services.admin_export import (
    PROXY_LOG_CSV_HEADERS,
    flatten_proxy_log,
)
from app.services.audit_export import backends
from app.services.audit_chain_canonical import CHAIN_COLUMNS, chain_evidence


log = logging.getLogger("brutor-audit-export")


# Upper bound on rows shipped in a single run. Caps memory and S3
# upload size; backlogs walk through in chunks across consecutive
# runs (the high-water mark advances after each chunk).
MAX_ROWS_PER_RUN = 50_000

# A run takes no row written in the last this many seconds: a transaction
# that began then may not have committed, and its rows carry the time it
# began.
COMMIT_LAG_SECONDS = 120

# Hard ceiling on `last_run_error` text. Operators want a one-line
# summary, not a stack trace.
MAX_ERROR_LENGTH = 500


class RunOutcome(str, Enum):
    SUCCESS = STATUS_SUCCESS
    NO_DATA = STATUS_NO_DATA
    FAILURE = STATUS_FAILURE


@dataclass
class RunResult:
    """Structured result of one export run. Returned by `run_destination`
    so callers (scheduler, manual-trigger endpoint, tests) can act on
    the outcome without re-querying the DB row."""

    destination_id: str
    outcome: RunOutcome
    rows_exported: int
    window_start: Optional[datetime]
    window_end: datetime
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AuditExportService:
    """Single-shot runner for one destination at a time.

    Stateless. Pass a DB session + an EncryptionService at construction
    time; call `run_destination(dest_id)` per export.
    """

    def __init__(self, db: Session, encryption_service) -> None:
        self.db = db
        self.encryption_service = encryption_service

    # ------------------------------------------------------------------
    # Top-level API
    # ------------------------------------------------------------------

    def run_destination(self, destination_id: str) -> RunResult:
        """Execute one full run for the destination identified by
        `destination_id`. Updates the DB row's high-water mark and
        last-run fields. Returns a `RunResult` summarising outcome.

        Does NOT raise on backend failure — failures land in the
        result's `error` field and on the DB row. The only exceptions
        that escape are programmer errors (destination not found,
        unsupported type) — those mean something's wrong with the
        caller and should be visible.
        """
        dest = (
            self.db.query(AuditExportDestination)
            .filter(AuditExportDestination.id == destination_id)
            .one_or_none()
        )
        if dest is None:
            raise ValueError(f"Audit export destination {destination_id!r} not found")

        started = datetime.now(timezone.utc)
        window_end = started - timedelta(seconds=COMMIT_LAG_SECONDS)
        window_start = dest.last_exported_at  # may be None on first run

        # Mark running so a concurrent scheduler tick + manual trigger
        # both see "in progress" via the API. Best-effort — if the
        # process dies mid-run, the next tick will overwrite to
        # success/failure when it completes. Not a distributed lock;
        # see "Run-skew note" at the bottom of this docstring.
        dest.last_run_at = started
        dest.last_run_status = STATUS_RUNNING
        dest.last_run_error = None
        self.db.flush()

        if dest.field_allowlist and not self._effective_allowlist(dest.field_allowlist):
            return self._record_failure(
                dest, window_start, window_end,
                "the column allow-list names no existing column: nothing is exported "
                f"until it is corrected (configured: {dest.field_allowlist})",
            )

        try:
            rows, complete = self._fetch_rows(dest.tenant_id, window_start, window_end)
        except Exception as e:
            log.exception("Failed to fetch proxy_logs for export %s", dest.id)
            return self._record_failure(
                dest, window_start, window_end, f"fetch failed: {e}"
            )

        if not rows:
            return self._record_no_data(dest, window_start, window_end)

        # The window held more rows than one run exports: what this run
        # covers ends at its last row, and the next run starts after it.
        if not complete:
            window_end = rows[-1].created_at

        try:
            payload = self._build_payload(dest, rows, window_start, window_end)
        except Exception as e:
            log.exception("Failed to build payload for export %s", dest.id)
            return self._record_failure(
                dest, window_start, window_end, f"payload build failed: {e}"
            )

        ok, error = self._dispatch(
            dest, payload, window_end, len(rows), window_start
        )
        if not ok:
            return self._record_failure(dest, window_start, window_end, error or "unknown error")

        return self._record_success(dest, window_start, window_end, len(rows))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_rows(
        self,
        tenant_id: str,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Tuple[List[ProxyLog], bool]:
        """The rows of the window for this tenant, in the order they were
        written, and whether they are all of them. The window is of
        `created_at`: when the gateway wrote the row.

        At most `MAX_ROWS_PER_RUN`, plus the rows written at the same
        instant as the last one: the position a run leaves behind is a
        time, so a run never ends between two rows of the same instant.
        """

        def window():
            q = self.db.query(ProxyLog).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.created_at <= window_end,
            )
            if window_start is not None:
                q = q.filter(ProxyLog.created_at > window_start)
            return q

        rows = (
            window()
            .order_by(ProxyLog.created_at.asc(), ProxyLog.id.asc())
            .limit(MAX_ROWS_PER_RUN + 1)
            .all()
        )
        if len(rows) <= MAX_ROWS_PER_RUN:
            return rows, True
        rows = rows[:MAX_ROWS_PER_RUN]
        last = rows[-1]
        rows.extend(
            window()
            .filter(ProxyLog.created_at == last.created_at, ProxyLog.id > last.id)
            .order_by(ProxyLog.id.asc())
            .all()
        )
        return rows, False

    def _build_payload(
        self,
        dest: AuditExportDestination,
        rows: List[ProxyLog],
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> bytes:
        """Flatten, project through the allowlist, JSONL-encode, gzip."""
        allowlist = self._effective_allowlist(dest.field_allowlist)

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            # Single metadata line at the head — same shape the
            # interactive JSONL export uses, so a SIEM ingest pipeline
            # can use one parser for both delivery channels.
            meta = {
                "_meta": {
                    "schema_version": "1.0",
                    "resource": "proxy_logs",
                    "exported_by": f"audit-export:{dest.id}",
                    "exported_at": window_end.isoformat(),
                    "window_start": (
                        window_start.isoformat() if window_start else None
                    ),
                    "window_end": window_end.isoformat(),
                    "tenant_id": dest.tenant_id,
                    "destination_id": dest.id,
                    "row_count": len(rows),
                    "field_allowlist": dest.field_allowlist,
                }
            }
            gz.write((json.dumps(meta, default=str) + "\n").encode("utf-8"))

            for row in rows:
                flat = flatten_proxy_log(row.to_dict(), tenant_id=row.tenant_id)
                # The chain block: the chain columns plus every field
                # the row's sealed format covers, under their column names,
                # so a recipient can recompute `row_hash` with the published
                # reference verifier. The flattened CSV view renames and
                # omits covered fields, and never carried the chain columns.
                flat[self.CHAIN_BLOCK] = chain_evidence(row)
                if allowlist is not None:
                    flat = {k: v for k, v in flat.items() if k in allowlist}
                gz.write((json.dumps(flat, default=str) + "\n").encode("utf-8"))

        return buf.getvalue()

    @staticmethod
    def _effective_allowlist(configured: Optional[List[str]]) -> Optional[set]:
        """The columns to export: None for every column (no allow-list),
        otherwise the configured names that exist. An allow-list is
        validated when it is saved; one that names no existing column
        (a stored row from before that, a column since removed) gives an
        empty set, and the run exports nothing."""
        if not configured:
            return None
        return {
            h for h in configured
            if h in PROXY_LOG_CSV_HEADERS or h == AuditExportService.CHAIN_BLOCK
        }

    # -- Export manifest --------------------------------------------
    #
    # An export leaves the database. Once it does, the hash chain in
    # `proxy_logs` says nothing about the copy sitting in the customer's
    # bucket — a truncated upload, a silently corrupted object or an edited
    # file all look identical to a recipient.
    #
    # The manifest is a sidecar object stating what was sent and its digest,
    # signed by the same audit key that signs chain checkpoints. A recipient
    # hashes the file they received and compares; nothing about our
    # serialisation has to be reproduced for that to work.

    #: Chain columns. If a field allowlist strips these, the exported rows
    #: cannot be independently verified — the manifest says so rather than
    #: leaving a recipient to work it out.
    CHAIN_COLUMNS = frozenset(CHAIN_COLUMNS)

    #: The per-row key carrying the chain columns and the covered
    #: fields (`audit_chain_canonical.chain_evidence`). Earlier the
    #: manifest said `chain_columns_included: true` for an unfiltered export
    #: whose rows carried no chain column at all: the flattened view never
    #: had them. The flag now follows what the rows actually hold.
    CHAIN_BLOCK = "chain"

    MANIFEST_TIMEOUT_SECONDS = 5.0

    def _build_manifest(
        self,
        dest: AuditExportDestination,
        payload: bytes,
        object_key: str,
        row_count: int,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> dict:
        """Describe the delivered artifact. Digest is over the gzipped bytes
        as sent, so a recipient can verify with `sha256sum` alone."""
        allowlist = self._effective_allowlist(dest.field_allowlist)
        chain_included = allowlist is None or self.CHAIN_BLOCK in allowlist

        return {
            "object_key": object_key,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "row_count": row_count,
            "tenant_id": dest.tenant_id,
            "destination_id": dest.id,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat(),
            "chain_columns_included": chain_included,
        }

    def _sign_manifest(self, manifest: dict) -> dict:
        """Ask the proxy to sign the manifest, and return it either way.

        Delegates to the shared signer (assurance certificates have
        the same need); behaviour is unchanged — best-effort, with
        `signed: false` recorded rather than implied.
        """
        from app.services.proxy_signing import sign_manifest

        return sign_manifest(manifest, timeout=self.MANIFEST_TIMEOUT_SECONDS)

    def _dispatch(
        self,
        dest: AuditExportDestination,
        payload: bytes,
        window_end: datetime,
        row_count: int,
        window_start: Optional[datetime],
    ) -> Tuple[bool, Optional[str]]:
        """Hand the payload to the right backend. Decrypts secrets
        from `dest.config` here so the backend layer never has to
        know about EncryptionService."""
        cfg = dict(dest.config or {})

        if dest.destination_type == DEST_TYPE_S3:
            return self._dispatch_s3(
                dest, cfg, payload, window_end, row_count, window_start
            )
        if dest.destination_type == DEST_TYPE_WEBHOOK:
            return self._dispatch_webhook(dest, cfg, payload)
        if dest.destination_type == DEST_TYPE_SPLUNK_HEC:
            return self._dispatch_splunk_hec(dest, cfg, payload)
        return False, f"unsupported destination_type: {dest.destination_type!r}"

    def _dispatch_s3(
        self,
        dest: AuditExportDestination,
        cfg: dict,
        payload: bytes,
        window_end: datetime,
        row_count: int,
        window_start: Optional[datetime],
    ) -> Tuple[bool, Optional[str]]:
        bucket = cfg.get("bucket")
        if not bucket:
            return False, "S3 config missing 'bucket'"
        access_key_id = cfg.get("access_key_id")
        secret_enc = cfg.get("secret_access_key_encrypted")
        if not access_key_id or not secret_enc:
            return False, "S3 config missing access_key_id / secret_access_key_encrypted"
        try:
            secret = self.encryption_service.decrypt(secret_enc)
        except Exception as e:
            return False, f"S3 secret decrypt failed: {e}"

        prefix = (cfg.get("prefix") or "").strip("/")
        object_key = self._build_object_key(prefix, dest.tenant_id, window_end)

        s3_kwargs = dict(
            bucket=bucket,
            region=cfg.get("region"),
            access_key_id=access_key_id,
            secret_access_key=secret,
            endpoint_url=cfg.get("endpoint_url"),
            sse=cfg.get("sse"),
            sse_kms_key_id=cfg.get("sse_kms_key_id"),
        )

        ok, err = backends.SEND_S3(payload, object_key, **s3_kwargs)
        if not ok:
            return ok, err

        # The manifest goes up only after the artifact it describes
        # landed. A manifest for an object that failed to upload would be an
        # attestation to something that is not there.
        #
        # Its own failure does not fail the export: the rows were delivered,
        # and the run row already records that. A missing manifest is a
        # weaker evidence position, not a lost export.
        try:
            manifest = self._sign_manifest(
                self._build_manifest(
                    dest,
                    payload,
                    object_key,
                    row_count,
                    window_start,
                    window_end,
                )
            )
            backends.SEND_S3(
                json.dumps(manifest, indent=2).encode("utf-8"),
                f"{object_key}.manifest.json",
                **s3_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - never fail a delivered export
            log.warning(
                "Audit export manifest upload failed for %s: %s", object_key, exc
            )

        return ok, err

    def _dispatch_webhook(
        self,
        dest: AuditExportDestination,
        cfg: dict,
        payload: bytes,
    ) -> Tuple[bool, Optional[str]]:
        url = cfg.get("url")
        if not url:
            return False, "webhook config missing 'url'"

        bearer = None
        bearer_enc = cfg.get("bearer_token_encrypted")
        if bearer_enc:
            try:
                bearer = self.encryption_service.decrypt(bearer_enc)
            except Exception as e:
                return False, f"webhook bearer decrypt failed: {e}"

        # The headers are stored encrypted, as one object.
        headers = {}
        headers_enc = cfg.get("headers_encrypted")
        if headers_enc:
            try:
                headers = json.loads(self.encryption_service.decrypt(headers_enc))
            except Exception as e:
                return False, f"webhook headers decrypt failed: {e}"
            if not isinstance(headers, dict):
                return False, "webhook headers are not an object"

        return backends.SEND_WEBHOOK(
            payload,
            url=url,
            headers=headers,
            bearer_token=bearer,
        )

    def _dispatch_splunk_hec(
        self,
        dest: AuditExportDestination,
        cfg: dict,
        payload: bytes,
    ) -> Tuple[bool, Optional[str]]:
        """Splunk HEC dispatcher.

        Config shape (after secret decryption):
          {
            "url":         "https://splunk.acme.com:8088/services/collector/event",
            "token_encrypted": "<encrypted HEC token>",
            "index":       "<optional Splunk index name>",
            "sourcetype":  "<optional; default brutor:proxy_log>",
            "source":      "<optional event source label>",
            "host":        "<optional event host>"
          }
        """
        url = cfg.get("url")
        if not url:
            return False, "splunk_hec config missing 'url'"

        token = None
        token_enc = cfg.get("token_encrypted")
        if token_enc:
            try:
                token = self.encryption_service.decrypt(token_enc)
            except Exception as e:
                return False, f"splunk_hec token decrypt failed: {e}"
        if not token:
            return False, "splunk_hec config missing 'token' / 'token_encrypted'"

        return backends.SEND_SPLUNK_HEC(
            payload,
            url=url,
            token=token,
            index=cfg.get("index"),
            source=cfg.get("source"),
            sourcetype=cfg.get("sourcetype") or "brutor:proxy_log",
            host=cfg.get("host"),
        )

    @staticmethod
    def _build_object_key(
        prefix: str, tenant_id: str, window_end: datetime
    ) -> str:
        """Construct the S3 object key.

        Format: `<prefix>/<tenant>/<YYYY>/<MM>/<DD>/<HHMMSS>.jsonl.gz`

        Date partitions make S3 lifecycle policies (Glacier transitions,
        retention windows) work naturally on a per-day basis. The
        tenant subdirectory means a single bucket can hold many
        tenants' logs without IAM gymnastics on the operator side."""
        date_path = window_end.strftime("%Y/%m/%d")
        time_part = window_end.strftime("%H%M%S")
        tail = f"{tenant_id}/{date_path}/{time_part}.jsonl.gz"
        return f"{prefix}/{tail}" if prefix else tail

    # ------------------------------------------------------------------
    # Result recording
    # ------------------------------------------------------------------

    def _record_success(
        self,
        dest: AuditExportDestination,
        window_start: Optional[datetime],
        window_end: datetime,
        rows: int,
    ) -> RunResult:
        dest.last_exported_at = window_end
        dest.last_run_status = STATUS_SUCCESS
        dest.last_run_rows = rows
        dest.last_run_error = None
        self.db.flush()
        log.info(
            "Audit export %s succeeded: tenant=%s rows=%d window=(%s, %s]",
            dest.id, dest.tenant_id, rows, window_start, window_end,
        )
        return RunResult(
            destination_id=dest.id,
            outcome=RunOutcome.SUCCESS,
            rows_exported=rows,
            window_start=window_start,
            window_end=window_end,
        )

    def _record_no_data(
        self,
        dest: AuditExportDestination,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> RunResult:
        # No rows — advance the cutoff anyway so we don't keep
        # re-scanning the same empty window forever. Status is
        # 'no_data' (not 'success') so the admin UI can show
        # "nothing happening" distinct from "ran fine, shipped N".
        dest.last_exported_at = window_end
        dest.last_run_status = STATUS_NO_DATA
        dest.last_run_rows = 0
        dest.last_run_error = None
        self.db.flush()
        return RunResult(
            destination_id=dest.id,
            outcome=RunOutcome.NO_DATA,
            rows_exported=0,
            window_start=window_start,
            window_end=window_end,
        )

    def _record_failure(
        self,
        dest: AuditExportDestination,
        window_start: Optional[datetime],
        window_end: datetime,
        error: str,
    ) -> RunResult:
        # Do NOT advance last_exported_at. Next run retries the
        # same window — at-least-once delivery; SIEM dedupes by id.
        dest.last_run_status = STATUS_FAILURE
        dest.last_run_rows = 0
        dest.last_run_error = error[:MAX_ERROR_LENGTH]
        self.db.flush()
        log.error(
            "Audit export %s FAILED: tenant=%s error=%s",
            dest.id, dest.tenant_id, error,
        )
        return RunResult(
            destination_id=dest.id,
            outcome=RunOutcome.FAILURE,
            rows_exported=0,
            window_start=window_start,
            window_end=window_end,
            error=error,
        )
