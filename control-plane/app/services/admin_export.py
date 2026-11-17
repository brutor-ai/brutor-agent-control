# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin Export Helpers

Streaming CSV / JSONL export utilities shared by admin endpoints
that export tabular data (proxy logs, audit logs, ...).

Design principles:
- Output is streamed (chunked) rather than fully buffered.
- Every export carries a self-describing metadata header so that
  downstream tooling can verify schema version, filters, exporter,
  and row count.
- A download holds at most ``EXPORT_ROW_CAP`` rows; a query that matches
  more is refused with HTTP 413, to be narrowed and downloaded in parts.
"""

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from fastapi import HTTPException, status

EXPORT_ROW_CAP = 50_000
EXPORT_SCHEMA_VERSION = "1.0"

# ============================================================================
# Cap enforcement
# ============================================================================


# What else delivers a resource that is too large to download at once.
SCHEDULED_DELIVERY = {
    "proxy log rows": (
        " To receive every row, set up an audit-export destination "
        "(/v1/admin/audit-export-destinations): it delivers the proxy log to your own "
        "storage or SIEM on a schedule, with no limit on rows."
    ),
}


def assert_within_cap(total: int, resource: str = "rows") -> None:
    """Raise HTTP 413 if a query would exceed the download cap. The
    message says what the administrator can do about it."""
    if total > EXPORT_ROW_CAP:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"The download would hold {total} {resource}; one download holds at most "
                f"{EXPORT_ROW_CAP}. Choose a shorter time range or add filters, and download "
                f"the rest in further parts." + SCHEDULED_DELIVERY.get(resource, "")
            ),
        )


# ============================================================================
# Metadata header
# ============================================================================


def build_meta(
    *,
    resource: str,
    format_type: str,
    exported_by: str,
    filters: Dict[str, Any],
    row_count: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the metadata block embedded at the top of every export."""
    meta: Dict[str, Any] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "resource": resource,
        "format": format_type,
        "exported_by": exported_by,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "row_count": row_count,
        "filters": _scrub_filters(filters),
    }
    if extra:
        meta.update(extra)
    return meta


def _scrub_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None/empty filter values for a cleaner header."""
    out: Dict[str, Any] = {}
    for k, v in filters.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ============================================================================
# Filename
# ============================================================================


def export_filename(
    resource: str,
    format_type: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> str:
    """Build a deterministic, range-aware filename."""
    fmt = "%Y%m%dT%H%M%SZ"
    now = datetime.now(timezone.utc)
    s = (start_time or None)
    e = (end_time or now)
    s_str = s.strftime(fmt) if s else "all"
    e_str = e.strftime(fmt)
    ext = "jsonl" if format_type == "jsonl" else "csv"
    return f"{resource}-{s_str}-to-{e_str}.{ext}"


def media_type_for(format_type: str) -> str:
    if format_type == "jsonl":
        return "application/x-ndjson"
    return "text/csv"


# ============================================================================
# CSV streaming
# ============================================================================


_CSV_CHUNK_ROWS = 200


def _csv_value(v: Any) -> str:
    """Coerce a value for CSV output. Bools → lowercase; None → empty;
    dicts/lists → compact JSON."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), default=str)
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _csv_meta_lines(meta: Dict[str, Any]) -> List[str]:
    """Render meta as ``# key: value`` comment lines for CSV consumers."""
    lines: List[str] = []
    lines.append(f"# brutor-export schema_version={meta['schema_version']}")
    for k, v in meta.items():
        if k == "schema_version":
            continue
        if isinstance(v, (dict, list)):
            v_str = json.dumps(v, separators=(",", ":"), default=str)
        elif isinstance(v, datetime):
            v_str = v.isoformat()
        else:
            v_str = str(v)
        lines.append(f"# {k}: {v_str}")
    return lines


