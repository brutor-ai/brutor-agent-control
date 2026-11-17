# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic shapes for audit-export-destination CRUD endpoints.

Per the project convention: response models MIRROR what
`AuditExportDestination.to_dict()` emits. If a new field is added to
the SQLAlchemy model, add it here too — Pydantic response models
SILENTLY DROP undeclared keys, which has historically caused subtle
API drift (the `wants_listener` bug from v0.7.95 was the canonical
case). Better to fail loudly here than to ship a stripped response.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.database.models.audit_export_destination import (
    DEST_TYPE_S3,
    DEST_TYPE_WEBHOOK,
    DEST_TYPE_SPLUNK_HEC,
    MIN_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
)

ALLOWED_TYPES = {DEST_TYPE_S3, DEST_TYPE_WEBHOOK, DEST_TYPE_SPLUNK_HEC}

#: One description for the allowlist wherever it appears (create, update,
#: response), because it is where an operator decides whether the export
#: stays verifiable.
FIELD_ALLOWLIST_DESCRIPTION = (
    "Optional list of proxy-log column names to include. Omit / pass null to "
    "ship every column. Used to redact PII columns before logs leave the org. "
    "A list must name at least one column and only columns that exist "
    "(a name that does not exist is refused). "
    "Every exported row also carries a `chain` object — the chain columns "
    "(`chain_id`, `chain_seq`, `prev_hash`, `row_hash`, `chain_version`) and "
    "every field the row's canonical format covers — from which a recipient "
    "recomputes `row_hash` offline; `chain_version` names the format the row "
    "was sealed under (`v4`, the one format this release writes and "
    "verifies; a row that names another format, or none, does not verify). "
    "Include `chain` in the allowlist to keep it: an "
    "allowlist without it makes the export unverifiable, and the signed "
    "export manifest says so (`chain_columns_included: false`)."
)


# ---------------------------------------------------------------------------
# Config: one rule per destination type, applied when a destination is
# created and when it is updated.
# ---------------------------------------------------------------------------

# destination type -> (keys it needs, keys it may also carry). A secret is
# named as a caller sends it; its stored form is never accepted.
CONFIG_KEYS: Dict[str, tuple] = {
    DEST_TYPE_S3: (
        ("bucket", "access_key_id", "secret_access_key"),
        ("prefix", "region", "endpoint_url", "sse", "sse_kms_key_id"),
    ),
    DEST_TYPE_WEBHOOK: (("url",), ("headers", "bearer_token")),
    DEST_TYPE_SPLUNK_HEC: (("url", "token"), ("index", "sourcetype", "source", "host")),
}

CONFIG_DESCRIPTION = (
    "Configuration of the destination, by `destination_type`. `s3`: `bucket`, "
    "`access_key_id`, `secret_access_key`, and optionally `prefix`, `region`, "
    "`endpoint_url`, `sse`, `sse_kms_key_id`. `webhook`: `url` (http or https), "
    "and optionally `headers` (an object of strings) and `bearer_token`. "
    "`splunk_hec`: `url` (http or https), `token`, and optionally `index`, "
    "`sourcetype`, `source`, `host`. Any other key is refused. "
    "`secret_access_key`, `bearer_token`, `token` and a webhook's `headers` "
    "(they carry credentials) are secrets: they are stored encrypted and never "
    "returned; a response carries `has_secret_access_key`, `has_bearer_token`, "
    "`has_token` or `has_headers` instead, and `header_names` for the headers."
)

CONFIG_UPDATE_DESCRIPTION = (
    CONFIG_DESCRIPTION
    + " On an update, `config` replaces the stored configuration except its "
    "secrets: a secret the request does not name stays as stored, a new value "
    "replaces it, and an empty string or null removes it (refused for a "
    "secret the destination type needs)."
)


def _is_http_url(value: Any) -> bool:
    from urllib.parse import urlsplit

    if not isinstance(value, str):
        return False
    parts = urlsplit(value.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def validate_destination_config(
    destination_type: str, config: Dict[str, Any], *, stored_secrets: frozenset = frozenset()
) -> Dict[str, Any]:
    """Refuse a configuration the destination type cannot run with.

    `stored_secrets` names the secrets already stored for the destination
    (an update): one of them may be left out of `config`, and is kept.
    Raises `ValueError` naming every problem.
    """
    from app.database.models.audit_export_destination import SECRET_KEYS

    required, optional = CONFIG_KEYS[destination_type]
    secrets = SECRET_KEYS[destination_type]
    problems: List[str] = []

    unknown = sorted(set(config) - set(required) - set(optional))
    if unknown:
        problems.append(
            f"config key(s) not accepted for destination_type {destination_type!r}: {unknown}"
            " (a secret is sent under its own name and is never read back)"
        )
    for key in required:
        value = config.get(key)
        if key in secrets and key not in config and key in stored_secrets:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"config.{key} is required for destination_type {destination_type!r}")
    for key in set(required) | set(optional):
        value = config.get(key)
        if key == "headers":
            if value is not None and not (
                isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            ):
                problems.append("config.headers must be an object of header name to string value")
        elif value is not None and not isinstance(value, str):
            problems.append(f"config.{key} must be a string")
    if "url" in required and config.get("url") and not _is_http_url(config["url"]):
        problems.append("config.url must be an http or https URL")
    if config.get("endpoint_url") and not _is_http_url(config["endpoint_url"]):
        problems.append("config.endpoint_url must be an http or https URL")
    if problems:
        raise ValueError("; ".join(problems))
    return config


