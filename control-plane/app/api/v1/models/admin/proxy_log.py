# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Proxy Logs Pydantic Models for API Requests/Responses
"""

from app.constants.proxy import ProxyTypeName
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

# ============================================================================
# REQUEST MODELS
# ============================================================================

class LogQueryRequest(BaseModel):
    """Request model for querying logs - aligned with ProxyLog database fields"""

    # Time range
    start_time: Optional[datetime] = Field(None, description="Start time (ISO format)")
    end_time: Optional[datetime] = Field(None, description="End time (ISO format)")
    hours_ago: Optional[int] = Field(None, ge=1, le=720,
                                     description="Query last N hours (alternative to start/end time)")

    # Proxy type filters
    proxy_types: Optional[List[ProxyTypeName]] = Field(
        None,
        description="Filter by proxy types"
    )
    proxy_subtype: Optional[str] = Field(None, description="Filter by proxy subtype")

    # Server filters
    server_id: Optional[str] = Field(None, description="Filter by MCP/LLM server ID")
    idp_server_id: Optional[str] = Field(None, description="Filter by Identity Provider server ID (OAuth)")

    # User filters
    gateway_user_id: Optional[str] = Field(None, description="Filter by gateway user ID")
    idp_user_id: Optional[str] = Field(None, description="Filter by IDP user ID")
    idp_user_username: Optional[str] = Field(None, description="Filter by IDP username")
    idp_user_email: Optional[str] = Field(None, description="Filter by IDP user email")
    session_id: Optional[str] = Field(None, description="Filter by session ID")

    # Status filter
    success: Optional[bool] = Field(None, description="Filter by success status (true/false/null for all)")

    # Correlation tracking
    correlation_id: Optional[str] = Field(None, description="Filter by correlation ID (request threading)")
    parent_request_id: Optional[str] = Field(None, description="Filter by parent request ID")
    request_id: Optional[str] = Field(None, description="Filter by request ID (X-Request-ID header)")

    # Performance filter - NEW
    min_duration_ms: Optional[float] = Field(None, ge=0, description="Minimum duration in milliseconds")
    max_duration_ms: Optional[float] = Field(None, ge=0, description="Maximum duration in milliseconds")

    # MCP-specific filters - UPDATED to use mcp_capability_name
    mcp_method: Optional[str] = Field(None, description="Filter by MCP method")
    mcp_capability_name: Optional[str] = Field(None, description="Filter by MCP capability (tool/resource/prompt name)")

    # LLM-specific filters
    llm_provider: Optional[str] = Field(None, description="Filter by LLM provider")
    llm_model_requested: Optional[str] = Field(None, description="Filter by requested LLM model")
    llm_model_used: Optional[str] = Field(None, description="Filter by actual LLM model used")
    min_tokens: Optional[int] = Field(None, ge=0, description="Minimum total tokens")
    max_cost_usd: Optional[float] = Field(None, ge=0, description="Maximum cost in USD")
    llm_streaming: Optional[bool] = Field(None, description="Filter by streaming usage")
    llm_function_calling: Optional[bool] = Field(None, description="Filter by function calling usage")
    llm_used_fallback: Optional[bool] = Field(None, description="Filter by fallback usage")

    # OAuth-specific filters
    oauth_flow_type: Optional[str] = Field(None, description="Filter by OAuth flow type")

    # Network filters
    client_ip: Optional[str] = Field(None, description="Filter by client IP address")

    # Compliance filter — match a single tag (e.g. "eu_ai_act.high",
    # "soc2.cc7.2", "gdpr_art_30"). Uses the GIN containment operator
    # on proxy_logs.compliance_tags so the query stays index-fast.
    compliance_tag: Optional[str] = Field(
        None,
        description=(
            "Filter to rows whose compliance_tags array contains this "
            "exact tag (e.g. 'eu_ai_act.high', 'soc2.cc7.2')."
        ),
    )

    # ------------------------------------------------------------------
    # Multi-value (Datadog-style faceted) filters — OR within a field,
    # AND across fields. Every plural form is a strict superset of its
    # singular sibling above; the endpoint coalesces both into a single
    # IN-list per column. Singular fields are kept for back-compat with
    # any client that still sends `{server_id: "x"}` — they're folded
    # into `server_ids` at query time.
    #
    # Convention:
    #   - `*_in`  → new plural form for fields where the singular name
    #               already exists (avoids breaking change to type)
    #   - `*_ids` / `*_usernames` → natural plural where unambiguous
    # ------------------------------------------------------------------
    server_ids: Optional[List[str]] = Field(
        None,
        description="OR-match against any of these server IDs (extends server_id)."
    )
    gateway_user_ids: Optional[List[str]] = Field(None)
    idp_user_ids: Optional[List[str]] = Field(None)
    idp_user_usernames: Optional[List[str]] = Field(None)
    proxy_subtypes: Optional[List[str]] = Field(None)
    mcp_capability_names: Optional[List[str]] = Field(None)
    llm_providers: Optional[List[str]] = Field(None)
    llm_models_used: Optional[List[str]] = Field(None)
    target_urls: Optional[List[str]] = Field(None)
    http_status_codes: Optional[List[int]] = Field(None)
    region_routed_to: Optional[List[str]] = Field(None)
    residency_class: Optional[List[str]] = Field(None)
    # OR-match across multiple compliance tags (e.g. "any of
    # eu_ai_act.high OR hipaa.phi"). The existing `compliance_tag`
    # singular is folded into this list when both are present.
    compliance_tags_any: Optional[List[str]] = Field(
        None,
        description=(
            "OR-match any of these compliance tags via GIN overlap. "
            "Extends compliance_tag (singular)."
        ),
    )
    # Free-text search across body previews + target_url + error_message.
    # Implemented via the GIN tsvector index; fallback to
    # ILIKE if the index is missing.
    q: Optional[str] = Field(
        None,
        max_length=500,
        description="Substring search across request/response body previews, target_url, and error_message.",
    )

    # Pagination
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum logs to return")
    offset: int = Field(default=0, ge=0, description="Pagination offset")

    # Sorting
    sort_by: str = Field(default="timestamp", description="Sort field (timestamp, duration_ms, etc.)")
    sort_order: Literal['asc', 'desc'] = Field(default="desc", description="Sort order")

    @field_validator('sort_order')
    @classmethod
    def validate_sort_order(cls, v: str) -> str:
        if v.lower() not in ['asc', 'desc']:
            raise ValueError("Sort order must be 'asc' or 'desc'")
        return v.lower()

    def coalesce_filters(self) -> Dict[str, List[Any]]:
        """Fold singular legacy fields into their plural successors.

        Returns a dict keyed by the *column name* on `proxy_logs`,
        with a deduped list of values per column. The endpoint
        consumes this directly: `query.filter(ProxyLog.col.in_(list))`.
        Empty / unset filters are omitted so the caller can iterate
        with no risk of an empty IN().

        Why a helper instead of building filters inline: the same
        coalescing logic is reused by the /facets and /suggest
        endpoints to scope value-lookups to the active filter set —
        keeping it on the model means it can't drift.
        """
        out: Dict[str, List[Any]] = {}

        def _merge(column: str, single: Any, plural: Optional[List[Any]]) -> None:
            values: List[Any] = []
            if single is not None:
                values.append(single)
            if plural:
                values.extend(plural)
            if values:
                # Dedupe preserving order so the operator's
                # explicit choice ordering survives into the SQL.
                seen: set = set()
                deduped: List[Any] = []
                for v in values:
                    if v not in seen:
                        seen.add(v)
                        deduped.append(v)
                out[column] = deduped

        _merge('server_id',            self.server_id,            self.server_ids)
        _merge('gateway_user_id',      self.gateway_user_id,      self.gateway_user_ids)
        _merge('idp_user_id',          self.idp_user_id,          self.idp_user_ids)
        _merge('idp_user_username',    self.idp_user_username,    self.idp_user_usernames)
        _merge('proxy_subtype',        self.proxy_subtype,        self.proxy_subtypes)
        _merge('mcp_capability_name',  self.mcp_capability_name,  self.mcp_capability_names)
        _merge('llm_provider',         self.llm_provider,         self.llm_providers)
        _merge('llm_model_used',       self.llm_model_used,       self.llm_models_used)
        _merge('target_url',           None,                       self.target_urls)
        _merge('http_status_code',     None,                       self.http_status_codes)
        _merge('region_routed_to',     None,                       self.region_routed_to)
        _merge('residency_class',      None,                       self.residency_class)
        # compliance_tags is a JSONB array column — the endpoint
        # handles it specially with the `&&` overlap operator, but
        # we still coalesce here so it has a uniform value list.
        _merge('compliance_tags',      self.compliance_tag,        self.compliance_tags_any)

        return out


# ============================================================================
# FACETS + TYPEAHEAD (Datadog-style filter discovery)
# ============================================================================

class FacetsRequest(LogQueryRequest):
    """Request model for /facets — same shape as /search so the
    counts the dropdowns surface reflect exactly the table view.

    Adds two optional knobs:
      * `fields`: restrict the response to one or more facetable
        columns. None = the full set returned by the service.
      * `per_field_limit`: cap on values per facet. 50 by default;
        the FacetMultiSelect uses /suggest for the long tail.
    """
    fields: Optional[List[str]] = Field(
        None,
        description=(
            "Restrict the response to these facet field names. "
            "Defaults to every facetable column known to the service."
        ),
    )
    per_field_limit: int = Field(
        50, ge=1, le=200,
        description="Maximum distinct values returned per facet.",
    )


class FacetValue(BaseModel):
    """One row of a facet — the column value and how many proxy_log
    rows in the active filter scope carry it."""
    value: Any = Field(..., description="The column value (string / int / bool).")
    count: int = Field(..., ge=0, description="Number of matching rows in the active scope.")


class FacetsResponse(BaseModel):
    """{field: [FacetValue, ...]} keyed by column name. Empty list
    means the field has zero non-null values in the active scope —
    the UI then collapses that sidebar section to a hint.

    `display_names` is the human-readable label per ID for fields
    whose `value` is opaque (server_id, gateway_user_id, llm_model_used).
    The UI keeps `value` as the filter key (what the search endpoint
    accepts) and renders `display_names[field][value]` for the label.
    Fields without resolvable names are omitted; the UI falls back to
    showing the raw value.
    """
    facets: Dict[str, List[FacetValue]] = Field(default_factory=dict)
    display_names: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class SuggestRequest(LogQueryRequest):
    """Request model for /suggest — extends the search envelope with
    the field + prefix the operator is typing into."""
    field: str = Field(..., description="Column to typeahead over.")
    prefix: str = Field(..., max_length=200, description="Prefix the operator typed.")
    suggest_limit: int = Field(
        20, ge=1, le=100,
        description="Maximum suggestions returned. Lower than facets per_field_limit because typeahead is interactive.",
    )


class SuggestResponse(BaseModel):
    """List of {value, count} ordered by descending count, matching
    the column's prefix in the active filter scope."""
    suggestions: List[FacetValue] = Field(default_factory=list)


