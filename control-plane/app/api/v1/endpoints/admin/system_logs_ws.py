# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""WebSocket log-tail endpoint for the System Operations Console.

Why a WebSocket and not Server-Sent Events:
    The Logs page wants bidirectional: clients send "pause", "resume",
    or "set filter" mid-stream so we don't have to drop the
    connection to retune. SSE would force a reconnect per filter
    change; that's ugly during an incident where ops is rapidly
    narrowing down a noisy module.

Why a per-replica ring buffer (not a cluster broadcast yet):
    Multi-replica tail demands a side-channel — Postgres LISTEN on a
    log-broadcast channel, Kafka, or a Loki/Grafana relay. Each adds
    a non-trivial DB-write rate (every log line × N consumers) that
    isn't justified at our current operator load. The tail therefore
    only tails the LOCAL replica's logger. Behind a load balancer
    operators will hit a different replica each tab open — that's
    documented as a known limitation, with a routing breadcrumb
    ('X-Replica-Id') exposed so on-call can keep refreshing until
    they land where the noisy request was served.

    A cluster-wide tail would publish each log line to a
    `brutor:log_broadcast` channel and have the WS handler LISTEN. It
    is deliberately not built: it is an architectural decision of its
    own.

Auth flow:
    Browsers can't send the `Authorization` header on a WebSocket
    upgrade, so what opens the stream is in its address, and an address
    is written down (a proxy's access log, the browser's own line when
    the connection fails). It is therefore never the session token. The
    console first asks `POST /logs/tail/ticket`, authenticated like any
    other request and holding `system:read`, for a ticket: random, good
    for thirty seconds and for one connection, bound to the
    administrator, its tenant and this stream, stored as its SHA-256.
    The upgrade presents `?ticket=…`; presenting it consumes it.

    The connection is then held to every check the session token was
    held to: the session the ticket was minted in is still open, the
    administrator is still active, and still holds `system:read`. That
    is checked ONCE at connect. We don't re-verify mid-stream — a
    long-running revoked session keeps streaming until the operator
    closes it, which is acceptable because the data being streamed
    is "logs the operator already has read access to" and the cost
    of re-verifying every line is unacceptable.

Backpressure & disconnects:
    Each connection holds its own queue. When the queue exceeds
    `MAX_QUEUE_LINES`, we drop the oldest and increment a dropped-
    line counter that the client can render as a "tail behind" badge.
    Slow consumers don't block the buffer for others.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import socket
import time
from datetime import timedelta
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.core.admin_user.admin_principal import AdminPrincipal
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import get_auth_service, require_permission, require_tenant_resource_access
from app.database import SessionLocal, get_db
from app.database.models.admin_stream_ticket import (
    STREAM_SYSTEM_LOGS_TAIL,
    TICKET_LIFETIME_SECONDS,
    AdminStreamTicket,
)
from app.env_vars import JWT_SECRET


# ---------------------------------------------------------------------------
# Ring buffer configuration
# ---------------------------------------------------------------------------
# Keep the last 2000 lines hot in memory. At ~200 bytes/line average
# that's ~400 KB resident — small enough not to matter, big enough
# that a freshly-connected client sees a useful pre-roll without
# replaying days of history. The fixed cap also bounds memory under
# a log storm; we'd rather drop the oldest line than OOM the proxy.

RING_BUFFER_SIZE = 2000

# How many lines we replay to a newly-connected client. Smaller than
# the ring so the initial render isn't a 2000-line slab the user
# scrolls through; large enough to read context around the bottom of
# the stream when they connect mid-incident.
INITIAL_REPLAY_LINES = 200

# Per-connection queue cap. When a slow consumer falls behind, we
# drop the oldest queued line and bump `dropped_lines`. The client
# sees the counter increment and renders a "tail behind" indicator.
MAX_QUEUE_LINES = 1000


# ---------------------------------------------------------------------------
# Ring buffer + handler — instantiated once at module import
# ---------------------------------------------------------------------------


