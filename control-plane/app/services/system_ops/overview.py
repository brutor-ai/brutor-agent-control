# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Overview — health-grid probe orchestrator

The Overview tab needs at-a-glance status for every dependency Brutor
relies on at runtime: Postgres, Redis, Qdrant, configured LLM
upstreams, and configured MCP servers. The endpoint must respond
within ~2 seconds so an operator pages through tabs without waiting.

Strategy:

  * **Infrastructure probes (DB, Redis, Qdrant)** run live. Each is a
    cheap PING/SELECT-1/readyz check with a 1500ms cap. These three
    run in parallel via `asyncio.gather` so the wall-clock is roughly
    the slowest single probe rather than the sum.

  * **LLM and MCP cards** are computed from the existing
    `system_alerts` table rather than via live HTTP. The Rust proxy's
    `system_alerts` helper already records downstream failures and
    transitions; trying to ping every configured LLM provider from
    the control plane on every overview load would be expensive and
    racy. The signal we render is the one already used by Mission
    Control's alert panel — keeping two surfaces consistent.

  * **Counters** are derived from the card list (single pass).

  * **Maintenance** state is read from the tenants row directly.

Defensive shape: any probe that throws is caught and rendered as
status='unknown' with the exception class in the `detail` line — the
endpoint never 500s on a single dependency failure, since the whole
point of the page is to make those failures visible. The exception
is the database itself: if the SQL session can't open, the request
already failed in the dependency-injector chain before we got here.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.composition.hooks import HOOKS, SYSTEM_OPS_DEPENDENCY_PROBES
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.system_alert import SystemAlert
from app.database.models.tenant import Tenant
from app.env_vars import REDIS_URL


# Per-probe cap. 1500ms leaves ~500ms headroom for serialization +
# downstream Redis/Qdrant round-trips in the synthetic test. The
# admin UI hides the spinner once the response lands; if a probe
# times out the card still renders with status='unknown'.
PROBE_TIMEOUT_SECS = 1.5

# Recent-alert window for LLM / MCP card status. An LLM upstream
# that hasn't failed in the last 5 minutes is considered healthy
# from the Overview point of view even if a stale 'active' alert
# row hasn't been resolved yet — that matches what operators
# actually expect to see ("is it failing RIGHT NOW?").
RECENT_ALERT_WINDOW_MINS = 5


# ---------------------------------------------------------------------------
# Status / kind helpers
# ---------------------------------------------------------------------------


def _severity_to_status(severity: Optional[str]) -> str:
    """Map a system_alerts severity to a HealthCard status token."""
    if severity == "critical":
        return "down"
    if severity == "high":
        return "down"
    if severity == "medium":
        return "degraded"
    if severity == "low":
        return "degraded"
    return "healthy"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Infrastructure probes
# ---------------------------------------------------------------------------