class StatsQueryRequest(BaseModel):
    """Request model for querying statistics"""

    # Time range
    start_time: Optional[datetime] = Field(None, description="Start time (ISO format)")
    end_time: Optional[datetime] = Field(None, description="End time (ISO format)")
    hours_ago: Optional[int] = Field(None, ge=1, le=720, description="Stats from last N hours")

    # Filters
    proxy_types: Optional[List[ProxyTypeName]] = Field(
        None,
        description="Filter by proxy types"
    )
    server_id: Optional[str] = Field(None, description="Filter by server ID")
    gateway_user_id: Optional[str] = Field(None, description="Filter by gateway user ID")
    idp_user_id: Optional[str] = Field(None, description="Filter by IDP user ID")
    idp_user_username: Optional[str] = Field(None, description="Filter by IDP username")


class ExportRequest(BaseModel):
    """Request model for exporting logs"""

    # Format
    format: Literal['csv', 'json'] = Field(default='csv', description="Export format")

    # All the same filters as LogQueryRequest (but no pagination/sorting)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    hours_ago: Optional[int] = Field(None, ge=1, le=720)

    proxy_types: Optional[List[ProxyTypeName]] = None
    proxy_subtype: Optional[str] = None

    server_id: Optional[str] = None
    idp_server_id: Optional[str] = None

    # User filters
    gateway_user_id: Optional[str] = None
    idp_user_id: Optional[str] = None
    idp_user_username: Optional[str] = None
    idp_user_email: Optional[str] = None
    session_id: Optional[str] = None

    success: Optional[bool] = None

    correlation_id: Optional[str] = None
    parent_request_id: Optional[str] = None
    request_id: Optional[str] = None

    # Performance filters
    min_duration_ms: Optional[float] = None
    max_duration_ms: Optional[float] = None

    # MCP filters
    mcp_method: Optional[str] = None
    mcp_capability_name: Optional[str] = None

    # LLM filters
    llm_provider: Optional[str] = None
    llm_model_requested: Optional[str] = None
    llm_model_used: Optional[str] = None
    min_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None

    # OAuth filters
    oauth_flow_type: Optional[str] = None

    client_ip: Optional[str] = None