def validate_field_allowlist(value: Optional[List[str]]) -> Optional[List[str]]:
    """Null exports every column. A list names existing columns, at least
    one: a list that named none would otherwise mean "every column"."""
    if value is None:
        return None
    from app.services.admin_export import PROXY_LOG_CSV_HEADERS

    valid = set(PROXY_LOG_CSV_HEADERS) | {"chain"}
    unknown = sorted({name for name in value if name not in valid})
    if unknown:
        raise ValueError(
            f"field_allowlist names column(s) that do not exist: {unknown}. "
            f"Columns: {sorted(valid)}"
        )
    if not value:
        raise ValueError("field_allowlist must name at least one column; pass null to export every column")
    return value


class CreateAuditExportDestinationRequest(BaseModel):
    """POST /v1/admin/audit-export-destinations body."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Operator-facing label; unique within tenant",
    )
    destination_type: str = Field(
        ...,
        description=f"One of: {sorted(ALLOWED_TYPES)}",
    )
    enabled: bool = Field(
        True,
        description="Disabled destinations are skipped by the scheduler",
    )
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description=CONFIG_DESCRIPTION,
    )
    field_allowlist: Optional[List[str]] = Field(
        None,
        description=FIELD_ALLOWLIST_DESCRIPTION,
    )
    interval_seconds: int = Field(
        DEFAULT_INTERVAL_SECONDS,
        ge=MIN_INTERVAL_SECONDS,
        description=(
            f"How often the scheduler runs this destination. "
            f"Minimum {MIN_INTERVAL_SECONDS}s — values below this "
            f"are rejected to avoid poll storms against S3/HEC."
        ),
    )

    @field_validator("destination_type")
    @classmethod
    def _type_must_be_allowed(cls, v: str) -> str:
        if v not in ALLOWED_TYPES:
            raise ValueError(
                f"destination_type must be one of {sorted(ALLOWED_TYPES)}; got {v!r}"
            )
        return v

    @field_validator("config")
    @classmethod
    def _config_must_fit_the_type(cls, v: Dict[str, Any], info) -> Dict[str, Any]:
        dest_type = (info.data or {}).get("destination_type")
        if dest_type not in ALLOWED_TYPES:
            return v  # the destination_type validator has refused it
        return validate_destination_config(dest_type, v)

    @field_validator("field_allowlist")
    @classmethod
    def _allowlist_names_columns(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return validate_field_allowlist(v)


class UpdateAuditExportDestinationRequest(BaseModel):
    """PATCH /v1/admin/audit-export-destinations/{id} body.

    All fields optional — only present fields are updated. Note:
    `destination_type` is intentionally NOT in this model. Changing
    the backend type would require re-validating + re-encrypting the
    entire config, and is an unusual enough operation that we
    require operators to delete + recreate instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = Field(None, description=CONFIG_UPDATE_DESCRIPTION)
    field_allowlist: Optional[List[str]] = Field(None, description=FIELD_ALLOWLIST_DESCRIPTION)
    interval_seconds: Optional[int] = Field(None, ge=MIN_INTERVAL_SECONDS)

    @field_validator("field_allowlist")
    @classmethod
    def _allowlist_names_columns(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return validate_field_allowlist(v)


class AuditExportDestinationResponse(BaseModel):
    """GET / POST / PATCH response shape. Mirrors `to_dict()`."""

    id: str
    tenant_id: str
    name: str
    destination_type: str
    enabled: bool
    config: Dict[str, Any] = Field(
        ...,
        description=(
            "The configuration without its secrets. For each secret of the "
            "destination type, `has_<name>` says whether one is stored "
            "(`has_secret_access_key`, `has_bearer_token`, `has_token`, `has_headers`); "
            "`header_names` lists the names of a webhook's stored headers."
        ),
    )
    field_allowlist: Optional[List[str]] = Field(..., description=FIELD_ALLOWLIST_DESCRIPTION)
    interval_seconds: int
    last_exported_at: Optional[str] = Field(
        ...,
        description=(
            "The position of the export: every row the gateway wrote up to this time has been "
            "delivered. A time of writing, not of the calls; a run stops two minutes short of now."
        ),
    )
    last_run_at: Optional[str]
    last_run_status: Optional[str]
    last_run_rows: Optional[int]
    last_run_error: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    created_by: Optional[str]


class AuditExportDestinationListResponse(BaseModel):
    destinations: List[AuditExportDestinationResponse]
    total: int


class RunResultResponse(BaseModel):
    """POST .../run-now response — what just happened."""

    destination_id: str
    outcome: str = Field(
        ...,
        description="One of: success | no_data | failure",
    )
    rows_exported: int
    window_start: Optional[str]
    window_end: str
    error: Optional[str]
