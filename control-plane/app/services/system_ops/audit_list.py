# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Audit list / filter / CSV export

The Audit tab is the compliance trail that backs every other System
sub-section. Every destructive click anywhere in the console emits
exactly one row to `system_operation_log` (see
`write_system_operation()` in `audit_row.py`). This module is the
read side — list / filter / facet / stream-as-CSV — that the Audit
tab UI talks to.

Design notes
------------

1.  The faceted-filter envelope mirrors the proxy-logs viewer
    (`UnifiedProxyLogService.query_logs`): every filter is AND-across,
    multi-value OR-within. The Audit tab reuses the same
    `FacetSidebar` / `FacetMultiSelect` / `TimeRangePicker` admin-UI
    components, so the wire shape stays predictable and the operator
    muscle memory transfers between the two viewers.

2.  Free-text `q` searches against the JSONB payload + before_state +
    after_state columns. On Postgres this uses an ILIKE against the
    column's text representation (`::text`) — slower than a tsvector
    GIN index but acceptable: the GIN index defined in the
    `a081_system_ops_console` migration is on `payload` and supports the `?` containment
    operator, not free-text. A077-style tsvector for system_ops will
    be a follow-up if the row count makes ILIKE painful.
    On SQLite (the test harness) the JSONB columns are plain JSON, so
    the `::text` cast becomes a no-op and ILIKE still works.

3.  The export is a *generator*, NOT a buffered list. Auditors with a
    wide date range hit hundreds of thousands of rows; streaming
    avoids loading the whole result into memory at once. We cap the
    synchronous export at `EXPORT_ROW_CAP` (50k) — same cap proxy
    logs use — so a runaway query 413s rather than 504-ing on a long
    OLAP scan.

4.  Tenant isolation is enforced by always pre-filtering on
    `tenant_id`. The system-admin "view across tenants" case is
    handled by the endpoint (it omits the filter); this module's
    contract is "scope to one tenant", same as the rest of the
    system_ops services.

5.  The audit list endpoint is `SYSTEM_READ`. Operators with
    `system:operate` automatically have `system:read` per the
    permission hierarchy, so this doesn't accidentally gate
    operators out of their own audit trail.