async def _probe_database(db: Session) -> Dict[str, Any]:
    """SELECT 1 against the same session the request runs on.

    No new connection — uses the request-scoped session, so a failed
    probe means the DB the rest of the request relies on is also
    broken. We catch the OperationalError to render a card rather
    than 500, but in practice if the DB is down the dependency
    injector already raised before we got here.
    """
    started = time.monotonic()
    try:
        row = db.execute(text("SELECT 1")).scalar_one()
        if row != 1:
            return _card_unknown(
                "database",
                "database",
                "Postgres",
                "SELECT 1 returned unexpected value",
                target_route="system-connections",
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return dependency_card(
            kind="database",
            card_id="database",
            label="Postgres",
            status="healthy",
            detail="SELECT 1 OK",
            latency_ms=elapsed_ms,
            target_route="system-connections",
        )
    except Exception as exc:  # pragma: no cover - bubbled by caller in prod
        return dependency_card(
            kind="database",
            card_id="database",
            label="Postgres",
            status="down",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=None,
            target_route="system-connections",
        )


async def _probe_redis() -> Optional[Dict[str, Any]]:
    """PING against $REDIS_URL.

    Returns None if Redis isn't configured for this deployment — the
    card list omits it entirely rather than showing 'disabled', since
    Redis is genuinely optional (we use Postgres NOTIFY for
    inter-process messaging).
    """
    redis_url = REDIS_URL.exact()
    if not redis_url:
        return None

    started = time.monotonic()
    try:
        # Import here so the probe is no-op when redis isn't installed
        import redis.asyncio as redis_async  # type: ignore[import-untyped]

        client = redis_async.from_url(
            redis_url, socket_timeout=PROBE_TIMEOUT_SECS
        )
        try:
            pong = await asyncio.wait_for(
                client.ping(), timeout=PROBE_TIMEOUT_SECS
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if pong:
                return dependency_card(
                    kind="redis",
                    card_id="redis",
                    label="Redis",
                    status="healthy",
                    detail="PING OK",
                    latency_ms=elapsed_ms,
                    target_route="system-connections",
                )
            return dependency_card(
                kind="redis",
                card_id="redis",
                label="Redis",
                status="degraded",
                detail="PING returned falsy",
                latency_ms=elapsed_ms,
                target_route="system-connections",
            )
        finally:
            await client.aclose()
    except asyncio.TimeoutError:
        return dependency_card(
            kind="redis",
            card_id="redis",
            label="Redis",
            status="down",
            detail=f"PING timeout after {int(PROBE_TIMEOUT_SECS * 1000)}ms",
            latency_ms=None,
            target_route="system-connections",
        )
    except Exception as exc:
        return dependency_card(
            kind="redis",
            card_id="redis",
            label="Redis",
            status="down",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=None,
            target_route="system-connections",
        )


# ---------------------------------------------------------------------------
# Alert-driven cards (LLM upstreams + MCP servers)
# ---------------------------------------------------------------------------


def _llm_cards(db: Session, tenant_id: str) -> List[Dict[str, Any]]:
    """One card per active LLM model for this tenant.

    Status derives from the latest active or recently-resolved
    `system_alerts` row whose `source` starts with `llm.` and whose
    `resource_id` matches the model id. No live HTTP probe.
    """
    models = (
        db.query(LLModel)
        .filter(LLModel.tenant_id == tenant_id, LLModel.enabled == True)
        .all()
    )
    if not models:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=RECENT_ALERT_WINDOW_MINS
    )

    alert_by_resource: Dict[str, SystemAlert] = {}
    rows = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.tenant_id == tenant_id,
            SystemAlert.resource_type == "llm_model",
            SystemAlert.status.in_(["active", "acknowledged"]),
            SystemAlert.last_seen_at >= cutoff,
        )
        .all()
    )
    for r in rows:
        prev = alert_by_resource.get(r.resource_id or "")
        # Most severe wins (active > acknowledged; critical > high > medium > low)
        if prev is None or _severity_rank(r.severity) > _severity_rank(prev.severity):
            alert_by_resource[r.resource_id or ""] = r

    cards: List[Dict[str, Any]] = []
    for m in models:
        alert = alert_by_resource.get(m.id)
        status = _severity_to_status(alert.severity if alert else None)
        detail = (
            alert.title if alert else "No recent failures"
        )
        cards.append(
            dependency_card(
                kind="llm_upstream",
                card_id=m.id,
                label=m.name or m.model_name or m.id,
                status=status,
                detail=detail,
                latency_ms=None,
                target_route=f"llm-models:{m.id}",
            )
        )
    return cards


def _mcp_cards(db: Session, tenant_id: str) -> List[Dict[str, Any]]:
    """One card per MCP server."""
    servers = (
        db.query(MCPServer)
        .filter(MCPServer.tenant_id == tenant_id)
        .all()
    )
    if not servers:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=RECENT_ALERT_WINDOW_MINS
    )

    alert_by_resource: Dict[str, SystemAlert] = {}
    rows = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.tenant_id == tenant_id,
            SystemAlert.resource_type == "mcp_server",
            SystemAlert.status.in_(["active", "acknowledged"]),
            SystemAlert.last_seen_at >= cutoff,
        )
        .all()
    )
    for r in rows:
        prev = alert_by_resource.get(r.resource_id or "")
        if prev is None or _severity_rank(r.severity) > _severity_rank(prev.severity):
            alert_by_resource[r.resource_id or ""] = r

    cards: List[Dict[str, Any]] = []
    for s in servers:
        # Disabled servers render as 'disabled' regardless of
        # alerts — they're not in the data path right now.
        enabled = getattr(s, "enabled", True)
        if not enabled:
            cards.append(
                dependency_card(
                    kind="mcp_server",
                    card_id=s.id,
                    label=getattr(s, "name", s.id),
                    status="disabled",
                    detail="Server disabled",
                    latency_ms=None,
                    target_route=f"mcp-servers:{s.id}",
                )
            )
            continue

        alert = alert_by_resource.get(s.id)
        status = _severity_to_status(alert.severity if alert else None)
        detail = (
            alert.title if alert else "No recent failures"
        )
        cards.append(
            dependency_card(
                kind="mcp_server",
                card_id=s.id,
                label=getattr(s, "name", s.id),
                status=status,
                detail=detail,
                latency_ms=None,
                target_route=f"mcp-servers:{s.id}",
            )
        )
    return cards


