# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Proxy Log Service
Query and analyze proxy logs written by the Rust proxy.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc, func, cast, case, Text, true, select
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

from app.constants.oauth import GrantType
from app.constants.proxy import ProxyType
from app.core.logging_system import BrutorLogger
from app.database.models.proxy_log import ProxyLog


# The gateway writes a virtual server's rows as `virtual-mcp`
# (never `virtual_mcp`, which nothing writes).
VIRTUAL_MCP = ProxyType.VIRTUAL_MCP.value

# What the gateway writes in `oauth_flow_type` for a token request
# (`brutor-gateway-core/src/routes/oauth.rs`).
OAUTH_FLOW_AUTHORIZATION_CODE = GrantType.AUTHORIZATION_CODE.value
OAUTH_FLOW_REFRESH_TOKEN = GrantType.REFRESH_TOKEN.value

class UnifiedProxyLogService:
    """
    Read-only service for querying proxy logs.

    Logs are written by the Rust proxy. This service provides
    query, statistics, thread tracking, and cleanup capabilities
    for the admin API.
    """

    def __init__(self, logger: BrutorLogger):
        self.logger = logger

    # =========================================================================
    # QUERY LOGS - UPDATED WITH ALL FILTERS
    # =========================================================================

    async def query_logs(
        self,
        db: Session,
        tenant_id: str,
        # Time filters
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        # Proxy type filters
        proxy_types: Optional[List[str]] = None,
        proxy_subtype: Optional[str] = None,
        # Server filters
        server_id: Optional[str] = None,
        idp_server_id: Optional[str] = None,
        # User filters
        gateway_user_id: Optional[str] = None,
        idp_user_id: Optional[str] = None,
        idp_user_username: Optional[str] = None,
        idp_user_email: Optional[str] = None,
        # Backwards compatibility aliases
        user_id: Optional[str] = None,         # Deprecated - maps to idp_user_id
        username: Optional[str] = None,        # Deprecated - maps to idp_user_username
        session_id: Optional[str] = None,
        # Status filter
        success: Optional[bool] = None,
        # Correlation tracking
        correlation_id: Optional[str] = None,
        parent_request_id: Optional[str] = None,
        request_id: Optional[str] = None,
        # Network filters
        client_ip: Optional[str] = None,
        # Performance filters - NEW
        min_duration_ms: Optional[float] = None,
        max_duration_ms: Optional[float] = None,
        # MCP-specific filters
        mcp_method: Optional[str] = None,
        mcp_capability_name: Optional[str] = None,
        # LLM-specific filters
        llm_provider: Optional[str] = None,
        llm_model_requested: Optional[str] = None,
        llm_model_used: Optional[str] = None,
        min_tokens: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        llm_streaming: Optional[bool] = None,
        llm_function_calling: Optional[bool] = None,
        llm_used_fallback: Optional[bool] = None,
        # OAuth-specific filters
        oauth_flow_type: Optional[str] = None,
        # Compliance — exact tag match against compliance_tags JSONB.
        compliance_tag: Optional[str] = None,
        # ------------------------------------------------------------------
        # Multi-value (Datadog-style) filters. Each key is a column on
        # `proxy_logs`; the value is a list of acceptable values —
        # OR-within, AND-across. Passed in by the endpoint after
        # coalescing the singular legacy fields above. Special-cased
        # keys: `compliance_tags` uses the `&&` JSONB overlap operator
        # instead of equality.
        # ------------------------------------------------------------------
        multi_value_filters: Optional[Dict[str, List[Any]]] = None,
        # Free-text substring search across the body previews,
        # target_url, and error_message. Fallback ILIKE is used here
        # until a GIN tsvector index exists — the endpoint will
        # switch to ts_query once that index exists.
        q: Optional[str] = None,
        # Pagination and sorting
        limit: int = 100,
        offset: int = 0,
        sort_by: str = 'timestamp',
        sort_order: str = 'desc',
        include_bodies: bool = False,
    ) -> Dict[str, Any]:
        """
        Query logs with comprehensive filtering

        Returns:
            {
                'logs': List[Dict],
                'total': int,
                'limit': int,
                'offset': int,
                'has_more': bool
            }
        """

        try:
            # Normalise the multi-value filter map so the rest of the
            # function can do `column in mvf` checks without worrying
            # about None.
            mvf: Dict[str, List[Any]] = multi_value_filters or {}

            # Build base query
            query = db.query(ProxyLog).filter(
                ProxyLog.tenant_id == tenant_id
            )

            # Time filters
            if start_time:
                query = query.filter(ProxyLog.timestamp >= start_time)
            if end_time:
                query = query.filter(ProxyLog.timestamp <= end_time)

            # ----- Multi-value filters (Datadog-style faceted lists) ------
            # Translate each (column, [values]) into a single IN()
            # clause. compliance_tags is special-cased: the column is a
            # JSONB array, so we ask "does the row's tag array share any
            # element with this list?" via `jsonb_exists_any(col, text[])`
            # — the function behind the `?|` operator (OR among the
            # supplied tags, AND with every other filter). NB: JSONB has
            # NO `&&` operator (that's array-overlap) — using it raised
            # `operator does not exist: jsonb && jsonb`. We use the
            # function form rather than `.op("?|")` because the `?` in
            # `?|` collides with SQLAlchemy/DBAPI param escaping. Hits the
            # same GIN index `@>` and `?|` both use.
            for column, values in mvf.items():
                if not values:
                    continue
                if column == 'compliance_tags':
                    query = query.filter(
                        func.jsonb_exists_any(
                            ProxyLog.compliance_tags,
                            cast(values, ARRAY(Text)),
                        )
                    )
                else:
                    col = getattr(ProxyLog, column, None)
                    if col is None:
                        # Unknown column slipped past the endpoint
                        # validator — log + skip rather than 500.
                        self.logger.warning(
                            "query_logs: skipping unknown multi-value column",
                            context={"column": column, "values": values},
                        )
                        continue
                    query = query.filter(col.in_(values))

            # Proxy type filters — `proxy_types` is the original list
            # form and stays as-is. `proxy_subtype` (singular) only
            # applies when the multi-value map didn't already cover it.
            if proxy_types:
                query = query.filter(ProxyLog.proxy_type.in_(proxy_types))
            if proxy_subtype and 'proxy_subtype' not in mvf:
                query = query.filter(ProxyLog.proxy_subtype == proxy_subtype)

            # Server filters — singular fields are no-ops when the
            # multi-value map already covers the same column.
            if server_id and 'server_id' not in mvf:
                query = query.filter(ProxyLog.server_id == server_id)
            # Support both old and new parameter names for IDP server
            if idp_server_id:
                query = query.filter(ProxyLog.idp_server_id == idp_server_id)

            # User filters — same singular-skip rule as the server block.
            if gateway_user_id and 'gateway_user_id' not in mvf:
                query = query.filter(ProxyLog.gateway_user_id == gateway_user_id)
            if idp_user_id and 'idp_user_id' not in mvf:
                query = query.filter(ProxyLog.idp_user_id == idp_user_id)
            elif user_id and 'idp_user_id' not in mvf:  # Backwards compatibility
                query = query.filter(ProxyLog.idp_user_id == user_id)
            if idp_user_username and 'idp_user_username' not in mvf:
                query = query.filter(ProxyLog.idp_user_username == idp_user_username)
            elif username and 'idp_user_username' not in mvf:  # Backwards compatibility
                query = query.filter(ProxyLog.idp_user_username == username)
            if idp_user_email:
                query = query.filter(ProxyLog.idp_user_email == idp_user_email)
            if session_id:
                query = query.filter(ProxyLog.end_user_session_id == session_id)

            # Status filter
            if success is not None:
                query = query.filter(ProxyLog.success == success)

            # Correlation tracking
            if correlation_id:
                query = query.filter(ProxyLog.correlation_id == correlation_id)
            if parent_request_id:
                query = query.filter(ProxyLog.parent_request_id == parent_request_id)
            if request_id:
                query = query.filter(ProxyLog.request_id == request_id)

            # Network filters
            if client_ip:
                query = query.filter(ProxyLog.client_ip == client_ip)

            # Duration filters
            if min_duration_ms is not None:
                query = query.filter(ProxyLog.duration_ms >= min_duration_ms)
            if max_duration_ms is not None:
                query = query.filter(ProxyLog.duration_ms <= max_duration_ms)

            # MCP-specific filters — singular fields gated by the
            # multi-value map so a coalesced list doesn't get an extra
            # equality clause that would force-empty the result.
            if mcp_method:
                query = query.filter(ProxyLog.mcp_method == mcp_method)
            if mcp_capability_name and 'mcp_capability_name' not in mvf:
                query = query.filter(ProxyLog.mcp_capability_name == mcp_capability_name)

            # LLM-specific filters
            if llm_provider and 'llm_provider' not in mvf:
                query = query.filter(ProxyLog.llm_provider == llm_provider)
            if llm_model_requested:
                query = query.filter(ProxyLog.llm_model_requested == llm_model_requested)
            if llm_model_used and 'llm_model_used' not in mvf:
                query = query.filter(ProxyLog.llm_model_used == llm_model_used)
            if min_tokens is not None:
                query = query.filter(ProxyLog.llm_total_tokens >= min_tokens)
            if max_cost_usd is not None:
                query = query.filter(ProxyLog.llm_estimated_cost_usd <= max_cost_usd)
            if llm_streaming is not None:
                query = query.filter(ProxyLog.llm_streaming == llm_streaming)
            if llm_function_calling is not None:
                query = query.filter(ProxyLog.llm_function_calling == llm_function_calling)
            if llm_used_fallback is not None:
                query = query.filter(ProxyLog.llm_used_fallback == llm_used_fallback)

            # OAuth-specific filters
            if oauth_flow_type:
                query = query.filter(ProxyLog.oauth_flow_type == oauth_flow_type)

            # Compliance tag filter — JSONB containment. Hits the
            # partial GIN index on compliance_tags so cost stays flat
            # even as proxy_logs grows. The cast keeps the param a
            # JSON array literal (`["eu_ai_act.high"]`) which is
            # what `@>` expects against a `JSONB` column.
            #
            # Skip when `compliance_tags` is already in the
            # multi-value map — the operator's set was applied above
            # via the `&&` overlap operator (semantic superset of the
            # singular case).
            if compliance_tag and 'compliance_tags' not in mvf:
                query = query.filter(
                    ProxyLog.compliance_tags.op("@>")(
                        cast([compliance_tag], JSONB)
                    )
                )

            # Free-text search across the four text columns most
            # likely to carry operator-meaningful substrings. Until
            # a GIN tsvector index exists this is a simple ILIKE
            # disjunction — fine in dev / trial-bundle volumes, slow
            # at large multi-tenant scale (adding the index + a generated
            # tsvector column so we can switch to plainto_tsquery).
            if q:
                like = f"%{q}%"
                query = query.filter(
                    or_(
                        # Body text is only searchable by callers who may
                        # read bodies; otherwise a filter is a content oracle.
                        *([ProxyLog.request_body_preview.ilike(like),
                           ProxyLog.response_body_preview.ilike(like)] if include_bodies else []),
                        ProxyLog.target_url.ilike(like),
                        ProxyLog.error_message.ilike(like),
                    )
                )

            # Get total count before pagination
            total = query.count()

            # Apply sorting
            sort_column = getattr(ProxyLog, sort_by, ProxyLog.timestamp)
            if sort_order == 'desc':
                query = query.order_by(desc(sort_column))
            else:
                query = query.order_by(asc(sort_column))

            # Apply pagination
            logs = query.limit(limit).offset(offset).all()

            # Convert to dicts
            log_dicts = [log.to_dict(include_sensitive=include_bodies) for log in logs]

            return {
                'logs': log_dicts,
                'total': total,
                'limit': limit,
                'offset': offset,
                'has_more': (offset + limit) < total
            }

        except Exception as e:
            self.logger.error("Failed to query logs", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # FACETS AND TYPEAHEAD (Datadog-style filter discovery)
    # =========================================================================
    #
    # Two endpoints surface the same proxy_log columns to the admin UI:
    #
    #   facets()     - for every facetable column, return the top-N
    #                  most-frequent values + counts, scoped to the
    #                  same time-range / filter envelope the operator
    #                  has on the page. Used to populate the
    #                  FacetMultiSelect dropdowns AND the Datadog-style
    #                  sidebar value lists.
    #
    #   suggest()    - typeahead for a single column when the operator
    #                  starts typing. ILIKE on the prefix + same scope
    #                  + limit. Cheap because there's an index on
    #                  every facet column.
    #
    # The same `query_logs()` WHERE-clause-building logic is reused —
    # facets / suggest take the same scoping args so the counts ALWAYS
    # match what's about to appear in the table. Drift between the
    # search and facet endpoints is the #1 source of operator
    # confusion in audit-log UIs (Datadog had this bug for years), so
    # we share the path on purpose.

    # Whitelist of columns that operators can facet / suggest over.
    # Anything outside this set is rejected at the endpoint level so a
    # crafted body can't trigger a GROUP BY on a sensitive column
    # (request_body_preview, etc.). The TextLabel column gives the
    # facet a human-friendly title for the UI.
    _FACETABLE: Dict[str, str] = {
        "proxy_type":           "Proxy type",
        "proxy_subtype":        "Proxy subtype",
        "success":              "Status",
        "server_id":            "Resource",
        "gateway_user_id":      "Gateway user",
        "gateway_user_username": "Gateway username",
        "idp_user_id":          "IdP user ID",
        "idp_user_username":    "IdP user",
        "idp_user_email":       "IdP email",
        "target_url":           "Target URL",
        "http_status_code":     "HTTP status",
        "http_method":          "HTTP method",
        "mcp_capability_name":  "Capability",
        "mcp_method":           "MCP method",
        "llm_provider":         "LLM provider",
        "llm_model_used":       "LLM model",
        "region_routed_to":     "Region",
        "residency_class":      "Residency class",
        "client_ip":            "Client IP",
        "resource_group_id":    "Resource group",
        "api_key_name":         "API key",
        # JSONB array column — counted by UNNESTing each tag (special
        # path in facets(), not the scalar GROUP BY loop).
        "compliance_tags":      "Compliance",
    }

    async def facets(
        self,
        db: Session,
        tenant_id: str,
        scope_filters: Dict[str, Any],
        fields: Optional[List[str]] = None,
        per_field_limit: int = 50,
        include_bodies: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return {field: [{value, count}, ...]} for every facetable
        column scoped to the active filter envelope.

        Args:
            scope_filters: same dict shape the endpoint passes to
                query_logs (start_time / end_time / multi_value_filters /
                q / etc.). Reused verbatim so counts match the table.
            fields: subset of self._FACETABLE to populate; None = all.
                The UI's FacetSidebar passes None on first paint and a
                single-element list when an operator expands a
                previously-collapsed field (saves a round trip on the
                dozens of fields they won't look at).
            per_field_limit: top-N per facet. 50 keeps the dropdown
                scannable; deeper exploration uses /suggest.
        """
        try:
            if fields is None:
                fields = list(self._FACETABLE.keys())
            else:
                fields = [f for f in fields if f in self._FACETABLE]
            if not fields:
                return {}

            # Special-case compliance_tags: it's a JSONB array column,
            # not a scalar — the value-count needs to UNNEST the array
            # so each tag contributes to its own row. Handled via a
            # separate query path below; not in the field loop.
            #
            # Build the base WHERE-only query once and clone per facet
            # field. SQLAlchemy's `subquery()` materialises the WHERE
            # so each GROUP BY runs against the same row set without
            # re-applying every filter (and importantly, without
            # double-counting).
            base = self._build_scoped_query(db, tenant_id, scope_filters, include_bodies)
            base_subq = base.subquery()

            out: Dict[str, List[Dict[str, Any]]] = {}
            for field in fields:
                if field == "compliance_tags":
                    # see below
                    continue
                col = getattr(base_subq.c, field, None)
                if col is None:
                    continue
                rows = (
                    db.query(col.label("value"), func.count().label("count"))
                    .filter(col.isnot(None))
                    .group_by(col)
                    .order_by(func.count().desc())
                    .limit(per_field_limit)
                    .all()
                )
                out[field] = [
                    {"value": r.value, "count": r.count}
                    for r in rows
                ]

            # compliance_tags facet — the JSONB array column the scalar
            # loop above skips. Each row's tag array is UNNESTed so every
            # distinct tag gets its own {value, count} row. Reuses the same
            # scoped query as the table (counts always match) and joins the
            # set-returning function as a CROSS JOIN LATERAL. compliance_tags
            # is always a JSON array or NULL (NULL contributes zero rows
            # through the lateral), so no jsonb_typeof guard is needed.
            if "compliance_tags" in fields:
                # Isolated so a failure here can NEVER take down the sibling
                # facets — the first-paint request fetches every field in one
                # batch, and an exception would otherwise blank the whole
                # sidebar. Degrades to an empty compliance facet instead.
                try:
                    scoped = self._build_scoped_query(
                        db, tenant_id, scope_filters, include_bodies
                    ).subquery()
                    # CROSS JOIN LATERAL over the JSONB array — the documented
                    # SQLAlchemy table-valued form: select_from(base.join(fn,
                    # true())). NULL/absent arrays yield zero rows through the
                    # lateral, so they simply don't contribute.
                    elem = func.jsonb_array_elements_text(
                        scoped.c.compliance_tags
                    ).table_valued("value")
                    stmt = (
                        select(
                            elem.c.value.label("value"),
                            func.count().label("count"),
                        )
                        .select_from(scoped.join(elem, true()))
                        .where(scoped.c.compliance_tags.isnot(None))
                        .group_by(elem.c.value)
                        .order_by(func.count().desc())
                        .limit(per_field_limit)
                    )
                    rows = db.execute(stmt).all()
                    out["compliance_tags"] = [
                        {"value": r.value, "count": r.count} for r in rows
                    ]
                except Exception as e:  # noqa: BLE001 — must not blank siblings
                    self.logger.warning(
                        "compliance_tags facet computation failed; "
                        "returning empty facet",
                        context={"tenant_id": tenant_id, "error": str(e)},
                    )
                    out["compliance_tags"] = []

            return out
        except Exception as e:
            self.logger.error("Failed to compute facets", context={
                "tenant_id": tenant_id,
                "fields": fields,
                "error": str(e),
            }, exc_info=True)
            raise

    def resolve_display_names(
        self,
        db: Session,
        tenant_id: str,
        facets_result: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, str]]:
        """For ID-shaped facet columns, look up the human-readable
        name and return `{field: {id: name}}`.

        Today this covers `server_id` (which is union-shaped — the
        column can carry an MCP server id, virtual server id, LLM
        model id, or IdP server id depending on `proxy_type`) and
        `gateway_user_id`. For `server_id` we issue one IN() query
        per source table and merge the results; the four ID spaces
        don't collide in practice (each table uses its own prefixed
        ULIDs), so a simple dict merge is safe.

        Why this lives on the service rather than in the endpoint:
        the facets endpoint is consumed by both the Datadog-style
        sidebar AND the FacetMultiSelect dropdown; the display map
        needs to flow to both. Keeping the lookup colocated with
        `facets()` means a future caller can't accidentally render
        raw ULIDs.

        Performance: at most 5 extra queries (4 for server_id, 1 for
        gateway_user_id), each capped at `per_field_limit` IDs (50).
        Per-tenant FK indexes make these point-lookups under 1ms even
        at large volume.
        """
        # Lazy import so the service file stays import-cheap.
        from app.database.models.mcp_server import MCPServer
        from app.database.models.virtual_server import VirtualServer
        from app.database.models.llm_model import LLModel
        from app.database.models.idp_server import IdPServer
        from app.database.models.end_user import EndUser
        from app.database.models.resource_group import ResourceGroup

        display: Dict[str, Dict[str, str]] = {}

        # ---- server_id: union of MCP / VMCP / LLM / IdP ----
        # Each source-table lookup is independently fail-soft. A bad
        # value in one ID space (e.g. an OAuth flow stamped a malformed
        # IdP ID) must not crash the resolver and take down the whole
        # /facets endpoint — operators rely on facets working even
        # when proxy_logs has dirty historical data.
        server_rows = facets_result.get("server_id") or []
        server_ids = [r["value"] for r in server_rows if r.get("value")]
        if server_ids:
            merged: Dict[str, str] = {}
            for ModelCls in (MCPServer, VirtualServer, LLModel, IdPServer):
                try:
                    rows = (
                        db.query(ModelCls.id, ModelCls.name)
                        .filter(
                            ModelCls.tenant_id == tenant_id,
                            ModelCls.id.in_(server_ids),
                        )
                        .all()
                    )
                    for row_id, row_name in rows:
                        if row_name:
                            merged[row_id] = row_name
                except Exception as e:
                    # Rollback the failed transaction so subsequent
                    # queries in this resolver don't fail with
                    # "current transaction is aborted".
                    db.rollback()
                    self.logger.warning(
                        "display-name lookup failed for one source table",
                        context={
                            "tenant_id": tenant_id,
                            "model": ModelCls.__name__,
                            "error": str(e),
                        },
                    )
            if merged:
                display["server_id"] = merged

        # ---- llm_model_used: same table as the LLM half of server_id ----
        llm_rows = facets_result.get("llm_model_used") or []
        llm_ids = [r["value"] for r in llm_rows if r.get("value")]
        if llm_ids:
            try:
                rows = (
                    db.query(LLModel.id, LLModel.name)
                    .filter(LLModel.tenant_id == tenant_id, LLModel.id.in_(llm_ids))
                    .all()
                )
                mapping = {r_id: r_name for r_id, r_name in rows if r_name}
                if mapping:
                    display["llm_model_used"] = mapping
            except Exception as e:
                db.rollback()
                self.logger.warning(
                    "display-name lookup failed for llm_model_used",
                    context={"tenant_id": tenant_id, "error": str(e)},
                )

        # ---- gateway_user_id: OVERLOADED column ----
        #
        # `proxy_logs.gateway_user_id` (String) carries one of two things:
        #   * an INTEGER end_users.id  — user-scoped key / JWT identity
        #   * a STRING resource-group id — GROUP-scoped API keys have no
        #     end-user, so the proxy stamps the bound resource group here.
        # We resolve BOTH into one display map so the single "Gateway user"
        # facet reads "<username>" (real user) or "<group name>" (group key),
        # matching the proxy-log column. Splitting by int-shape first also
        # avoids feeding a string ULID into the Integer EndUser PK (which
        # would 500 the whole /facets call with InvalidTextRepresentation).
        user_rows = facets_result.get("gateway_user_id") or []
        user_vals = [r["value"] for r in user_rows if r.get("value")]
        user_ids_int: List[int] = []
        group_ids: List[str] = []
        for v in user_vals:
            try:
                user_ids_int.append(int(v))
            except (TypeError, ValueError):
                group_ids.append(v)

        gw_mapping: Dict[str, str] = {}

        # Non-integer values → resource group name (group-scoped API keys).
        if group_ids:
            try:
                rg_rows = (
                    db.query(ResourceGroup.id, ResourceGroup.name)
                    .filter(
                        ResourceGroup.tenant_id == tenant_id,
                        ResourceGroup.id.in_(group_ids),
                    )
                    .all()
                )
                for r_id, r_name in rg_rows:
                    if r_name:
                        gw_mapping[r_id] = r_name
            except Exception as e:
                db.rollback()
                self.logger.warning(
                    "display-name lookup failed for gateway_user_id (groups)",
                    context={"tenant_id": tenant_id, "error": str(e)},
                )

        if user_ids_int:
            try:
                rows = (
                    db.query(
                        EndUser.id,
                        EndUser.display_name,
                        EndUser.username,
                        EndUser.email,
                    )
                    .filter(
                        EndUser.tenant_id == tenant_id,
                        EndUser.id.in_(user_ids_int),
                    )
                    .all()
                )
                for u_id, u_display, u_username, u_email in rows:
                    # Prefer username so the facet label matches the proxy-log
                    # "Gateway/Idp User" column (which shows
                    # gateway_user_username, e.g. "sales-rep"). Fall back to
                    # display name / email only when the username is blank.
                    label = u_username or u_display or u_email
                    if label:
                        # The UI looks the map up by the facet value
                        # verbatim (always a string), so the map keys
                        # must be strings too.
                        gw_mapping[str(u_id)] = label
            except Exception as e:
                db.rollback()
                self.logger.warning(
                    "display-name lookup failed for gateway_user_id (users)",
                    context={"tenant_id": tenant_id, "error": str(e)},
                )

        if gw_mapping:
            display["gateway_user_id"] = gw_mapping

        return display

    async def suggest(
        self,
        db: Session,
        tenant_id: str,
        field: str,
        prefix: str,
        scope_filters: Dict[str, Any],
        limit: int = 20,
        include_bodies: bool = False,
    ) -> List[Dict[str, Any]]:
        """Typeahead for one facet column. ILIKE on prefix, scoped to
        the active filter set so suggestions only include values that
        would actually return rows."""
        if field not in self._FACETABLE:
            raise ValueError(f"field {field!r} is not facetable")
        if not prefix:
            return []
        try:
            # compliance_tags is a JSONB array, not a scalar — ILIKE on the
            # column itself raises `jsonb ILIKE text`. UNNEST and prefix-match
            # the individual tag strings instead (mirrors facets()).
            if field == "compliance_tags":
                # CROSS JOIN LATERAL over the JSONB array, prefix-matched on the
                # individual tag strings (documented table-valued form; same as
                # facets()). ILIKE on the column itself would raise
                # `jsonb ILIKE text`.
                scoped = self._build_scoped_query(
                    db, tenant_id, scope_filters, include_bodies
                ).subquery()
                elem = func.jsonb_array_elements_text(
                    scoped.c.compliance_tags
                ).table_valued("value")
                like = f"{prefix}%"
                stmt = (
                    select(
                        elem.c.value.label("value"),
                        func.count().label("count"),
                    )
                    .select_from(scoped.join(elem, true()))
                    .where(scoped.c.compliance_tags.isnot(None))
                    .where(elem.c.value.ilike(like))
                    .group_by(elem.c.value)
                    .order_by(func.count().desc())
                    .limit(limit)
                )
                rows = db.execute(stmt).all()
                return [{"value": r.value, "count": r.count} for r in rows]

            base_subq = self._build_scoped_query(db, tenant_id, scope_filters, include_bodies).subquery()
            col = getattr(base_subq.c, field, None)
            if col is None:
                return []
            like = f"{prefix}%"
            rows = (
                db.query(col.label("value"), func.count().label("count"))
                .filter(col.isnot(None))
                .filter(col.ilike(like))
                .group_by(col)
                .order_by(func.count().desc())
                .limit(limit)
                .all()
            )
            return [{"value": r.value, "count": r.count} for r in rows]
        except Exception as e:
            self.logger.error("Failed to suggest values", context={
                "tenant_id": tenant_id, "field": field, "prefix": prefix,
                "error": str(e),
            }, exc_info=True)
            raise

    def _build_scoped_query(
        self,
        db: Session,
        tenant_id: str,
        scope_filters: Dict[str, Any],
        include_bodies: bool,
    ):
        """Build the same WHERE clause `query_logs` would build, but
        return the query object (without LIMIT/ORDER) so the facets +
        suggest endpoints can layer GROUP BY on top.

        This is a *deliberate duplication* of the filter logic in
        query_logs() — keeping the two in sync is the whole point of
        sharing the scope_filters dict. Don't refactor into a single
        function unless you're prepared to also factor out the SQLA
        query object plumbing; the cost-benefit isn't worth it today.
        """
        q = db.query(ProxyLog).filter(ProxyLog.tenant_id == tenant_id)
        if scope_filters.get("start_time"):
            q = q.filter(ProxyLog.timestamp >= scope_filters["start_time"])
        if scope_filters.get("end_time"):
            q = q.filter(ProxyLog.timestamp <= scope_filters["end_time"])
        if scope_filters.get("proxy_types"):
            q = q.filter(ProxyLog.proxy_type.in_(scope_filters["proxy_types"]))
        if scope_filters.get("success") is not None:
            q = q.filter(ProxyLog.success == scope_filters["success"])

        mvf: Dict[str, List[Any]] = scope_filters.get("multi_value_filters") or {}
        for column, values in mvf.items():
            if not values:
                continue
            if column == "compliance_tags":
                # JSONB array overlap (OR among tags). See query_logs for
                # why jsonb_exists_any and not the non-existent `&&`.
                q = q.filter(
                    func.jsonb_exists_any(
                        ProxyLog.compliance_tags,
                        cast(values, ARRAY(Text)),
                    )
                )
            else:
                col = getattr(ProxyLog, column, None)
                if col is not None:
                    q = q.filter(col.in_(values))

        free_text = scope_filters.get("q")
        if free_text:
            like = f"%{free_text}%"
            q = q.filter(
                or_(
                    *([ProxyLog.request_body_preview.ilike(like),
                       ProxyLog.response_body_preview.ilike(like)] if include_bodies else []),
                    ProxyLog.target_url.ilike(like),
                    ProxyLog.error_message.ilike(like),
                )
            )
        return q

    # =========================================================================
    # THREAD AND ACTIVITY METHODS
    # =========================================================================

    async def get_thread(
        self,
        db: Session,
        tenant_id: str,
        correlation_id: str,
        include_bodies: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Get all requests in a correlated thread

        Returns logs in chronological order
        """

        try:
            logs = db.query(ProxyLog).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.correlation_id == correlation_id
            ).order_by(asc(ProxyLog.timestamp)).all()

            return [log.to_dict(include_sensitive=include_bodies) for log in logs]

        except Exception as e:
            self.logger.error("Failed to get thread", context={
                "tenant_id": tenant_id,
                "correlation_id": correlation_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def get_user_activity(
        self,
        db: Session,
        tenant_id: str,
        # Support multiple ways to identify user
        gateway_user_id: Optional[str] = None,
        idp_user_id: Optional[str] = None,
        idp_user_username: Optional[str] = None,
        # Backwards compatibility
        user_id: Optional[str] = None,
        # Time range
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        proxy_types: Optional[List[str]] = None,
        include_bodies: bool = False,
    ) -> Dict[str, Any]:
        """
        Get all activity for a user across all proxies

        Can query by gateway_user_id, idp_user_id, idp_user_username, or user_id (deprecated)
        """

        try:
            # Build base query
            query = db.query(ProxyLog).filter(
                ProxyLog.tenant_id == tenant_id
            )

            # Add time filters if provided
            if start_time:
                query = query.filter(ProxyLog.timestamp >= start_time)
            if end_time:
                query = query.filter(ProxyLog.timestamp <= end_time)

            # Add user filter - support multiple identifiers
            user_filter_applied = False
            if gateway_user_id:
                query = query.filter(ProxyLog.gateway_user_id == gateway_user_id)
                user_filter_applied = True
            if idp_user_id:
                query = query.filter(ProxyLog.idp_user_id == idp_user_id)
                user_filter_applied = True
            elif user_id:  # Backwards compatibility
                query = query.filter(ProxyLog.idp_user_id == user_id)
                user_filter_applied = True
            if idp_user_username:
                query = query.filter(ProxyLog.idp_user_username == idp_user_username)
                user_filter_applied = True

            if not user_filter_applied:
                raise ValueError("Must provide at least one user identifier")

            if proxy_types:
                query = query.filter(ProxyLog.proxy_type.in_(proxy_types))

            logs = query.order_by(asc(ProxyLog.timestamp)).all()

            # Return as dict with additional metadata
            log_dicts = [log.to_dict(include_sensitive=include_bodies) for log in logs]

            return {
                'logs': log_dicts,
                'total_requests': len(log_dicts),
                'successful_requests': sum(1 for log in log_dicts if log.get('success', False)),
                'failed_requests': sum(1 for log in log_dicts if not log.get('success', False))
            }

        except Exception as e:
            self.logger.error("Failed to get user activity", context={
                "tenant_id": tenant_id,
                "gateway_user_id": gateway_user_id,
                "idp_user_id": idp_user_id,
                "idp_user_username": idp_user_username,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # STATISTICS METHODS - ENHANCED
    # =========================================================================

    async def get_statistics(
        self,
        db: Session,
        tenant_id: str,
        start_time: datetime,
        end_time: datetime,
        proxy_types: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get aggregated statistics with enhanced calculations
        """

        try:
            query = db.query(ProxyLog).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.timestamp >= start_time,
                ProxyLog.timestamp <= end_time
            )

            if proxy_types:
                query = query.filter(ProxyLog.proxy_type.in_(proxy_types))

            # Basic counts
            total_requests = query.count()
            successful_requests = query.filter(ProxyLog.success == True).count()
            failed_requests = total_requests - successful_requests

            # Average calculations
            avg_duration = db.query(func.avg(ProxyLog.duration_ms)).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.timestamp >= start_time,
                ProxyLog.timestamp <= end_time,
                ProxyLog.duration_ms.isnot(None)
            ).scalar() or 0.0

            avg_request_size = db.query(func.avg(ProxyLog.request_size_bytes)).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.timestamp >= start_time,
                ProxyLog.timestamp <= end_time,
                ProxyLog.request_size_bytes.isnot(None)
            ).scalar() or 0.0

            avg_response_size = db.query(func.avg(ProxyLog.response_size_bytes)).filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.timestamp >= start_time,
                ProxyLog.timestamp <= end_time,
                ProxyLog.response_size_bytes.isnot(None)
            ).scalar() or 0.0

            # Get stats by proxy type
            stats_by_type = {}
            for proxy_type in ('mcp', 'llm', 'oauth', VIRTUAL_MCP):
                type_query = query.filter(ProxyLog.proxy_type == proxy_type)
                type_total = type_query.count()

                if type_total > 0:
                    type_successful = type_query.filter(ProxyLog.success == True).count()
                    type_avg_duration = db.query(func.avg(ProxyLog.duration_ms)).filter(
                        ProxyLog.tenant_id == tenant_id,
                        ProxyLog.proxy_type == proxy_type,
                        ProxyLog.timestamp >= start_time,
                        ProxyLog.timestamp <= end_time,
                        ProxyLog.duration_ms.isnot(None)
                    ).scalar() or 0.0

                    stats_by_type[proxy_type] = {
                        'total_requests': type_total,
                        'successful_requests': type_successful,
                        'failed_requests': type_total - type_successful,
                        'success_rate': (type_successful / type_total) if type_total > 0 else 0,
                        'avg_duration_ms': float(type_avg_duration)
                    }

            # LLM-specific stats
            llm_stats = None
            if not proxy_types or 'llm' in proxy_types:
                # `proxy_logs` is the largest table in the schema and this
                # window can be a month wide, so every extra pass is paid
                # for at the size of the estate. What follows used to be
                # five scans for the headline numbers plus a DISTINCT and
                # three more scans *per provider* for the rollup. Both are
                # one projection over one scan.
                llm_filters = (
                    ProxyLog.tenant_id == tenant_id,
                    ProxyLog.proxy_type == 'llm',
                    ProxyLog.timestamp >= start_time,
                    ProxyLog.timestamp <= end_time,
                )
                headline = db.query(
                    func.count(ProxyLog.id),
                    func.sum(case((ProxyLog.success == True, 1), else_=0)),
                    func.sum(ProxyLog.llm_total_tokens),
                    func.sum(ProxyLog.llm_estimated_cost_usd),
                    # AVG already ignores NULL duration rows, so the old
                    # explicit `isnot(None)` filter changed nothing.
                    func.avg(ProxyLog.duration_ms),
                ).filter(*llm_filters).one()

                llm_total = int(headline[0] or 0)

                if llm_total > 0:
                    llm_successful = int(headline[1] or 0)
                    total_tokens = int(headline[2] or 0)
                    total_cost = float(headline[3] or 0.0)

                    llm_stats = {
                        'total_requests': llm_total,
                        'successful_requests': llm_successful,
                        'failed_requests': llm_total - llm_successful,
                        'success_rate': llm_successful / llm_total,
                        'avg_duration_ms': float(headline[4] or 0.0),
                        'total_tokens': total_tokens,
                        'total_cost_usd': total_cost,
                        'avg_tokens_per_request': float(total_tokens / llm_total),
                        'avg_cost_per_request': float(total_cost / llm_total),
                    }

                    # By provider — one GROUP BY.
                    provider_rows = db.query(
                        ProxyLog.llm_provider,
                        func.count(ProxyLog.id),
                        func.sum(ProxyLog.llm_total_tokens),
                        func.sum(ProxyLog.llm_estimated_cost_usd),
                    ).filter(
                        *llm_filters,
                        ProxyLog.llm_provider.isnot(None),
                    ).group_by(ProxyLog.llm_provider).all()

                    llm_stats['by_provider'] = {
                        provider: {
                            'requests': int(requests or 0),
                            'tokens': int(tokens or 0),
                            'cost_usd': float(cost or 0.0),
                        }
                        for provider, requests, tokens, cost in provider_rows
                    }

            # MCP-specific stats
            mcp_stats = None
            if not proxy_types or 'mcp' in proxy_types or VIRTUAL_MCP in proxy_types:
                mcp_query = query.filter(
                    or_(
                        ProxyLog.proxy_type == 'mcp',
                        ProxyLog.proxy_type == VIRTUAL_MCP
                    )
                )
                mcp_total = mcp_query.count()

                if mcp_total > 0:
                    # Top methods
                    top_methods = db.query(
                        ProxyLog.mcp_method,
                        func.count(ProxyLog.id).label('count')
                    ).filter(
                        ProxyLog.tenant_id == tenant_id,
                        or_(
                            ProxyLog.proxy_type == 'mcp',
                            ProxyLog.proxy_type == VIRTUAL_MCP
                        ),
                        ProxyLog.timestamp >= start_time,
                        ProxyLog.timestamp <= end_time,
                        ProxyLog.mcp_method.isnot(None)
                    ).group_by(ProxyLog.mcp_method).order_by(desc('count')).limit(10).all()

                    # Top tools
                    top_tools = db.query(
                        ProxyLog.mcp_capability_name,
                        func.count(ProxyLog.id).label('count')
                    ).filter(
                        ProxyLog.tenant_id == tenant_id,
                        or_(
                            ProxyLog.proxy_type == 'mcp',
                            ProxyLog.proxy_type == VIRTUAL_MCP
                        ),
                        ProxyLog.timestamp >= start_time,
                        ProxyLog.timestamp <= end_time,
                        ProxyLog.mcp_capability_name.isnot(None)
                    ).group_by(ProxyLog.mcp_capability_name).order_by(desc('count')).limit(10).all()

                    mcp_stats = {
                        'total_requests': mcp_total,
                        'successful_requests': mcp_query.filter(ProxyLog.success == True).count(),
                        'failed_requests': mcp_query.filter(ProxyLog.success == False).count(),
                        'success_rate': (mcp_query.filter(ProxyLog.success == True).count() / mcp_total) if mcp_total > 0 else 0,
                        'avg_duration_ms': float(db.query(func.avg(ProxyLog.duration_ms)).filter(
                            ProxyLog.tenant_id == tenant_id,
                            or_(
                                ProxyLog.proxy_type == 'mcp',
                                ProxyLog.proxy_type == VIRTUAL_MCP
                            ),
                            ProxyLog.timestamp >= start_time,
                            ProxyLog.timestamp <= end_time,
                            ProxyLog.duration_ms.isnot(None)
                        ).scalar() or 0.0),
                        'top_methods': [{'method': method, 'count': count} for method, count in top_methods],
                        'top_tools': [{'tool_name': tool, 'count': count} for tool, count in top_tools]
                    }

            # OAuth-specific stats
            oauth_stats = None
            if not proxy_types or 'oauth' in proxy_types:
                # One projection over one scan, as for the LLM block. The
                # grant is in `oauth_flow_type`, as the gateway writes it
                # (`authorization_code`, `refresh_token`): this block
                # counted on `oauth_code_exchange` and `oauth_token_refresh`,
                # columns the table never had, and the statistics answered
                # 500 with one OAuth row in the window.
                oauth_total, oauth_successful, token_exchanges, token_refreshes, oauth_avg_duration = db.query(
                    func.count(ProxyLog.id),
                    func.sum(case((ProxyLog.success == True, 1), else_=0)),
                    func.sum(case((ProxyLog.oauth_flow_type == OAUTH_FLOW_AUTHORIZATION_CODE, 1), else_=0)),
                    func.sum(case((ProxyLog.oauth_flow_type == OAUTH_FLOW_REFRESH_TOKEN, 1), else_=0)),
                    func.avg(ProxyLog.duration_ms),
                ).filter(
                    ProxyLog.tenant_id == tenant_id,
                    ProxyLog.proxy_type == 'oauth',
                    ProxyLog.timestamp >= start_time,
                    ProxyLog.timestamp <= end_time,
                ).one()
                oauth_total = int(oauth_total or 0)

                if oauth_total > 0:
                    oauth_successful = int(oauth_successful or 0)
                    oauth_stats = {
                        'total_requests': oauth_total,
                        'successful_requests': oauth_successful,
                        'failed_requests': oauth_total - oauth_successful,
                        'success_rate': oauth_successful / oauth_total,
                        'avg_duration_ms': float(oauth_avg_duration or 0.0),
                        'token_exchanges': int(token_exchanges or 0),
                        'token_refreshes': int(token_refreshes or 0),
                    }

            # Build response matching StatisticsResponse Pydantic model
            return {
                'period_start': start_time.isoformat(),
                'period_end': end_time.isoformat(),
                'total_requests': total_requests,
                'successful_requests': successful_requests,
                'failed_requests': failed_requests,
                'success_rate': (successful_requests / total_requests) if total_requests > 0 else 0,
                'avg_duration_ms': float(avg_duration),
                'avg_request_size_bytes': float(avg_request_size),
                'avg_response_size_bytes': float(avg_response_size),
                'stats_by_type': {
                    'mcp': stats_by_type.get('mcp'),
                    'llm': llm_stats,
                    'oauth': oauth_stats,
                    'virtual_mcp': stats_by_type.get(VIRTUAL_MCP)
                },
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            self.logger.error("Failed to get statistics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # CLEANUP METHODS
    # =========================================================================

    async def cleanup_old_logs(
        self,
        db: Session,
        tenant_id: Optional[str],
        days_to_keep: int = 90,
        actor: str = "manual-cleanup",
    ) -> int:
        """
        Delete logs older than specified days, declaring what is deleted.

        The rows are positions of the audit chain: a prune declaration
        (`audit_chain_prunes`, reason `manual`) is written for them before
        they go, as the scheduled retention sweep writes one, so that the
        chain's verifier reports a declared prune and not a gap.

        Args:
            tenant_id: Tenant ID (None for system-wide cleanup)
            days_to_keep: Keep logs from last N days
            actor: Who asked, for the declaration

        Returns:
            Number of logs deleted
        """

        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)

            query = db.query(ProxyLog).filter(
                ProxyLog.timestamp < cutoff_date
            )

            if tenant_id:
                query = query.filter(ProxyLog.tenant_id == tenant_id)

            from app.database.models.audit_chain_prune import REASON_MANUAL
            from app.services.data_retention.sweeper import declare_prune

            doomed_by_tenant: Dict[Optional[str], list] = {}
            for row in query.with_entities(
                ProxyLog.tenant_id, ProxyLog.chain_id, ProxyLog.chain_seq, ProxyLog.row_hash
            ).all():
                doomed_by_tenant.setdefault(row[0], []).append((row[1], row[2], row[3]))
            for owner, doomed in doomed_by_tenant.items():
                declare_prune(db, tenant_id=owner, doomed=doomed, reason=REASON_MANUAL, actor=actor)

            deleted_count = query.delete(synchronize_session=False)
            db.flush()

            self.logger.debug("Cleaned up old logs", context={
                "tenant_id": tenant_id or "all_tenants",
                "deleted_count": deleted_count,
                "days_to_keep": days_to_keep,
                "cutoff_date": cutoff_date.isoformat()
            })

            return deleted_count

        except Exception as e:
            self.logger.error("Failed to cleanup logs", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # HEALTH CHECK - ENHANCED
    # =========================================================================

    async def get_system_health(self, db: Session) -> Dict[str, Any]:
        """Get logging system health status with comprehensive metrics"""

        try:
            # Test database connectivity
            db.execute("SELECT 1")

            # Get total logs count
            total_logs = db.query(func.count(ProxyLog.id)).scalar()

            # Get oldest and newest logs
            oldest_log = db.query(func.min(ProxyLog.timestamp)).scalar()
            newest_log = db.query(func.max(ProxyLog.timestamp)).scalar()

            # Get logs from last 24 hours
            twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
            logs_last_24h = db.query(func.count(ProxyLog.id)).filter(
                ProxyLog.timestamp >= twenty_four_hours_ago
            ).scalar()

            # Get errors from last 24 hours
            errors_last_24h = db.query(func.count(ProxyLog.id)).filter(
                ProxyLog.timestamp >= twenty_four_hours_ago,
                ProxyLog.success == False
            ).scalar()

            # Determine health status
            status = "healthy"
            issues = []

            if logs_last_24h == 0:
                status = "degraded"
                issues.append("No logs created in last 24 hours")

            if errors_last_24h > (logs_last_24h * 0.5) and logs_last_24h > 0:
                status = "degraded"
                issues.append(f"High error rate: {errors_last_24h}/{logs_last_24h} requests failed")

            return {
                "status": status,
                "database_connection": True,
                "total_logs": total_logs,
                "oldest_log_timestamp": oldest_log.isoformat() if oldest_log else None,
                "newest_log_timestamp": newest_log.isoformat() if newest_log else None,
                "logs_last_24h": logs_last_24h,
                "errors_last_24h": errors_last_24h,
                "avg_write_latency_ms": None,  # Implement if needed
                "storage_usage_mb": None,  # Implement if needed
                "issues": issues,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            self.logger.error("Health check failed", context={
                "error": str(e)
            }, exc_info=True)

            return {
                "status": "unhealthy",
                "database_connection": False,
                "total_logs": 0,
                "oldest_log_timestamp": None,
                "newest_log_timestamp": None,
                "logs_last_24h": 0,
                "errors_last_24h": 0,
                "avg_write_latency_ms": None,
                "storage_usage_mb": None,
                "issues": [str(e)],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