"""

from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import asc, cast, desc, func, or_
from sqlalchemy.orm import Query, Session
from sqlalchemy.types import String as SAString

from app.database.models.system_operation_log import SystemOperationLog


# ---------------------------------------------------------------------------
# Facetable columns
# ---------------------------------------------------------------------------
# The Audit-tab FacetSidebar exposes these three sidebar sections.
# action / scope_kind / actor_admin_id mirror the most common filter
# axes operators care about — "show me every cache_flush in the last
# 24h", "show me everything anchor@brutor did this week", etc.
# Anything outside this set is rejected at the endpoint to prevent a
# crafted body from triggering a GROUP BY on a sensitive column
# (payload, etc.).
_FACETABLE: Dict[str, str] = {
    "action": "Action",
    "scope_kind": "Scope",
    "actor_admin_id": "Actor",
    "actor_type": "Actor type",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_audit(
    db: Session,
    tenant_id: Optional[str],
    *,
    actions: Optional[List[str]] = None,
    scope_kinds: Optional[List[str]] = None,
    actor_admin_ids: Optional[List[str]] = None,
    actor_types: Optional[List[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_order: str = "desc",
) -> Tuple[List[Dict[str, Any]], int]:
    """Return one page of audit rows + a total count for the same filter.

    The filters follow the faceted-search convention used elsewhere in
    the admin UI: every list filter is OR-within-field, AND-across
    fields. `tenant_id=None` means "all tenants" — only the endpoint's
    sysadmin path passes that.

    Returns a tuple of (rows-as-dicts, total). Splitting it this way
    keeps the endpoint cheap: it doesn't have to re-walk the rows to
    compute a count, and the count uses the same WHERE clause so
    pagination footers can never drift from the table.
    """
    query = _build_scoped_query(
        db,
        tenant_id=tenant_id,
        actions=actions,
        scope_kinds=scope_kinds,
        actor_admin_ids=actor_admin_ids,
        actor_types=actor_types,
        start=start,
        end=end,
        q=q,
    )

    # Count BEFORE sort/limit — count() ignores ORDER BY anyway, but
    # paying for the sort cost on a count-only path would be wasteful.
    total = query.count()

    # Operators read top-to-bottom newest-first by default. `asc`
    # exists for the rare "scroll backwards through a known incident
    # window" path that downstream tooling may use.
    order_col = SystemOperationLog.created_at
    query = query.order_by(desc(order_col) if sort_order == "desc" else asc(order_col))

    rows = query.limit(limit).offset(offset).all()
    return [r.to_dict() for r in rows], total


def facets(
    db: Session,
    tenant_id: Optional[str],
    *,
    actions: Optional[List[str]] = None,
    scope_kinds: Optional[List[str]] = None,
    actor_admin_ids: Optional[List[str]] = None,
    actor_types: Optional[List[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    q: Optional[str] = None,
    per_field_limit: int = 50,
) -> Dict[str, List[Dict[str, Any]]]:
    """Top-N value+count for every facetable column.

    The same WHERE clause `list_audit()` would build is shared via a
    subquery — that way facet counts always reflect the same row set
    the table is about to render. Drift between the search and facet
    endpoints is the #1 source of operator confusion in audit-log UIs
    (Datadog had this bug for years), so we share the path on purpose.

    `per_field_limit=50` keeps the sidebar dropdown scannable; a
    future /suggest endpoint can deep-dive into a single field.
    """
    base = _build_scoped_query(
        db,
        tenant_id=tenant_id,
        actions=actions,
        scope_kinds=scope_kinds,
        actor_admin_ids=actor_admin_ids,
        actor_types=actor_types,
        start=start,
        end=end,
        q=q,
    )
    # `subquery()` materialises the WHERE so each GROUP BY runs
    # against the same row set without re-applying every filter
    # (and importantly, without double-counting). Same pattern as
    # UnifiedProxyLogService.facets().
    base_subq = base.subquery()

    out: Dict[str, List[Dict[str, Any]]] = {}
    for field in _FACETABLE:
        col = getattr(base_subq.c, field, None)
        if col is None:
            # Shouldn't happen — the facetable map is hand-maintained
            # against the ORM columns. Skip silently rather than 500.
            continue
        rows = (
            db.query(col.label("value"), func.count().label("count"))
            .filter(col.isnot(None))
            .group_by(col)
            .order_by(func.count().desc())
            .limit(per_field_limit)
            .all()
        )
        out[field] = [{"value": r.value, "count": r.count} for r in rows]
    return out


def stream_csv_rows(
    db: Session,
    tenant_id: Optional[str],
    *,
    actions: Optional[List[str]] = None,
    scope_kinds: Optional[List[str]] = None,
    actor_admin_ids: Optional[List[str]] = None,
    actor_types: Optional[List[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    q: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield flattened-dict rows for the CSV export.

    The endpoint wraps this in `admin_export.stream_csv()` along with
    the `AUDIT_CSV_HEADERS` list below so the column order is
    deterministic. The generator pattern matters: auditors with a
    wide range can be exporting tens of thousands of rows, and
    buffering the whole list before the response starts would blow
    the worker memory budget.

    `summary` is a one-line truncation of the JSONB payload so the
    CSV is grep-able from a terminal — auditors typically open the
    CSV in less/grep before reaching for the JSON drawer.
    """
    query = _build_scoped_query(
        db,
        tenant_id=tenant_id,
        actions=actions,
        scope_kinds=scope_kinds,
        actor_admin_ids=actor_admin_ids,
        actor_types=actor_types,
        start=start,
        end=end,
        q=q,
    ).order_by(desc(SystemOperationLog.created_at))

    # `yield_per` streams from the DB cursor in batches instead of
    # materialising the whole result set. Tied to the export module's
    # 200-row CSV chunk size so memory stays flat regardless of
    # filter selectivity.
    for row in query.yield_per(200):
        yield _flatten_for_csv(row)


# CSV column order. Stable for downstream tooling (compliance teams
# pipe these into spreadsheets / SIEMs) — adding a column at the end
# is backwards-compatible, reordering is not.
AUDIT_CSV_HEADERS: List[str] = [
    "id",
    "created_at",
    "actor_admin_id",
    "actor_type",
    "action",
    "scope_kind",
    "scope_id",
    "ip",
    "tenant_id",
    "summary",
]


# Column allowlist for the facets endpoint. Re-exported so the
# endpoint can return field labels without re-importing the private
# map.
def facetable_fields() -> Dict[str, str]:
    """Return the {field: label} map of columns operators can facet
    over. The labels are what the FacetSidebar section header
    renders — translate-friendly without breaking the API contract."""
    return dict(_FACETABLE)