def stream_csv(
    *,
    records: Iterable[Dict[str, Any]],
    headers: List[str],
    meta: Dict[str, Any],
    row_extractor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Iterator[bytes]:
    """Yield CSV bytes: meta comment block, header row, then data rows.

    ``row_extractor`` lets callers flatten/rename fields without copying
    the whole record list ahead of time.
    """
    # Meta comment lines
    head = "\n".join(_csv_meta_lines(meta)) + "\n"
    yield head.encode("utf-8")

    # Header row
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    yield buf.getvalue().encode("utf-8")

    # Data rows in chunks
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    rows_in_chunk = 0
    for rec in records:
        flat = row_extractor(rec) if row_extractor else rec
        writer.writerow([_csv_value(flat.get(h)) for h in headers])
        rows_in_chunk += 1
        if rows_in_chunk >= _CSV_CHUNK_ROWS:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate()
            rows_in_chunk = 0
    if rows_in_chunk > 0:
        yield buf.getvalue().encode("utf-8")


# ============================================================================
# JSONL streaming
# ============================================================================


def stream_jsonl(
    *,
    records: Iterable[Dict[str, Any]],
    meta: Dict[str, Any],
) -> Iterator[bytes]:
    """Yield JSONL bytes: first line is ``{"_meta": ...}``, then one record
    per line."""
    yield (json.dumps({"_meta": meta}, default=str) + "\n").encode("utf-8")
    for rec in records:
        yield (json.dumps(rec, default=str) + "\n").encode("utf-8")


# ============================================================================
# Proxy log flattening (CSV view)
# ============================================================================


PROXY_LOG_CSV_HEADERS: List[str] = [
    "id",
    "timestamp",
    "tenant_id",
    "proxy_type",
    "proxy_subtype",
    "correlation_id",
    "parent_request_id",
    "request_id",
    # "principal" is the authenticated identity that hit the gateway —
    # either an end user (username) or, for a group-scoped API key, the
    # bound resource group. Matches the "Principal" column in the admin
    # proxy-log table. The federated IdP identity (upstream OAuth) is
    # exported separately in the idp_user_* columns below.
    "principal_id",
    "principal_username",
    "idp_user_id",
    "idp_user_username",
    "idp_user_email",
    "session_id",
    "client_ip",
    "user_agent",
    "server_id",
    "idp_server_id",
    "method",
    "endpoint",
    "target_url",
    "request_size_bytes",
    "request_body",
    "status_code",
    "response_size_bytes",
    "response_body",
    "duration_ms",
    "success",
    "error_type",
    "error_message",
    "skill_id",
    "skill_execution_id",
    "mcp_method",
    "mcp_capability_name",
    "llm_provider",
    "llm_model_requested",
    "llm_model_used",
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "llm_total_tokens",
    "llm_streaming",
    "llm_function_calling",
    "llm_used_fallback",
    "llm_estimated_cost_usd",
    "llm_cache_hit",
    "llm_cache_layer",
    "oauth_flow_type",
    "oauth_scope",
    # Residency audit. Two columns per row so
    # operators can run "show me all blocked-by-residency requests
    # last month" against the CSV export without re-querying the DB.
    "region_routed_to",
    "residency_class",
    # Call-chain / multi-step trace. Six columns so
    # operators can reconstruct an LLM→tool→MCP→agent delegation graph from
    # the CSV: the four task-ids identify a step (parent_task_id is empty at a
    # root), trace_root_kind/trace_continuity say how the chain was joined.
    # Populated on every stamped surface (OpenAI-format LLM, MCP, Skills, A2A).
    "root_task_id",
    "current_task_id",
    "parent_task_id",
    "delegation_depth",
    "trace_root_kind",
    "trace_continuity",
]


def flatten_proxy_log(
    log: Dict[str, Any],
    *,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Flatten a ``ProxyLog.to_dict()`` payload into the CSV header schema."""
    req = log.get("request") or {}
    resp = log.get("response") or {}
    mcp = log.get("mcp") or {}
    llm = log.get("llm") or {}
    oauth = log.get("oauth") or {}

    return {
        "id": log.get("id"),
        "timestamp": log.get("timestamp"),
        "tenant_id": tenant_id or log.get("tenant_id"),
        "proxy_type": log.get("proxy_type"),
        "proxy_subtype": log.get("proxy_subtype"),
        "correlation_id": log.get("correlation_id"),
        "parent_request_id": log.get("parent_request_id"),
        "request_id": log.get("request_id"),
        # Principal = gateway-authenticated identity (user or group-key).
        # Source columns are unchanged on the model; only the exported
        # header is renamed to the industry-standard "principal".
        "principal_id": log.get("gateway_user_id"),
        "principal_username": log.get("gateway_user_username"),
        "idp_user_id": log.get("idp_user_id"),
        "idp_user_username": log.get("idp_user_username"),
        "idp_user_email": log.get("idp_user_email"),
        "session_id": log.get("session_id"),
        "client_ip": log.get("client_ip"),
        "user_agent": log.get("user_agent"),
        "server_id": log.get("server_id"),
        "idp_server_id": log.get("idp_server_id"),
        "method": req.get("method"),
        "endpoint": req.get("endpoint"),
        "target_url": req.get("target_url"),
        "request_size_bytes": req.get("size_bytes"),
        # Body fields land verbatim from to_dict()'s request/response
        # sub-objects. The Rust gateway log writers already truncate
        # and null-byte-strip these for storage, so what's exported is
        # what's queryable in the admin UI's expanded log row.
        "request_body": req.get("body_preview"),
        "status_code": resp.get("status_code"),
        "response_size_bytes": resp.get("size_bytes"),
        "response_body": resp.get("body_preview"),
        "duration_ms": resp.get("duration_ms"),
        "success": log.get("success"),
        "error_type": log.get("error_type"),
        "error_message": log.get("error_message"),
        "skill_id": log.get("skill_id"),
        "skill_execution_id": log.get("skill_execution_id"),
        "mcp_method": mcp.get("method"),
        "mcp_capability_name": mcp.get("capability_name"),
        "llm_provider": llm.get("provider"),
        "llm_model_requested": llm.get("model_requested"),
        "llm_model_used": llm.get("model_used"),
        "llm_prompt_tokens": llm.get("prompt_tokens"),
        "llm_completion_tokens": llm.get("completion_tokens"),
        "llm_total_tokens": llm.get("total_tokens"),
        "llm_streaming": llm.get("streaming"),
        "llm_function_calling": llm.get("function_calling"),
        "llm_used_fallback": llm.get("used_fallback"),
        "llm_estimated_cost_usd": llm.get("estimated_cost_usd"),
        "llm_cache_hit": llm.get("cache_hit"),
        "llm_cache_layer": llm.get("cache_layer"),
        "oauth_flow_type": oauth.get("flow_type"),
        "oauth_scope": oauth.get("scope"),
        "region_routed_to": log.get("region_routed_to"),
        "residency_class": log.get("residency_class"),
        # Call-chain trace (see header comment).
        "root_task_id": log.get("root_task_id"),
        "current_task_id": log.get("current_task_id"),
        "parent_task_id": log.get("parent_task_id"),
        "delegation_depth": log.get("delegation_depth"),
        "trace_root_kind": log.get("trace_root_kind"),
        "trace_continuity": log.get("trace_continuity"),
    }


# ============================================================================
# Audit log flattening (CSV view)
# ============================================================================


AUDIT_LOG_CSV_HEADERS: List[str] = [
    "id",
    "timestamp",
    "admin_id",
    "admin_type",
    "tenant_id",
    "affected_tenant_id",
    "action",
    "resource_type",
    "resource_id",
    "success",
    "ip_address",
    "user_agent",
    "request_method",
    "request_path",
    "details",
]


def flatten_audit_log(log: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an audit log dict (system or tenant) into the CSV header schema."""
    return {
        "id": log.get("id"),
        "timestamp": log.get("timestamp"),
        "admin_id": log.get("admin_id"),
        "admin_type": log.get("admin_type"),
        "tenant_id": log.get("tenant_id"),
        "affected_tenant_id": log.get("affected_tenant_id"),
        "action": log.get("action"),
        "resource_type": log.get("resource_type"),
        "resource_id": log.get("resource_id"),
        "success": log.get("success"),
        "ip_address": log.get("ip_address"),
        "user_agent": log.get("user_agent"),
        "request_method": log.get("request_method"),
        "request_path": log.get("request_path"),
        "details": log.get("details") or {},
    }
