# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""write_system_operation — the audit-row helper every System endpoint
must call before returning a successful response.

Every system-page click writes one row to `system_operation_log` (see
the System Operations audit list). The helper centralises four concerns:

  1. **Identity propagation.** The actor's id and type (system_admin /
     tenant_admin) come from the auth context, not the request body.
     A misimplemented endpoint that lets the caller forge an
     `actor_admin_id` is a forensic-trail bypass; the helper enforces
     "take from auth, never from body".

  2. **Request metadata capture.** Source IP and User-Agent are
     pulled from the FastAPI Request automatically. Endpoint code
     never has to thread them through.

  3. **Payload sanitisation.** The helper strips known-sensitive
     keys (api_key, password, jwt, …) from the payload before
     persisting. Endpoints can still pass through the rest of the
     body verbatim — sanitisation is one-call from anywhere.

  4. **Consistent ULID identifier.** Prefix `sol-` so audit-log rows
     are distinguishable from proxy_log / admin_audit_log rows in a
     debugger output.

The helper is **synchronous**. System endpoints run on FastAPI's
threadpool path (they hold a SQLAlchemy Session, not an async one),
so async won't help and would complicate the call sites.

The helper **commits its own transaction** so the audit row survives
even if the endpoint's main work errors out and rolls back. This is
the audit-trail-integrity guarantee — an action that errored should
still show up in Audit so an operator sees "someone tried to flush
that cache and it failed". When the endpoint's work succeeds, the two
commits are sequential and the order doesn't matter.

Returning the persisted row lets the endpoint embed the new row id in
its response if it chooses (most don't — the audit row is for the
log, not the response).
"""

import copy
from typing import Any, Dict, Optional, Union

from fastapi import Request
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.composition.principals import PRINCIPALS
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.system_operation_log import (
    VALID_ACTOR_TYPES,
    SystemOperationLog,
)
from app.middleware.trusted_proxies import client_ip

logger = BrutorLogger(__name__)


# The redactor never persists a value under these keys. Matches loose
# (case-insensitive substring) so common variants like `api_key`,
# `apiKey`, `API_KEY` all hit. The redactor walks the entire JSON
# tree and replaces hit values with the literal `"[REDACTED]"` so the
# Audit tab can render the shape without leaking the secret.
SENSITIVE_PAYLOAD_KEYS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "jwt",
    "bearer",
    "authorization",
    "private_key",
    "client_secret",
    "session",  # session_id is captured as scope_id, never in payload
    "refresh_token",
    "access_token",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(needle in lowered for needle in SENSITIVE_PAYLOAD_KEYS)


def redact_payload(payload: Any) -> Any:
    """Walk a JSON-shaped value and replace any value at a sensitive
    key with the literal `"[REDACTED]"`.

    Non-dict / non-list values are returned unchanged. Strings that
    LOOK like an API key (`sk-…`, `ghp_…`, etc.) are NOT detected
    here — that's the secrets-detector's job. This redactor is a
    structural pass against known sensitive keys, not a content
    classifier. We deliberately keep the contract narrow so endpoint
    authors can predict what reaches the row.
    """
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for k, v in payload.items():
            if _is_sensitive_key(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    return payload


def _resolve_actor(
    actor: Optional[AdminPrincipal],
) -> tuple[Optional[str], Optional[str]]:
    """Map the auth-context principal onto (actor_admin_id, actor_type).

    Returns (None, None) when the caller supplied no actor — the
    endpoint helper will reject that, but we don't want the audit
    helper itself to raise mid-write.
    """
    if actor is None:
        return None, None
    # Whose administrator this is, is asked of the principal registry:
    # the actor type is the owning principal's name ("system_admin",
    # "tenant_admin"), and this module names no administrator model.
    principal = PRINCIPALS.owner_of(actor)
    if principal is not None:
        actor_type = f"{principal.name}_admin"
        if actor_type in VALID_ACTOR_TYPES:
            return actor.id, actor_type
    # Unknown principal class — surface loudly. Forensics matter.
    raise TypeError(
        f"write_system_operation called with unknown actor type: "
        f"{type(actor).__name__}. Expected SystemAdmin or TenantAdmin."
    )


def _request_metadata(request: Optional[Request]) -> tuple[Optional[str], Optional[str]]:
    """Pull (ip, user_agent) from a FastAPI Request, defaulting to
    None when the request isn't available (test paths, background
    work, schedulers reusing the helper)."""
    if request is None:
        return None, None
    ip = client_ip(request)
    user_agent = request.headers.get("user-agent")
    return ip, user_agent


def write_system_operation(
    db: Session,
    *,
    actor: AdminPrincipal,
    action: str,
    tenant_id: Optional[str] = None,
    scope_kind: Optional[str] = None,
    scope_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    sanitise: bool = True,
    commit: bool = True,
) -> SystemOperationLog:
    """Persist one audit row for a System Console action.

    Args:
        db: Open SQLAlchemy session. The helper commits its own
            transaction unless ``commit=False`` is passed (used by
            tests that want to assert flush-without-commit shapes).
        actor: The authenticated admin. Determines actor_admin_id +
            actor_type. SystemAdmin or TenantAdmin only.
        action: Action verb. See `SystemOperationLog.ACTION_*`
            constants for the canonical set. Open enum.
        tenant_id: Tenant the action targeted. None = global / cross-
            tenant (system_admin only by convention; the helper
            doesn't enforce — endpoints do).
        scope_kind / scope_id: Optional sub-tenant scope.
        payload: Request body / endpoint args. Will be deep-copied and
            redacted unless ``sanitise=False``.
        before_state / after_state: Runtime state snapshots. Endpoint
            code captures these around the actual mutation.
        request: FastAPI Request for IP + User-Agent. Optional — the
            helper just leaves those columns NULL when absent.
        sanitise: When True (default), redact sensitive payload keys
            before persisting. Always passes through before / after
            state unchanged — those are runtime-state diffs, not
            user-supplied input, and shouldn't contain secrets.
        commit: When True (default), commit the new row. False is for
            tests that want to inspect the session before commit.

    Returns:
        The persisted SystemOperationLog instance.
    """
    actor_admin_id, actor_type = _resolve_actor(actor)
    if actor_admin_id is None or actor_type is None:
        # Defensive: if an endpoint reaches the helper with no actor
        # we'd write a row attributed to nobody. That's worse than
        # raising — fail noisily so the missing auth wiring shows up
        # in the test suite.
        raise ValueError(
            "write_system_operation called without an actor. The "
            "endpoint must resolve a SystemAdmin or TenantAdmin from "
            "the auth context before invoking this helper."
        )

    ip, user_agent = _request_metadata(request)

    persisted_payload: Optional[Dict[str, Any]] = None
    if payload is not None:
        # Deep-copy first so we never mutate the caller's dict, then
        # redact in place.
        copied = copy.deepcopy(payload)
        persisted_payload = redact_payload(copied) if sanitise else copied

    row = SystemOperationLog(
        id=f"sol-{str(ULID())}",
        tenant_id=tenant_id,
        actor_admin_id=actor_admin_id,
        actor_type=actor_type,
        action=action,
        scope_kind=scope_kind,
        scope_id=scope_id,
        payload=persisted_payload,
        before_state=before_state,
        after_state=after_state,
        ip=ip,
        user_agent=user_agent,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()

    # Structured log alongside the row — useful when the operator is
    # tailing logs while clicking around the System page.
    logger.info(
        "system operation",
        context={
            "audit_id": row.id,
            "action": action,
            "actor_admin_id": actor_admin_id,
            "actor_type": actor_type,
            "tenant_id": tenant_id,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
        },
    )
    return row