def get_audit_row(
    db: Session,
    tenant_id: Optional[str],
    row_id: str,
) -> Optional[SystemOperationLog]:
    """Fetch one audit row by id, scoped to tenant.

    Surfaces the row drawer's "full payload + before/after diff"
    view. Returning the ORM model so the endpoint can decide whether
    to serialise via the response Pydantic or expose richer fields.
    """
    q = db.query(SystemOperationLog).filter(SystemOperationLog.id == row_id)
    if tenant_id is not None:
        q = q.filter(SystemOperationLog.tenant_id == tenant_id)
    return q.first()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_scoped_query(
    db: Session,
    *,
    tenant_id: Optional[str],
    actions: Optional[List[str]],
    scope_kinds: Optional[List[str]],
    actor_admin_ids: Optional[List[str]],
    actor_types: Optional[List[str]],
    start: Optional[datetime],
    end: Optional[datetime],
    q: Optional[str],
) -> Query:
    """Build the WHERE-only query shared by list / facets / export.

    Centralising the WHERE construction is the contract that
    guarantees facet counts match the table results — every caller
    feeds the same filter envelope into the same clauses. Don't
    inline these filters in the public functions; the duplication is
    a footgun audit-log viewers have a long history of stepping on.
    """
    query = db.query(SystemOperationLog)
    if tenant_id is not None:
        query = query.filter(SystemOperationLog.tenant_id == tenant_id)

    # Time bounds. start is INCLUSIVE, end is EXCLUSIVE so adjacent
    # window queries don't double-count rows on the boundary. Same
    # convention proxy_logs uses.
    if start is not None:
        query = query.filter(SystemOperationLog.created_at >= start)
    if end is not None:
        query = query.filter(SystemOperationLog.created_at < end)

    if actions:
        query = query.filter(SystemOperationLog.action.in_(actions))
    if scope_kinds:
        query = query.filter(SystemOperationLog.scope_kind.in_(scope_kinds))
    if actor_admin_ids:
        query = query.filter(SystemOperationLog.actor_admin_id.in_(actor_admin_ids))
    if actor_types:
        query = query.filter(SystemOperationLog.actor_type.in_(actor_types))

    # Free-text search. Cast the JSONB columns to text and ILIKE
    # against the surrounding string. The cast is necessary because
    # SQLAlchemy doesn't expose a portable JSONB::text operator, but
    # both Postgres and SQLite accept a literal cast to `String`. We
    # cast *every* JSON-shaped column so an operator searching for a
    # value that appeared in `before_state` finds it without having
    # to know which column it was stored in.
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                cast(SystemOperationLog.payload, SAString).ilike(like),
                cast(SystemOperationLog.before_state, SAString).ilike(like),
                cast(SystemOperationLog.after_state, SAString).ilike(like),
                # scope_id is a String already; include it so a
                # search for a known group_id / user_id surfaces
                # actions taken against it.
                SystemOperationLog.scope_id.ilike(like),
            )
        )

    return query


def _flatten_for_csv(row: SystemOperationLog) -> Dict[str, Any]:
    """One audit row → one CSV-shaped dict. The `summary` column is
    a JSON-stringified, length-capped view of the payload so the row
    fits a reasonable spreadsheet column without blowing up
    long-payload rows (a cache_flush with N keys can be kilobytes).
    """
    payload_summary = ""
    if row.payload is not None:
        try:
            import json

            payload_summary = json.dumps(row.payload, default=str, separators=(",", ":"))
        except Exception:
            # Defensive: the payload column is JSONB so this should
            # always serialise, but a row written by an out-of-band
            # tool might carry an unexpected type. Don't crash the
            # whole stream — emit a marker the auditor can grep on.
            payload_summary = "<unserialisable>"
        # 4kb cap. Anything larger is exceptional; auditors who care
        # open the full row in the drawer.
        if len(payload_summary) > 4096:
            payload_summary = payload_summary[:4093] + "..."

    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "actor_admin_id": row.actor_admin_id,
        "actor_type": row.actor_type,
        "action": row.action,
        "scope_kind": row.scope_kind or "",
        "scope_id": row.scope_id or "",
        "ip": str(row.ip) if row.ip else "",
        "tenant_id": row.tenant_id or "",
        "summary": payload_summary,
    }
