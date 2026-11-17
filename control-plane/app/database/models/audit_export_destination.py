# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit Export Destination — per-tenant configuration of where the
gateway's scheduled background job should ship `proxy_logs` rows.

Two destination types in 0.8.x:

  * **s3** — S3 or any S3-compatible object store (MinIO, Cloudflare
    R2, Wasabi, GCS via S3-compatible endpoint). Logs are uploaded
    as gzipped JSONL files under `<prefix>/<tenant_id>/<window_end>.jsonl.gz`.
    Operators bring their own bucket + IAM creds; we never act on
    behalf of the customer's AWS account.

  * **webhook** — POSTs a JSONL body to an arbitrary HTTPS endpoint
    with optional `Authorization: Bearer …`. Generic enough to feed
    Splunk HEC, Datadog Logs HTTP intake, or a custom collector. The
    purpose-built Splunk HEC sink is a separate destination
    type (sharper config + tags).

Why this is per-tenant (not per-deployment):
  * The audit data BELONGS to the tenant, not the operator of the installation —
    each tenant gets to direct it at their own SIEM.
  * Compliance teams expect to set "send our org's logs to OUR S3
    bucket", not "trust the vendor to forward".
  * A multi-tenant installation could have dozens of destinations live
    simultaneously; the scheduler iterates them per-tenant.

Operator workflow:
  1. POST /v1/admin/audit-export-destinations with bucket + creds.
  2. Optionally POST .../test to verify the proxy can write.
  3. Enable. The scheduler picks it up on its next tick and starts
     exporting hourly (or whatever interval was configured).
  4. The `last_run_*` columns surface health in the admin UI.

Secret handling:
  * `secret_access_key` and `webhook_bearer` are encrypted via
    `EncryptionService` before persistence (same Fernet primitive
    used for OAuth tokens at rest). They never appear in to_dict()
    output or in API responses except for the brief moment between
    POST/PATCH and persistence.

Field allowlist:
  * If `field_allowlist` is null / empty, the export carries every
    flattened proxy-log column from `PROXY_LOG_CSV_HEADERS` in
    `admin_export.py`.
  * If set, only the named columns survive — used to strip PII
    columns (`request_body`, `response_body`, `idp_user_email`, etc.)
    before logs leave the org.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.database import Base


# Destination-type constants. String values are the wire format
# (request bodies, DB rows, admin UI labels). Don't rename without a
# migration — these strings are referenced by admin UI dropdowns.
DEST_TYPE_S3 = "s3"
DEST_TYPE_WEBHOOK = "webhook"
# Splunk HTTP Event Collector. Distinct from `webhook`
# because the wire format (HEC event envelope per record) and auth
# scheme (`Authorization: Splunk <token>` rather than Bearer)
# differ. Reuses the same httpx plumbing in the backend layer.
DEST_TYPE_SPLUNK_HEC = "splunk_hec"

# Status values stored in `last_run_status`. Surface these as-is in
# the admin UI; they're the operator's primary "is this thing working"
# signal.
STATUS_SUCCESS = "success"
STATUS_NO_DATA = "no_data"      # ran but had nothing to ship
STATUS_FAILURE = "failure"
STATUS_RUNNING = "running"      # transient — set at the start of a run,
                                # replaced on completion

# Default interval (1 hour). Operators tune via API; 60s is the
# enforced minimum to avoid pathological poll storms against S3.
# The secrets of each destination type: the name a caller sends it under ->
# the key of `config` its ciphertext is stored under.
SECRET_KEYS = {
    DEST_TYPE_S3: {"secret_access_key": "secret_access_key_encrypted"},
    # A webhook's headers carry credentials (an API key header): the whole
    # object is stored encrypted, and its names beside it in clear.
    DEST_TYPE_WEBHOOK: {"bearer_token": "bearer_token_encrypted", "headers": "headers_encrypted"},
    DEST_TYPE_SPLUNK_HEC: {"token": "token_encrypted"},
}


# Stored beside a webhook's encrypted headers: their names, which are shown.
HEADER_NAMES_KEY = "header_names"


def stored_mark(secret_name: str) -> str:
    """The key of a returned `config` that says whether the secret is stored."""
    return f"has_{secret_name}"


def public_config(destination_type: str, config) -> dict:
    """`config` without anything stored encrypted, and with a stored-mark
    for each secret of the destination type."""
    stored = dict(config or {})
    secrets = SECRET_KEYS.get(destination_type, {})
    # Nothing stored encrypted, and nothing under a secret's own name (a
    # value stored there in clear by an earlier version is not shown).
    view = {
        key: value for key, value in stored.items()
        if not key.endswith("_encrypted") and key not in secrets
    }
    for name, stored_key in secrets.items():
        view[stored_mark(name)] = bool(stored.get(stored_key))
    return view


DEFAULT_INTERVAL_SECONDS = 3600
MIN_INTERVAL_SECONDS = 60