class CleanupRequest(BaseModel):
    """Request model for log cleanup"""
    days_to_keep: int = Field(default=90, ge=1, le=365, description="Keep logs from last N days")


# ============================================================================
# RESPONSE MODELS
# ============================================================================

class AdminAccessInfo(BaseModel):
    """Information about the admin who accessed the resource"""
    admin_id: str
    admin_username: str
    tenant_id: str


class ProxyLogPaginationInfo(BaseModel):
    """Pagination information"""
    total: int = Field(..., description="Total matching records")
    offset: int = Field(..., description="Current offset")
    limit: int = Field(..., description="Records per page")
    returned: int = Field(..., description="Records in this response")
    has_more: bool = Field(..., description="More results available")


class LogQueryResponse(BaseModel):
    """Response model for log query"""
    logs: List[Dict[str, Any]] = Field(..., description="List of log entries")
    pagination: ProxyLogPaginationInfo
    filters_applied: Dict[str, Any] = Field(..., description="Filters that were applied")
    accessed_by: AdminAccessInfo
    timestamp: str = Field(..., description="Response timestamp (ISO format)")


class ThreadQueryResponse(BaseModel):
    """Response model for request thread"""
    correlation_id: str = Field(..., description="Correlation ID")
    thread_length: int = Field(..., description="Number of requests in thread")
    logs: List[Dict[str, Any]] = Field(..., description="Chronological list of requests")
    accessed_by: AdminAccessInfo
    timestamp: str