@dataclass
class _LogLine:
    """One line in the ring + on the wire.

    Sealed (not extended) deliberately — the wire shape is a hot path
    for tail performance, and an extra field per line is multiplied
    by every byte through every consumer. Add a sibling type if you
    need richer metadata; don't grow this one.
    """

    ts: str                # ISO-8601 UTC, recorded at handler emit.
    level: str             # lowercase canonical: trace/debug/info/warn/error.
    logger: str            # `record.name` — e.g. "guardrails-service".
    message: str           # `record.getMessage()`.
    seq: int = 0           # Monotonically-increasing seq from this process.

    def to_wire(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "seq": self.seq,
        }


_LEVEL_NAME_BY_INT = {
    logging.CRITICAL: "error",
    logging.ERROR: "error",
    logging.WARNING: "warn",
    logging.INFO: "info",
    logging.DEBUG: "debug",
    logging.NOTSET: "info",
}


class _RingBuffer:
    """Thread-safe-ish ring buffer of the last N log lines.

    The logging library may call our `emit` from arbitrary threads
    (uvicorn worker pools, the gunicorn master, background
    schedulers), so `_lines` is guarded by a single lock. The lock is
    held only for the deque mutation — broadcast lookups copy out the
    subscribers list under the lock and emit outside it.
    """

    def __init__(self) -> None:
        # `deque(maxlen=)` is O(1) at both ends and drops the oldest
        # automatically on overflow — exactly the ring shape we want.
        self._lines: Deque[_LogLine] = deque(maxlen=RING_BUFFER_SIZE)
        self._subscribers: Set["_TailClient"] = set()
        self._lock = asyncio.Lock()
        self._sync_lock = __import__("threading").Lock()
        self._seq: int = 0
        # Each WS handler's event loop is captured at subscribe so
        # `emit()` (called from a non-async logging thread) can schedule
        # the broadcast back into the right loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the main event loop so synchronous emit() calls can
        schedule async broadcasts. Idempotent — first call wins."""
        if self._loop is None:
            self._loop = loop

    def push(self, line: _LogLine) -> None:
        """Append a line to the ring and schedule broadcast.

        Called from the `logging` library's thread context. We hold a
        thread lock during the deque mutation, then schedule the
        async broadcast via `call_soon_threadsafe`. This bridges from
        sync (logging) to async (WebSocket) cleanly.
        """
        with self._sync_lock:
            self._seq += 1
            line.seq = self._seq
            self._lines.append(line)
            # Snapshot under lock; emit outside.
            subs = list(self._subscribers)

        if self._loop is None or self._loop.is_closed():
            return  # No one listening — buffer-only mode is fine.

        for sub in subs:
            # Best-effort hand-off; subscriber implements its own
            # backpressure (drop oldest on queue overflow).
            self._loop.call_soon_threadsafe(sub.enqueue, line)

    def snapshot_tail(self, n: int = INITIAL_REPLAY_LINES) -> List[_LogLine]:
        """Last `n` lines, copied. Used as the initial replay for a
        newly-connected client. Costs one allocation, no locking
        contention with the ring writer because deque slicing is
        O(n)."""
        with self._sync_lock:
            return list(self._lines)[-n:]

    def subscribe(self, client: "_TailClient") -> None:
        with self._sync_lock:
            self._subscribers.add(client)

    def unsubscribe(self, client: "_TailClient") -> None:
        with self._sync_lock:
            self._subscribers.discard(client)


_RING = _RingBuffer()


class _TailHandler(logging.Handler):
    """`logging.Handler` that diverts every record into the ring.

    Attached at app startup via `install_tail_handler()`. We
    intentionally subclass the plain handler (not `QueueHandler` /
    `MemoryHandler`) because we want the per-line WAV-style stream
    behaviour, not batched flush.

    The handler accepts ALL levels (`setLevel(NOTSET)`) and lets the
    individual loggers' `getEffectiveLevel()` filter — that way an
    on-call dialing one module to DEBUG via the log-level endpoint
    sees its lines in the tail without us also having to mutate the
    handler's level.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_int = (record.levelno // 10) * 10  # collapse to known rungs
            level = _LEVEL_NAME_BY_INT.get(level_int, "info")
            msg = record.getMessage()
            # Stringify in the formatter sense ourselves so the wire
            # shape is JSON-safe regardless of how the original log
            # call formed `args`. `getMessage()` already does %-style
            # interpolation; we don't add traceback dumping here —
            # `exc_info` would balloon the line, and the operator
            # already has full DB-backed proxy_logs for forensics.
            line = _LogLine(
                ts=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                level=level,
                logger=record.name,
                message=msg,
            )
            _RING.push(line)
        except Exception:  # noqa: BLE001 — handler must never throw
            # A failure here would cascade into the logger which
            # called us, which would call us again. Swallow with
            # `handleError` so the standard handler path takes over.
            self.handleError(record)