def _severity_rank(severity: Optional[str]) -> int:
    """Numeric ordering for "most severe wins" deduplication."""
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(severity or "", 0)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def _maintenance(db: Session, tenant_id: str) -> Dict[str, Any]:
    """Read maintenance fields straight off the tenants row."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        return {"mode": "off", "since": None, "drain_grace_secs": None}
    mode = getattr(tenant, "maintenance_mode", None) or "off"
    since = getattr(tenant, "maintenance_mode_at", None)
    drain = getattr(tenant, "drain_grace_secs", None)
    return {
        "mode": mode,
        "since": since.isoformat() if since else None,
        "drain_grace_secs": int(drain) if drain is not None else None,
    }


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def _counters(cards: List[Dict[str, Any]]) -> Dict[str, int]:
    counters = {
        "healthy": 0,
        "degraded": 0,
        "down": 0,
        "unknown": 0,
        "disabled": 0,
    }
    for c in cards:
        s = c.get("status")
        if s in counters:
            counters[s] += 1
    return counters


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------


async def _hooked_probe(probe, logger: BrutorLogger) -> Optional[Dict[str, Any]]:
    """A registered dependency probe's card. A probe that raises has no card
    (and is logged): it must not take the page down with it."""
    try:
        return await probe()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dependency probe failed", context={"error": str(exc)})
        return None


async def build_overview(
    db: Session,
    tenant_id: str,
    logger: BrutorLogger,
) -> Dict[str, Any]:
    """Compose the Overview payload.

    The infra probes (DB, Redis and the registered dependency probes) run
    in parallel.
    The alert-driven cards (LLM / MCP) read from the request-scoped
    session synchronously — those don't benefit from concurrency
    (single SQL round-trip already).
    """

    # Pull static (non-probe) data first so it's available even if
    # an infra probe errors hard.
    llm_cards = _llm_cards(db, tenant_id)
    mcp_cards = _mcp_cards(db, tenant_id)
    maintenance = _maintenance(db, tenant_id)

    # Run the live probes in parallel: the database, Redis, and the
    # dependencies an Enterprise Edition feature brings with it (the
    # vector database), which are behaviour hooks (`composition/hooks.py`).
    # A composition without them probes what it uses and nothing else.
    db_card, redis_card, *extra_cards = await asyncio.gather(
        _probe_database(db),
        _probe_redis(),
        *[_hooked_probe(probe, logger) for probe in HOOKS.all(SYSTEM_OPS_DEPENDENCY_PROBES)],
        return_exceptions=False,
    )

    cards: List[Dict[str, Any]] = [db_card]
    if redis_card is not None:
        cards.append(redis_card)
    cards.extend(card for card in extra_cards if card is not None)
    # Sort LLM + MCP alphabetically within their kind so the grid
    # is stable across reloads (avoids cards reshuffling on every
    # poll).
    cards.extend(sorted(llm_cards, key=lambda c: c["label"].lower()))
    cards.extend(sorted(mcp_cards, key=lambda c: c["label"].lower()))

    payload = {
        "tenant_id": tenant_id,
        "generated_at": _now_iso(),
        "counters": _counters(cards),
        "maintenance": maintenance,
        "cards": cards,
    }
    logger.debug(
        "System overview built",
        context={
            "tenant_id": tenant_id,
            "card_count": len(cards),
            "healthy": payload["counters"]["healthy"],
            "down": payload["counters"]["down"],
        },
    )
    return payload


# ---------------------------------------------------------------------------
# Card factories — keep the dict shape in one place
# ---------------------------------------------------------------------------


def dependency_card(
    *,
    kind: str,
    card_id: str,
    label: str,
    status: str,
    detail: Optional[str],
    latency_ms: Optional[int],
    target_route: Optional[str],
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "id": card_id,
        "label": label,
        "status": status,
        "detail": detail,
        "latency_ms": latency_ms,
        "target_route": target_route,
        "last_checked_at": _now_iso(),
    }


def _card_unknown(
    kind: str,
    card_id: str,
    label: str,
    detail: str,
    *,
    target_route: Optional[str] = None,
) -> Dict[str, Any]:
    return dependency_card(
        kind=kind,
        card_id=card_id,
        label=label,
        status="unknown",
        detail=detail,
        latency_ms=None,
        target_route=target_route,
    )