class UserActivityResponse(BaseModel):
    """Response model for user activity"""
    gateway_user_id: Optional[str] = Field(None, description="Gateway User ID")
    idp_user_id: Optional[str] = Field(None, description="IDP User ID")
    idp_user_username: Optional[str] = Field(None, description="IDP Username")
    period_start: str = Field(..., description="Period start time (ISO format)")
    period_end: str = Field(..., description="Period end time (ISO format)")
    total_requests: int = Field(..., description="Total requests in period")
    successful_requests: int = Field(..., description="Successful requests")
    failed_requests: int = Field(..., description="Failed requests")
    logs: List[Dict[str, Any]] = Field(..., description="User's requests")
    accessed_by: AdminAccessInfo
    timestamp: str


class MCPStats(BaseModel):
    """MCP-specific statistics"""
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    avg_duration_ms: float
    top_methods: Optional[List[Dict[str, Any]]] = None
    top_capabilities: Optional[List[Dict[str, Any]]] = None  


class LLMStats(BaseModel):
    """LLM-specific statistics"""
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    avg_duration_ms: float
    total_tokens: int
    total_cost_usd: float
    avg_tokens_per_request: float
    avg_cost_per_request: float
    by_provider: Optional[Dict[str, Any]] = None
    by_model: Optional[Dict[str, Any]] = None