class AuditExportDestination(Base):
    """One configured sink for scheduled proxy_logs export.

    Created per-tenant by tenant admins. The platform's audit-export
    scheduler picks up every enabled row on its tick, computes the
    rows written since `last_exported_at`, projects them through
    `field_allowlist`, and ships them via the appropriate backend.

    The same tenant can configure multiple destinations (e.g. one
    long-retention S3 bucket plus a Splunk HEC webhook for live
    visibility); each runs independently.
    """

    __tablename__ = "audit_export_destinations"

    id = Column(
        String(64),
        primary_key=True,
        comment="ULID-shaped identifier, prefix axd-",
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Tenant this destination belongs to",
    )

    name = Column(
        String(255),
        nullable=False,
        comment="Operator-facing label, unique within tenant",
    )

    destination_type = Column(
        String(32),
        nullable=False,
        comment="Backend type: 's3' or 'webhook'. See module constants.",
    )

    enabled = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Disabled destinations are skipped by the scheduler",
    )

    # ------------------------------------------------------------------
    # Type-specific configuration (JSON; shape depends on destination_type)
    # ------------------------------------------------------------------
    #
    # S3:
    #   {
    #     "bucket": "my-audit-bucket",
    #     "prefix": "brutor/proxy-logs",       # optional, no leading slash
    #     "region": "us-east-1",
    #     "access_key_id": "AKIA…",
    #     "secret_access_key_encrypted": "<Fernet token>",
    #     "endpoint_url": null | "https://s3.example.com",
    #     "sse": null | "AES256" | "aws:kms",  # server-side encryption
    #     "sse_kms_key_id": null | "arn:aws:kms:…"
    #   }
    #
    # Webhook:
    #   {
    #     "url": "https://splunk.example.com/services/collector",
    #     "headers": {"X-Custom-Header": "…"},  # optional extra headers
    #     "bearer_token_encrypted": null | "<Fernet token>"
    #   }
    config = Column(
        JSON,
        nullable=False,
        default=dict,
        comment="Backend-specific config; secrets stored encrypted",
    )

    field_allowlist = Column(
        JSON,
        nullable=True,
        comment=(
            "Optional list of proxy-log columns to include. None/empty = "
            "include all PROXY_LOG_CSV_HEADERS fields. Used to redact "
            "PII columns before logs leave the org."
        ),
    )

    interval_seconds = Column(
        Integer,
        nullable=False,
        default=DEFAULT_INTERVAL_SECONDS,
        comment=(
            f"How often the scheduler runs this destination. Minimum "
            f"{MIN_INTERVAL_SECONDS}s — anything tighter is rejected at "
            f"the API layer to avoid poll storms."
        ),
    )

    # ------------------------------------------------------------------
    # High-water mark + last-run state
    # ------------------------------------------------------------------
    #
    # `last_exported_at` is the position the next run starts from: only
    # rows the gateway wrote after it (`proxy_logs.created_at`) are picked
    # up. It is a time of writing, not of the calls: a row written late for
    # a call that started earlier is after it. Advancing this only on
    # success means a failed run retries the same window on next tick
    # (at-least-once delivery; the SIEM side dedupes by `proxy_logs.id`).

    last_exported_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Position of the export: the time of writing (proxy_logs.created_at) "
            "up to which rows were delivered by the last successful run."
        ),
    )

    last_run_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Wall-clock of the most recent run attempt (success or failure)",
    )

    last_run_status = Column(
        String(32),
        nullable=True,
        comment="See STATUS_* constants in this module",
    )

    last_run_rows = Column(
        Integer,
        nullable=True,
        comment="Number of proxy-log rows shipped on the last run",
    )

    last_run_error = Column(
        Text,
        nullable=True,
        comment=(
            "Last failure message (truncated). Cleared on next success. "
            "Operator-facing — keep it actionable, not stack traces."
        ),
    )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_by = Column(
        String(255),
        nullable=True,
        comment="Admin id (system or tenant) who created this destination",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "name",
            name="uix_audit_export_dest_tenant_name",
        ),
        # The same index the migration creates, so create_all builds it too.
        Index("ix_audit_export_dest_tenant_id", "tenant_id"),
        Index(
            "ix_audit_export_dest_tenant_enabled",
            "tenant_id",
            "enabled",
        ),
        {
            "comment": (
                "Per-tenant scheduled exports of proxy_logs to S3 / "
                "webhook sinks. See model docstring for the full design."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<AuditExportDestination(id={self.id!r}, "
            f"tenant_id={self.tenant_id!r}, name={self.name!r}, "
            f"type={self.destination_type!r}, enabled={self.enabled})>"
        )

    def to_dict(self) -> dict:
        """The destination as the API returns it. No secret and no
        ciphertext of one is in it: `config` carries, for each secret of
        the destination type, `has_<name>` saying whether one is stored.
        The export service reads `self.config`, never this."""
        config_view = public_config(self.destination_type, self.config)

        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "destination_type": self.destination_type,
            "enabled": self.enabled,
            "config": config_view,
            "field_allowlist": self.field_allowlist,
            "interval_seconds": self.interval_seconds,
            "last_exported_at": (
                self.last_exported_at.isoformat()
                if self.last_exported_at
                else None
            ),
            "last_run_at": (
                self.last_run_at.isoformat() if self.last_run_at else None
            ),
            "last_run_status": self.last_run_status,
            "last_run_rows": self.last_run_rows,
            "last_run_error": self.last_run_error,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
            "created_by": self.created_by,
        }