def replica_id() -> str:
    """Name of this replica in the live system log: its host name (a
    container's is its id, a pod's its name)."""
    return socket.gethostname()


def install_tail_handler() -> None:
    """Attach the ring-buffer handler to the root logger.

    Idempotent — repeated calls (e.g. test fixtures re-running app
    startup) won't stack handlers. Call exactly once at app boot,
    before the first record gets emitted that you want in the tail.
    """
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _TailHandler):
            return
    root.addHandler(_TailHandler())


# ---------------------------------------------------------------------------
# Per-connection state
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _TailClient:
    """One live WebSocket reader. Compared and hashed by identity: the
    ring keeps its readers in a set."""

    ws: WebSocket
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE_LINES))
    dropped_lines: int = 0
    paused: bool = False
    level_filter: Optional[str] = None
    module_filter: Optional[str] = None

    def enqueue(self, line: _LogLine) -> None:
        """Push a line into the per-client queue. On overflow, drop
        the oldest and bump the dropped-line counter so the client
        can render a 'tail behind' indicator. Filtering happens here
        so unfiltered-out lines don't even occupy queue slots."""
        if self.paused:
            return
        if self.level_filter and line.level != self.level_filter:
            return
        if self.module_filter and self.module_filter not in line.logger:
            return
        try:
            self.queue.put_nowait(line)
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
                self.dropped_lines += 1
                self.queue.put_nowait(line)
            except Exception:
                # Queue raced; drop this line silently. Better than a
                # raise that escapes into a logging path.
                self.dropped_lines += 1


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _get_auth_service_lazy() -> UnifiedAuthService:
    """Local copy of the dependencies.py singleton accessor.

    The dependency function isn't directly callable in a WebSocket
    handler because FastAPI's WS path doesn't run the `Depends` graph
    the same way. We could replicate it via `Depends(get_auth_service)`
    on the WS function signature, but that ties our hands on
    test-time mocking. Keep the explicit accessor + a known-good
    secret env var.
    """
    jwt_secret = JWT_SECRET.exact()
    if not jwt_secret:
        raise RuntimeError(
            "JWT_SECRET env var missing — required by the log-tail WS auth path."
        )
    from app.core.jwt_settings import jwt_algorithm

    return UnifiedAuthService(jwt_secret=jwt_secret, jwt_algorithm=jwt_algorithm())


class StreamTicketResponse(BaseModel):
    """A ticket that opens the log tail once."""

    ticket: str = Field(
        ...,
        description=(
            "Present it as `?ticket=` when opening `/v1/admin/system/logs/tail`. It opens that "
            "stream once, for the administrator who asked, and nothing else."
        ),
    )
    expires_in: int = Field(..., description="Seconds the ticket may wait to be presented: 30.")


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def mint_stream_ticket(
    db: Session, *, stream: str, admin: Any, admin_type: Any, tenant_id: Optional[str], claims: Dict[str, Any]
) -> str:
    """A new ticket for `stream`, bound to the administrator, its tenant
    and the session `claims` are of. Tickets nobody presented in time are
    removed on the way."""
    now = datetime.now(timezone.utc)
    db.query(AdminStreamTicket).filter(AdminStreamTicket.expires_at < now).delete(synchronize_session=False)
    ticket = secrets.token_urlsafe(32)
    db.add(
        AdminStreamTicket(
            ticket_hash=_ticket_hash(ticket),
            stream=stream,
            admin_id=str(admin.id),
            admin_type=getattr(admin_type, "value", str(admin_type)),
            tenant_id=tenant_id,
            session_claims=claims,
            expires_at=now + timedelta(seconds=TICKET_LIFETIME_SECONDS),
        )
    )
    db.flush()
    return ticket