class OAuthStats(BaseModel):
    """OAuth-specific statistics"""
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    avg_duration_ms: float
    # Rows of the authorization-code grant and of the refresh-token grant
    # (`oauth_flow_type`, as the gateway writes it).
    token_exchanges: int
    token_refreshes: int
    by_idp: Optional[Dict[str, Any]] = None


class VirtualMCPStats(BaseModel):
    """Virtual MCP-specific statistics"""
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    avg_duration_ms: float


class StatsByType(BaseModel):
    """Statistics broken down by proxy type"""
    mcp: Optional[MCPStats] = None
    llm: Optional[LLMStats] = None
    oauth: Optional[OAuthStats] = None
    virtual_mcp: Optional[VirtualMCPStats] = None


class StatisticsResponse(BaseModel):
    """Response model for statistics"""
    period_start: str = Field(..., description="Period start (ISO format)")
    period_end: str = Field(..., description="Period end (ISO format)")
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    avg_duration_ms: float
    avg_request_size_bytes: float
    avg_response_size_bytes: float
    stats_by_type: StatsByType
    accessed_by: AdminAccessInfo
    timestamp: str


class ExportResponse(BaseModel):
    """Response model for log export"""
    message: str = Field(..., description="Status message")
    format: Literal['csv', 'json'] = Field(..., description="Export format")
    download_url: Optional[str] = Field(None, description="URL to download the file")
    data: Optional[str] = Field(None, description="Inline data (if small)")
    file_size_bytes: Optional[int] = Field(None, description="File size in bytes")
    record_count: int = Field(..., description="Number of records exported")
    exported_by: AdminAccessInfo
    timestamp: str


class CleanupResponse(BaseModel):
    """Response model for cleanup"""
    message: str = Field(..., description="Status message")
    deleted_count: int = Field(..., description="Number of logs deleted")
    days_to_keep: int = Field(..., description="Retention period used")
    tenant_id: str = Field(..., description="Tenant ID")
    deleted_by: AdminAccessInfo
    timestamp: str


class SystemHealthResponse(BaseModel):
    """Response model for system health"""
    status: Literal['healthy', 'degraded', 'unhealthy'] = Field(..., description="Overall health status")
    database_connection: bool = Field(..., description="Database connectivity")
    total_logs: int = Field(..., description="Total logs in database")
    oldest_log_timestamp: Optional[str] = Field(None, description="Oldest log timestamp")
    newest_log_timestamp: Optional[str] = Field(None, description="Newest log timestamp")
    logs_last_24h: int = Field(..., description="Logs created in last 24 hours")
    errors_last_24h: int = Field(..., description="Error logs in last 24 hours")
    avg_write_latency_ms: Optional[float] = Field(None, description="Average write latency")
    avg_duration_ms: Optional[float] = Field(None, description="Average request duration")
    storage_usage_mb: Optional[float] = Field(None, description="Storage used by logs")
    issues: List[str] = Field(default_factory=list, description="List of issues detected")
    timestamp: str


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def build_filters_dict(request: LogQueryRequest) -> Dict[str, Any]:
    """
    Build a dictionary of applied filters from request
    Useful for including in response
    """
    filters = {}

    # Only include non-None values
    for field_name, field_value in request.model_dump(exclude_none=True).items():
        # Skip pagination and sorting fields
        if field_name not in ['limit', 'offset', 'sort_by', 'sort_order']:
            filters[field_name] = field_value

    return filters