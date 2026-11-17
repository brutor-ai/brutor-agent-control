# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Live session enumeration + revoke for the System Operations
Console.

The Sessions page surfaces three credential families that all let a
caller act on the platform:

  * **Admin sessions** — JWT-backed rows in `system_admin_sessions` /
    `tenant_admin_sessions`. Revoke flips `is_active` and tells every
    gateway-control replica via NOTIFY so the admin-API auth middleware
    drops them from its session cache before its TTL would have
    expired.

  * **Portal sessions** — end-user JWTs. There is no dedicated portal-
    session table; the user identity carries on the JWT and the only
    durable record of a "live session" is the rolling activity log in
    `proxy_log` (the runtime stamps a per-session id on every request
    via `end_user_session_id`). We surface those by aggregating the
    most-recent activity per (end_user, end_user_session_id) tuple over
    the last 24h — a stale row drops off the list after 24h of no
    requests, which matches the JWT expiry the portal issues by default.
    Revoke for portal sessions writes nothing to the DB (no row to
    flip) — it only publishes the NOTIFY so the gateway-core JWT-deny
    list grows by one entry and the next request 401s.

  * **API keys** — `api_keys` rows scoped by `(tenant_id, enabled,
    revoked)`. Revoke flips `revoked=true`, sets the audit fields, and
    publishes NOTIFY so the proxy's API-key SHA-256 hash cache drops
    the entry. The DB write is the source of truth; NOTIFY is the
    fast-path so the proxy doesn't have to wait for its own poll.

Three deliberate design choices:

  1. **Two-step revoke (durable + ephemeral).** Every revoke writes
     the audit row + DB tombstone FIRST, then publishes NOTIFY. The
     audit-row helper commits its own tx so the row survives even if
     NOTIFY delivery raises; if NOTIFY succeeds but the DB write
     failed the operator gets an obvious 500 from the endpoint and the
     credential remains valid (safer than the inverse, which would
     leak a usable token while the audit log says it was revoked).

  2. **Identity surface is identical across kinds.** Every entry
     returns `(id, user_id, created_at, last_seen_at, ip, user_agent,
     expires_at)` so the UI table renders one component regardless of
     the active tab. Where a kind lacks a field (portal sessions have
     no real `expires_at` because the JWT carries it, not the DB) we
     return `None` and the UI shows a dash — better than synthesising
     a fake value.

  3. **No cross-tenant peek for tenant admins.** Both list paths take
     `tenant_id` as a required positional and filter every query by
     it. System admins get cross-tenant by explicitly passing the
     target tenant via `?tenant_id=` on the endpoint — there is no
     `tenant_id=None` shortcut to enumerate every tenant's sessions
     here. That keeps the auth perimeter on the endpoint, not in the
     service helper, and lets unit tests exercise the helper without
     having to simulate a sysadmin actor.