def consume_stream_ticket(db: Session, *, stream: str, ticket: str) -> Optional[AdminStreamTicket]:
    """The ticket's row when `ticket` opens `stream` now, else None. The
    row is deleted either way: a ticket is presented once. Of two
    connections presenting the same ticket, one deletes the row and the
    other is refused."""
    if not ticket:
        return None
    row = db.get(AdminStreamTicket, _ticket_hash(ticket))
    if row is None:
        return None
    db.expunge(row)
    deleted = (
        db.query(AdminStreamTicket)
        .filter(AdminStreamTicket.ticket_hash == row.ticket_hash)
        .delete(synchronize_session=False)
    )
    db.commit()
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if deleted != 1 or row.stream != stream or expires_at <= datetime.now(timezone.utc):
        return None
    return row


def create_system_logs_ws_router(logger: BrutorLogger) -> APIRouter:
    """Factory for the log-tail WebSocket router.

    Mounted under `/v1/admin/system` to keep the URL stable with the
    REST sibling endpoints. The WS path is `/logs/tail`.
    """
    router = APIRouter(prefix="/admin/system", tags=["System Logs WS"])

    @router.post(
        "/logs/tail/ticket",
        response_model=StreamTicketResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Mint a ticket that opens the log tail",
        description=(
            "A ticket for one connection to the log-tail WebSocket "
            "(`/v1/admin/system/logs/tail?ticket=…`). It is random, may wait 30 seconds to be "
            "presented, is consumed by the first connection that presents it, and is bound to "
            "the calling administrator, its tenant (for a system administrator the tenant it "
            "names, as on the other system routes) and that stream. The session token is never "
            "put in the address of the WebSocket. Changes no configuration."
        ),
    )
    def mint_tail_ticket(
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)),
        _perm: AdminPrincipal = Depends(require_permission(Permission.SYSTEM_READ)),
        auth_service: UnifiedAuthService = Depends(get_auth_service),
        db: Session = Depends(get_db),
    ) -> StreamTicketResponse:
        # The tenant is decided as on the other routes of the system pages:
        # a tenant administrator's own, the one a system administrator names.
        admin, tenant_id, admin_type = admin_and_tenant
        header = request.headers.get("authorization", "")
        claims = auth_service.token_claims(header.partition(" ")[2].strip())
        if claims is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        ticket = mint_stream_ticket(
            db, stream=STREAM_SYSTEM_LOGS_TAIL, admin=admin, admin_type=admin_type, tenant_id=tenant_id,
            claims=claims,
        )
        return StreamTicketResponse(ticket=ticket, expires_in=TICKET_LIFETIME_SECONDS)

    @router.websocket("/logs/tail")
    async def tail(
        websocket: WebSocket,
        ticket: str = Query(
            default="",
            description=(
                "A ticket from `POST /v1/admin/system/logs/tail/ticket`: single-use, "
                "30 seconds. The session token is not accepted here."
            ),
        ),
        level: Optional[str] = Query(
            default=None,
            description=(
                "Optional level filter — only lines at this level pass. "
                "Same canonical levels as the REST API."
            ),
        ),
        module: Optional[str] = Query(
            default=None,
            description="Optional logger-name substring filter (case-sensitive).",
        ),
    ) -> None:
        # 1. AUTH GATE. Consume the ticket, then hold the connection to
        # what the session token was held to: an open session, an active
        # administrator, the permission.
        # Closing before `accept()` returns a 1008 to the browser.
        #
        # Every close arm logs at WARN with the specific reason — the
        # browser only sees a generic close code, so the gateway-
        # control container log is the only place the operator can
        # see WHY their tail WS just disconnected. Don't silently
        # close any arm. Neither the ticket nor any part of it is logged.
        db: Optional[Session] = None
        try:
            db = SessionLocal()
            presented = consume_stream_ticket(db, stream=STREAM_SYSTEM_LOGS_TAIL, ticket=ticket)
            if presented is None:
                logger.warning("log-tail WS rejected: no ticket, or one that is spent, expired or for another stream")
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="invalid or expired ticket",
                )
                return
            auth_svc = _get_auth_service_lazy()
            verified = auth_svc.verify_claims(dict(presented.session_claims or {}), db)
            if verified is None:
                logger.warning(
                    "log-tail WS rejected: the session the ticket was minted in is no longer open",
                    context={"admin_id": presented.admin_id},
                )
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="session ended",
                )
                return
            admin, _session, _admin_type = verified

            # The ticket is the administrator's and its tenant's who minted it.
            own_tenant = getattr(admin, "tenant_id", None)
            if str(admin.id) != presented.admin_id or (own_tenant is not None and own_tenant != presented.tenant_id):
                logger.warning(
                    "log-tail WS rejected: the ticket is another administrator's",
                    context={"admin_id": getattr(admin, "id", None), "ticket_admin_id": presented.admin_id},
                )
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="invalid or expired ticket",
                )
                return

            # Permission check. `system:read` is enough — the tail is
            # a read of operational data; mutating log levels is the
            # REST sibling endpoint, which carries its own gate.
            if not admin.has_permission(Permission.SYSTEM_READ.value):
                logger.warning(
                    "log-tail WS rejected: missing system:read",
                    context={
                        "admin_id": getattr(admin, "id", None),
                        "admin_type": str(_admin_type),
                        "tenant_id": getattr(admin, "tenant_id", None),
                    },
                )
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="missing system:read",
                )
                return
        except Exception as e:
            logger.warning(
                "log-tail WS auth path crashed",
                context={"error": str(e), "type": type(e).__name__},
                exc_info=True,
            )
            try:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="auth failure",
                )
            except Exception:
                pass
            return
        finally:
            if db is not None:
                db.close()

        await websocket.accept()

        # 2. ATTACH the loop on first use — the ring writer needs it
        # to schedule async broadcasts from sync logging threads.
        _RING.attach_loop(asyncio.get_running_loop())

        client = _TailClient(
            ws=websocket,
            level_filter=level.lower() if level else None,
            module_filter=module if module else None,
        )

        # 3. REPLAY initial window so the operator sees pre-roll
        # context, not an empty pane.
        for line in _RING.snapshot_tail():
            if client.level_filter and line.level != client.level_filter:
                continue
            if client.module_filter and client.module_filter not in line.logger:
                continue
            try:
                await websocket.send_text(json.dumps(line.to_wire()))
            except Exception:
                # Client disconnected before we finished replaying.
                return

        _RING.subscribe(client)

        # Send a one-shot welcome with replica metadata so the UI can
        # display "you're tailing replica X" — known limitation
        # without cross-replica fanout.
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "replica_id": replica_id(),
                        "ring_size": RING_BUFFER_SIZE,
                        "initial_replay": INITIAL_REPLAY_LINES,
                    }
                )
            )
        except Exception:
            _RING.unsubscribe(client)
            return

        # 4. STREAM loop. Two concurrent tasks:
        #   * inbound — read control messages from the client.
        #   * outbound — drain the queue and write lines.
        # First task that returns (typically the inbound on close)
        # cancels the other.
        async def _inbound() -> None:
            while True:
                msg = await websocket.receive_text()
                try:
                    payload = json.loads(msg)
                except Exception:
                    continue
                op = payload.get("op")
                if op == "pause":
                    client.paused = True
                elif op == "resume":
                    client.paused = False
                elif op == "set_filter":
                    client.level_filter = (payload.get("level") or "").lower() or None
                    client.module_filter = payload.get("module") or None
                # Unknown ops ignored — wire-compatible with future
                # additions.

        async def _outbound() -> None:
            while True:
                line = await client.queue.get()
                wire = line.to_wire()
                if client.dropped_lines:
                    # Surface backlog warning out-of-band so the UI
                    # can render a counter. Reset after sending so
                    # we don't double-count.
                    wire["dropped_since_last"] = client.dropped_lines
                    client.dropped_lines = 0
                await websocket.send_text(json.dumps(wire))

        in_task = asyncio.create_task(_inbound())
        out_task = asyncio.create_task(_outbound())
        try:
            done, pending = await asyncio.wait(
                {in_task, out_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(
                "log-tail WS error",
                context={"error": str(e)},
            )
        finally:
            _RING.unsubscribe(client)
            try:
                await websocket.close()
            except Exception:
                pass

    return router
