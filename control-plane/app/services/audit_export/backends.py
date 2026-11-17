# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit-export backends — the "actually ship the bytes somewhere" layer.

Each backend is a callable: given a payload (already gzipped JSONL) and
a destination's decrypted config dict, it returns either `None` on
success or an `Exception` (so the service layer can record it without
swallowing the traceback).

Backends are split out so:
  1. The service layer stays small and testable — its job is
     "pull rows, project, gzip, hand off, update high-water mark";
     it doesn't need to know S3 or HTTP details.
  2. Tests can swap the real backends for stubs by `monkeypatch`'ing
     the module-level `SEND_S3` / `SEND_WEBHOOK` functions.

Import-time:
  * `boto3` is imported lazily inside `send_s3` so deployments without
    an S3 destination configured never pay the boto3 startup cost
    (~150ms + ~5MB resident). Trial-bundle users in particular
    benefit — they almost never wire up an S3 sink.
  * `httpx` is already a core dep (it's everywhere in the gateway),
    so the webhook backend imports it at the top.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import httpx

from app.services.outbound_http import outbound_client


log = logging.getLogger("brutor-audit-export")


# ---------------------------------------------------------------------------
# S3 / S3-compatible backend
# ---------------------------------------------------------------------------


def send_s3(
    payload: bytes,
    object_key: str,
    *,
    bucket: str,
    region: Optional[str],
    access_key_id: str,
    secret_access_key: str,
    endpoint_url: Optional[str] = None,
    sse: Optional[str] = None,
    sse_kms_key_id: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Upload `payload` (gzipped JSONL bytes) to S3 at `object_key`.

    Synchronous — call this from a background task that's already off
    the event loop, OR wrap it in `asyncio.to_thread`. boto3's
    `put_object` is blocking and we deliberately don't fight it.

    The S3 client is constructed per-call. boto3 caches credential
    resolvers internally, and clients are cheap; spinning one up per
    upload isn't a measured hotspot at the volumes we ship (one
    request per destination per hour by default).

    Args:
        payload:          The gzipped JSONL bytes to upload.
        object_key:       Full key under the bucket, no leading slash.
                          The service layer computes this from prefix
                          + tenant_id + window-end timestamp.
        bucket:           S3 bucket name.
        region:           AWS region; pass `None` for S3-compatible
                          backends that don't care (MinIO, R2).
        access_key_id:    AWS access key ID.
        secret_access_key:Decrypted secret access key. NEVER log this.
        endpoint_url:     Optional S3-compatible endpoint. Setting it
                          unlocks MinIO / R2 / Wasabi / GCS-S3.
        sse:              Optional server-side encryption: "AES256" or
                          "aws:kms".
        sse_kms_key_id:   Required when sse == "aws:kms".

    Returns:
        (success, error_message). `error_message` is None on success;
        on failure it's a one-line operator-facing summary, NEVER the
        secret key or the full traceback.
    """
    # boto3 lazy-import — see module docstring for rationale
    try:
        import boto3
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            EndpointConnectionError,
        )
    except ImportError as e:
        return False, (
            f"boto3 not installed — required for the s3 backend ({e}). "
            "Reinstall the gateway with the standard dependency set."
        )

    client_kwargs: dict = {
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_access_key,
    }
    if region:
        client_kwargs["region_name"] = region
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url

    put_kwargs: dict = {
        "Bucket": bucket,
        "Key": object_key,
        "Body": payload,
        "ContentType": "application/x-ndjson",
        "ContentEncoding": "gzip",
    }
    if sse == "AES256":
        put_kwargs["ServerSideEncryption"] = "AES256"
    elif sse == "aws:kms":
        put_kwargs["ServerSideEncryption"] = "aws:kms"
        if sse_kms_key_id:
            put_kwargs["SSEKMSKeyId"] = sse_kms_key_id

    try:
        s3 = boto3.client("s3", **client_kwargs)
        s3.put_object(**put_kwargs)
        return True, None
    except EndpointConnectionError as e:
        # Bucket region mismatch / bad endpoint_url. The error message
        # from botocore is already operator-friendly here.
        return False, f"S3 endpoint unreachable: {e}"
    except ClientError as e:
        # NoSuchBucket, AccessDenied, InvalidAccessKeyId, etc.
        # error['Code'] is the actionable bit; error['Message'] is the
        # human-readable version. Surface both.
        err = e.response.get("Error", {}) if hasattr(e, "response") else {}
        code = err.get("Code", "UnknownError")
        msg = err.get("Message", str(e))
        return False, f"S3 {code}: {msg}"
    except BotoCoreError as e:
        return False, f"S3 client error: {e}"


# ---------------------------------------------------------------------------
# Generic webhook backend
# ---------------------------------------------------------------------------


def send_webhook(
    payload: bytes,
    *,
    url: str,
    headers: Optional[dict] = None,
    bearer_token: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> Tuple[bool, Optional[str]]:
    """POST `payload` to `url` as `application/x-ndjson` gzipped.

    Works for Splunk HEC (`https://…/services/collector`), Datadog
    Logs (`https://http-intake.logs.datadoghq.com/api/v2/logs`), and
    any custom HTTP receiver. A purpose-built Splunk
    HEC backend that handles HEC's specific event envelope; until
    then operators can wrap their payloads or use a small middlebox.

    Args:
        payload:        Gzipped JSONL bytes (same shape as the S3
                        backend's payload — same wire format means
                        we can re-target without rewriting the export
                        machinery).
        url:            Full HTTPS URL. http:// is allowed but logged
                        as a warning since audit data MUST stay
                        encrypted in transit.
        headers:        Extra request headers (e.g. Splunk's
                        `Authorization: Splunk <token>` format).
        bearer_token:   Optional `Authorization: Bearer <token>`.
                        If both `bearer_token` and a manual
                        Authorization header are supplied, the
                        manual header wins (callers can use this to
                        override the auth scheme).
        timeout_seconds:Hard timeout. Default 30s; up to operators
                        to extend if their SIEM is slow.

    Returns:
        (success, error_message). 2xx = success.
    """
    if url.startswith("http://"):
        log.warning(
            "Audit-export webhook URL uses plaintext HTTP — audit data "
            "is being transmitted unencrypted: %s",
            url,
        )

    final_headers = {
        "Content-Type": "application/x-ndjson",
        "Content-Encoding": "gzip",
    }
    if bearer_token:
        final_headers["Authorization"] = f"Bearer {bearer_token}"
    if headers:
        # Operator-supplied headers override defaults so they can fix
        # quirky receivers (Splunk wants `Content-Type: application/json`
        # even for ndjson, for example).
        final_headers.update(headers)

    try:
        # Use a fresh client per call. Persistent connections would be
        # nice but the export interval is in minutes/hours, not
        # milliseconds — pooling buys nothing here.
        with outbound_client(timeout=timeout_seconds) as client:
            resp = client.post(url, content=payload, headers=final_headers)
        if 200 <= resp.status_code < 300:
            return True, None
        # Surface a short error: HTTP code + first line of body.
        body_preview = (resp.text or "")[:200].replace("\n", " ")
        return False, f"webhook HTTP {resp.status_code}: {body_preview}"
    except httpx.TimeoutException:
        return False, f"webhook timed out after {timeout_seconds}s"
    except httpx.RequestError as e:
        return False, f"webhook request failed: {e}"


# ---------------------------------------------------------------------------
# Splunk HEC backend
#
# Splunk's HTTP Event Collector wants:
#   - Auth: `Authorization: Splunk <token>` (NOT Bearer)
#   - Body: newline-delimited JSON, one event PER LINE, each event
#     wrapped in an envelope: `{"event": <obj>, "time": <unix>,
#                              "host": <str>, "sourcetype": <str>,
#                              "source": <str>, "index": <str>}`
#   - Endpoint: `/services/collector/event` (raw collector wants
#     a different format that's not as friendly for structured logs)
#
# We transform the gateway's gzipped JSONL into the HEC envelope at
# send time. The metadata line (the first JSONL row containing
# `_meta`) is skipped — it isn't a real event, just bookkeeping.
# ---------------------------------------------------------------------------


def send_splunk_hec(
    payload: bytes,
    *,
    url: str,
    token: str,
    index: Optional[str] = None,
    source: Optional[str] = None,
    sourcetype: Optional[str] = "brutor:proxy_log",
    host: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> Tuple[bool, Optional[str]]:
    """POST gzipped JSONL `payload` to Splunk HEC as event-envelope-wrapped
    records.

    Each JSONL line (except the metadata-only first line) becomes one
    HEC event. The `time` field is pulled from the row's `timestamp`
    (parsed as ISO 8601) when present, otherwise defaults to "now".

    Args:
        url:        Full HEC endpoint, e.g.
                    `https://splunk.acme.com:8088/services/collector/event`
        token:      HEC token from Splunk's Data Inputs → HTTP Event
                    Collector. Decrypted by the service layer.
        index:      Optional Splunk index override. If unset, Splunk
                    routes to the token's configured default index.
        source:     Optional event source (e.g. tenant ID).
        sourcetype: Splunk sourcetype. Defaults to `brutor:proxy_log`
                    so operators can build a dashboard against a
                    stable name without depending on Brutor's
                    payload to declare it.
        host:       Optional event host (e.g. the gateway hostname).
    """
    import gzip
    import json
    import time

    if url.startswith("http://"):
        log.warning(
            "Splunk HEC URL uses plaintext HTTP — audit data is being "
            "transmitted unencrypted: %s",
            url,
        )

    # Decompress and split into rows. The metadata-only first row
    # (`{"_meta": {...}}`) is skipped — operators want Splunk to
    # index events, not envelopes.
    try:
        decompressed = gzip.decompress(payload).decode("utf-8")
    except Exception as e:
        return False, f"splunk_hec: failed to decompress payload: {e}"

    hec_lines = []
    for raw_line in decompressed.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("splunk_hec: skipping unparseable JSON line: %s", e)
            continue
        if isinstance(event, dict) and "_meta" in event:
            # Metadata line — skip.
            continue

        # Pull the event timestamp out so Splunk's _time field
        # aligns with the actual request time, not the export time.
        # Convert ISO 8601 → unix seconds with millisecond precision.
        event_time_unix: float
        ts_str = event.get("timestamp") if isinstance(event, dict) else None
        if isinstance(ts_str, str):
            try:
                from datetime import datetime
                event_time_unix = datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                event_time_unix = time.time()
        else:
            event_time_unix = time.time()

        envelope = {
            "event": event,
            "time": event_time_unix,
            "sourcetype": sourcetype,
        }
        if index:
            envelope["index"] = index
        if source:
            envelope["source"] = source
        if host:
            envelope["host"] = host
        hec_lines.append(json.dumps(envelope, separators=(",", ":")))

    if not hec_lines:
        # Nothing to ship after stripping the metadata line —
        # treat as success (the scheduler advances the high-water
        # mark, no spurious failure alerts).
        return True, None

    body = "\n".join(hec_lines).encode("utf-8")
    headers = {
        "Authorization": f"Splunk {token}",
        "Content-Type": "application/json",  # HEC ignores ndjson Content-Type
    }
    try:
        with outbound_client(timeout=timeout_seconds) as client:
            resp = client.post(url, content=body, headers=headers)
        if 200 <= resp.status_code < 300:
            return True, None
        body_preview = (resp.text or "")[:200].replace("\n", " ")
        return False, f"splunk_hec HTTP {resp.status_code}: {body_preview}"
    except httpx.TimeoutException:
        return False, f"splunk_hec timed out after {timeout_seconds}s"
    except httpx.RequestError as e:
        return False, f"splunk_hec request failed: {e}"


# ---------------------------------------------------------------------------
# Test-injection hooks
# ---------------------------------------------------------------------------
#
# The service layer reads these module-level names rather than
# importing `send_s3` / `send_webhook` directly, so tests can
# `monkeypatch.setattr("app.services.audit_export.backends.SEND_S3", …)`
# without having to mock boto3 / httpx underneath.

SEND_S3 = send_s3
SEND_WEBHOOK = send_webhook
SEND_SPLUNK_HEC = send_splunk_hec