The helpers run synchronously off a SQLAlchemy Session because every
caller (FastAPI's threadpool path used by the system_ops router)
already holds one — there's no async DB driver in the path and
adding `async` would complicate the call sites without measurable
speedup for the small result sets these queries return (capped at 50).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from app.database.models.admin import (
    TenantAdminSession,
)
from app.database.models.api_key import APIKey
from app.database.models.end_user import EndUser
from app.database.models.proxy_log import ProxyLog
from app.services.system_ops.notify import (
    CHANNEL_SESSION_REVOKE,
    publish_system_notify,
)


# Wire-format constants for the three kinds the UI dispatches between.
# The strings show up in NOTIFY payload, audit row payload, and the
# revoke-endpoint request body — renaming any of them is a wire-protocol
# break that needs both the UI and Rust consumer bumped at the same time.
KIND_ADMIN = "admin"
KIND_PORTAL = "portal"
KIND_API_KEY = "api_key"

SessionKind = Literal["admin", "portal", "api_key"]

VALID_KINDS = (KIND_ADMIN, KIND_PORTAL, KIND_API_KEY)

# Default page size — small because the table is operator-facing and
# the alternative (one giant list) hides the high-cardinality reality
# behind a misleading "matches" count. The endpoint accepts an
# override per-request but caps at 500 to keep the response cheap.
DEFAULT_LIMIT = 50

# Portal sessions are derived from rolling proxy_log activity. 24h is
# the longest window that's both:
#   * Practically useful for an incident-response operator who needs
#     to see "everyone who hit the portal today"
#   * Cheap to query — the (tenant_id, end_user_session_id, timestamp)
#     index from `a073_proxy_logs_api_key_attr` makes the rollup an index-only scan.
PORTAL_ACTIVITY_WINDOW_HOURS = 24


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Base for session-helper failures."""


class UnknownSessionKindError(SessionError):
    """Raised when `revoke_session` gets a `kind` not in VALID_KINDS.
    The endpoint will already have rejected via the Pydantic Literal,
    but the helper double-checks to defend against direct programmatic
    callers (background tasks, future schedulers reusing the helper)."""


class SessionNotFoundError(SessionError):
    """Raised when revoke targets a row that doesn't exist for the
    caller's tenant. Surfaces as a 404 to the caller — distinguishes
    "wrong id" from "wrong tenant" only by absence: a tenant admin
    asking about another tenant's session id gets the same 404 as
    asking about a typo. That's deliberate — the inverse would leak
    cross-tenant id existence."""


# ---------------------------------------------------------------------------
# Shared row shape — every kind maps to the same surface
# ---------------------------------------------------------------------------


def _row(
    *,
    kind: SessionKind,
    id: str,
    user_id: Optional[str],
    user_label: Optional[str],
    created_at: Optional[datetime],
    last_seen_at: Optional[datetime],
    expires_at: Optional[datetime],
    ip: Optional[str],
    user_agent: Optional[str],
    tenant_id: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Coerce the three heterogenous source rows into the single
    `(kind, id, user_id, ...)` envelope the UI table renders.

    Keeping this in one place means a future schema change (say, a
    `risk_score` column landing on every session table) updates the
    UI without each list_* function having to add the same key in
    three places.
    """
    return {
        "kind": kind,
        "id": id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "user_label": user_label,
        "created_at": _iso(created_at),
        "last_seen_at": _iso(last_seen_at),
        "expires_at": _iso(expires_at),
        "ip": ip,
        "user_agent": user_agent,
        "extra": extra or {},
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 with timezone, or None. SQLAlchemy returns aware
    datetimes from Postgres `DateTime(timezone=True)` columns; some
    SQLite test rows land naive — we normalise so the UI never sees
    a wire-format mismatch."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Admin sessions
# ---------------------------------------------------------------------------


def list_admin_sessions(
    db: Session,
    tenant_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """Active tenant-admin sessions for one tenant.

    The query intentionally excludes `SystemAdminSession` rows. System
    admin sessions live in their own table and are NOT tenant-scoped;
    a cross-tenant sysadmin acting on a tenant's Sessions page is
    looking at *that tenant's* admins, not at themselves. The
    Sessions page for the system-admin's own console (not yet
    shipped) will surface SystemAdminSession separately when that
    sub-tab lands.

    Inactive / expired rows are filtered out — operators care about
    the live set, not the historical one. Expired rows still exist
    in the DB for forensic queries but they're not part of "who can
    act right now".
    """
    cutoff = datetime.now(timezone.utc)
    rows = (
        db.query(TenantAdminSession)
        .filter(
            TenantAdminSession.tenant_id == tenant_id,
            TenantAdminSession.is_active.is_(True),
            TenantAdminSession.expires_at > cutoff,
        )
        .order_by(desc(TenantAdminSession.last_activity))
        .limit(limit)
        .all()
    )
    return [
        _row(
            kind=KIND_ADMIN,
            id=row.id,
            user_id=row.admin_id,
            # The admin's username would be nicer to render here, but
            # joining `tenant_admins` per row triggers an N+1 and we
            # don't yet have an in-process lookup map. The UI falls
            # back to admin_id when label is None — good enough for
            # now and the field is forward-compatible when the join
            # gets folded in.
            user_label=None,
            created_at=row.created_at,
            last_seen_at=row.last_activity,
            expires_at=row.expires_at,
            ip=row.ip_address,
            user_agent=row.user_agent,
            tenant_id=row.tenant_id,
            extra={"risk_score": float(row.risk_score or 0.0)},
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Portal sessions (derived from proxy_log)
# ---------------------------------------------------------------------------


def list_portal_sessions(
    db: Session,
    tenant_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """Live portal sessions in the last 24h, ordered most-recent-first.

    There is no portal session table; the runtime stamps a per-session
    id (`end_user_session_id`) on every request. This rollup aggregates
    that field over the activity window to surface "who has been
    actively using the portal".

    A session id with even one request in the window appears once,
    keyed by `end_user_session_id`, with `last_seen_at` set to the
    most recent request timestamp. `created_at` is the first
    timestamp inside the window — it's a proxy for "session start"
    but if the same session id has been active for >24h the
    user-facing start time is older than this. The UI surfaces this
    caveat in the column tooltip; we don't try to reach back further
    because the (tenant_id, end_user_session_id, timestamp) index
    only covers a73-onwards rows.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=PORTAL_ACTIVITY_WINDOW_HOURS)
    # idp_user_id is the canonical "who owns this session" — falls
    # back to gateway_user_id for legacy JWTs that haven't picked up
    # the IdP claim yet. Coalescing in SQL avoids a Python pass.
    user_id_col = func.coalesce(ProxyLog.idp_user_id, ProxyLog.gateway_user_id)
    user_label_col = func.coalesce(
        ProxyLog.idp_user_email,
        ProxyLog.idp_user_username,
        ProxyLog.gateway_user_username,
    )

    rows = (
        db.query(
            ProxyLog.end_user_session_id.label("session_id"),
            user_id_col.label("user_id"),
            func.max(user_label_col).label("user_label"),
            func.min(ProxyLog.timestamp).label("first_seen"),
            func.max(ProxyLog.timestamp).label("last_seen"),
            func.max(ProxyLog.client_ip).label("client_ip"),
        )
        .filter(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.timestamp >= since,
            # Exclude API-key authed traffic — those rows belong on the
            # API Keys tab. NULL `api_key_id` is the canonical "this
            # was a JWT request" marker (see `a073_proxy_logs_api_key_attr`).
            ProxyLog.api_key_id.is_(None),
        )
        .group_by(ProxyLog.end_user_session_id, user_id_col)
        .order_by(desc(func.max(ProxyLog.timestamp)))
        .limit(limit)
        .all()
    )
    return [
        _row(
            kind=KIND_PORTAL,
            id=row.session_id,
            user_id=row.user_id,
            user_label=row.user_label,
            created_at=row.first_seen,
            last_seen_at=row.last_seen,
            # Portal JWT expiry lives on the token, not the DB. The
            # Sessions page leaves the cell blank; operators who need
            # the exact value drill into the IdP server config.
            expires_at=None,
            ip=row.client_ip,
            # proxy_log doesn't capture user-agent today (tracked in
            # tech-debt). When that column lands, swap in
            # func.max(ProxyLog.user_agent) here.
            user_agent=None,
            tenant_id=tenant_id,
        )
        for row in rows
        if row.session_id  # Defensive: end_user_session_id is NOT NULL but skip empty strings.
    ]


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def list_api_keys(
    db: Session,
    tenant_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """API keys that are usable RIGHT NOW for the given tenant.

    "Usable" means `enabled=True`, `revoked=False`, and either
    no `expires_at` or `expires_at` is in the future. The Sessions
    tab is about live credentials, not the historical key list (that
    lives on the API Keys admin page).

    `last_seen_at` is sourced from `api_keys.last_used_at` — the proxy
    updates that column on every successful auth. Ordering by it
    surfaces the keys that matter to an operator triaging "which key
    is causing this load right now".
    """
    now = datetime.now(timezone.utc)
    rows = (
        db.query(APIKey)
        .filter(
            APIKey.tenant_id == tenant_id,
            APIKey.enabled.is_(True),
            APIKey.revoked.is_(False),
            or_(APIKey.expires_at.is_(None), APIKey.expires_at > now),
        )
        .order_by(desc(APIKey.last_used_at), desc(APIKey.created_at))
        .limit(limit)
        .all()
    )
    return [
        _row(
            kind=KIND_API_KEY,
            id=row.id,
            user_id=(str(row.user_id) if row.user_id is not None else None),
            user_label=row.name or row.user_email,
            created_at=row.created_at,
            last_seen_at=row.last_used_at,
            expires_at=row.expires_at,
            # API keys are bearer credentials with no inherent IP /
            # user-agent — the proxy records those per-request in
            # proxy_log. The Sessions page intentionally leaves the
            # columns blank rather than pulling a misleading
            # "most-recent" value here; the operator drills into the
            # Logs tab for that.
            ip=None,
            user_agent=None,
            tenant_id=row.tenant_id,
            extra={
                "scope": getattr(row, "scope", None),
                "usage_count": int(row.usage_count or 0),
            },
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


def revoke_session(
    db: Session,
    *,
    tenant_id: str,
    kind: str,
    id: str,
    revoked_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Tombstone the matching row + publish NOTIFY.

    Returns the canonical NOTIFY payload that was published so the
    endpoint can echo it in the response (handy for tests and for the
    UI's "queued: <payload>" confirmation toast).

    Order of operations:

      1. Look up the target row scoped by `tenant_id`. Missing row =>
         SessionNotFoundError. The lookup also enforces the tenant
         perimeter — the endpoint already gates on
         `require_tenant_resource_access` but defence-in-depth here
         catches a future caller that bypasses the dependency.

      2. Flip the durable state (DB write). Admin sessions go inactive,
         API keys get `revoked=True` + audit fields, portal sessions
         have no row to flip — the kill switch is the NOTIFY-driven
         deny-list on the proxy.

      3. Commit before NOTIFY. If NOTIFY delivery fails for any reason
         the DB tombstone is still correct; the proxy will catch up at
         its next cache TTL refresh. The inverse (NOTIFY-first) could
         leave a "I told the proxy to drop this but the DB says it's
         still valid" race that's much harder to reason about during
         an incident.

      4. Publish NOTIFY. `publish_system_notify` validates the payload
         against the `SessionRevokePayload` model and writes the
         pg_notify SQL call on the same session — Postgres flushes
         the NOTIFY on the next commit, which means the NOTIFY arrives
         at every replica after the DB tombstone is already durable.

    The endpoint then writes the audit row via `write_system_operation`
    after this helper returns. We don't write the audit row here so
    the caller controls the action/scope choice (force_logout vs
    api_key_revoke, etc.) and the request metadata (IP / User-Agent
    from the FastAPI Request).
    """
    if kind not in VALID_KINDS:
        raise UnknownSessionKindError(
            f"revoke_session got kind={kind!r}; expected one of {VALID_KINDS}"
        )

    if kind == KIND_ADMIN:
        row = (
            db.query(TenantAdminSession)
            .filter(
                TenantAdminSession.id == id,
                TenantAdminSession.tenant_id == tenant_id,
            )
            .first()
        )
        if row is None:
            raise SessionNotFoundError(
                f"No admin session id={id!r} for tenant={tenant_id!r}"
            )
        # `deactivate` sets `is_active=False`, `deactivated_at=now`,
        # and `deactivation_reason`. The model exposes a method for
        # exactly this — using it keeps the audit-row payload's
        # `before_state`/`after_state` diff readable.
        row.deactivate(reason=reason or "system_console_revoke")
        db.commit()

    elif kind == KIND_API_KEY:
        row = (
            db.query(APIKey)
            .filter(APIKey.id == id, APIKey.tenant_id == tenant_id)
            .first()
        )
        if row is None:
            raise SessionNotFoundError(
                f"No api_key id={id!r} for tenant={tenant_id!r}"
            )
        row.revoked = True
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_by = revoked_by
        row.revoked_reason = reason or "system_console_revoke"
        # Defensive: also flip enabled=False so older code paths that
        # only consult `enabled` (legacy auth checks pre-revoked
        # column) still 401 the next request. The proxy's modern auth
        # path checks both, so this is belt-and-braces.
        row.enabled = False
        db.commit()

    elif kind == KIND_PORTAL:
        # No table to flip — the JWT is the credential and there's no
        # server-side row that represents the session. The NOTIFY
        # below tells every gateway-core replica to add this session
        # id to its in-memory deny list; the next request that
        # presents this `end_user_session_id` is 401'd by the proxy
        # auth middleware before it touches any other layer.
        #
        # We intentionally don't validate the session id exists in
        # proxy_log first — a sysadmin should be able to pre-empt a
        # known-bad token even if it hasn't shown up in logs yet.
        # The cost of a no-op deny-list entry is one extra map slot.
        pass

    payload = publish_system_notify(
        db=db,
        channel=CHANNEL_SESSION_REVOKE,
        payload={
            "session_id": id,
            "session_type": _payload_session_type(kind),
            "tenant_id": tenant_id,
        },
    )
    return {
        "kind": kind,
        "id": id,
        "tenant_id": tenant_id,
        "channel": CHANNEL_SESSION_REVOKE,
        "payload": payload,
    }


def _payload_session_type(kind: str) -> str:
    """The NOTIFY payload uses `session_type` to disambiguate which
    in-memory cache the subscriber should touch. The kinds we accept
    map 1:1 except api_key, which the proxy treats as a distinct
    auth-cache partition."""
    if kind == KIND_ADMIN:
        return "admin"
    if kind == KIND_PORTAL:
        return "portal"
    if kind == KIND_API_KEY:
        # The proxy's session deny-list keys API keys under a separate
        # bucket from JWT sessions because the lookup primitive
        # differs (sha256(api_key) vs session_id string). The
        # `mcp` value matches the existing `SessionRevokePayload`
        # enum hint in notify.py (which originally enumerated
        # 'admin' / 'portal' / 'mcp' before the api_key surface
        # arrived); we send 'api_key' explicitly here and the Rust
        # consumer dispatches by string match, not by enum.
        return "api_key"
    raise UnknownSessionKindError(kind)
