# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance notification admin API:
channels (CRUD + test-send), subscriptions (CRUD), delivery history.

Channel configs (webhook URLs, HMAC secrets) are credentials: stored
encrypted with the platform ENCRYPTION_KEY, returned only as masked
summaries, and never round-tripped — an update without a new config
keeps the stored one.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.admin_user.dependencies import (
    resolve_tenant_id,
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.notification_channel import (
    CHANNEL_KINDS,
    DELIVERY_STATUSES,
    EVENT_CLASSES,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from app.services.assurance_notifications import NotificationDeliverer

import json


class ChannelConfigInput(BaseModel):
    """Accepts either kind's fields; validated per kind at the endpoint."""

    webhook_url: Optional[str] = Field(None, description="slack_webhook: incoming webhook URL")
    url: Optional[str] = Field(None, description="webhook: destination URL")
    secret: Optional[str] = Field(None, description="webhook: HMAC-SHA256 secret")


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    kind: str
    config: ChannelConfigInput
    enabled: bool = True


class ChannelUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    config: Optional[ChannelConfigInput] = None
    enabled: Optional[bool] = None


class ChannelResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    kind: str
    enabled: bool
    consecutive_failures: int
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    config_summary: str


class SubscriptionCreateRequest(BaseModel):
    channel_id: str
    event_class: str = "inbox_item_created"
    severity_floor: str = "warning"
    categories: Optional[List[str]] = None
    ai_system_id: Optional[str] = None
    coalesce_seconds: int = Field(900, ge=0, le=86400)
    enabled: bool = True


class SubscriptionUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    event_class: Optional[str] = None
    severity_floor: Optional[str] = None
    categories: Optional[List[str]] = None
    ai_system_id: Optional[str] = None
    coalesce_seconds: Optional[int] = Field(None, ge=0, le=86400)
    enabled: Optional[bool] = None


class SubscriptionResponse(BaseModel):
    id: str
    tenant_id: str
    channel_id: str
    event_class: str
    severity_floor: str
    categories: Optional[List[str]] = None
    ai_system_id: Optional[str] = None
    coalesce_seconds: int
    enabled: bool
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class DeliveryResponse(BaseModel):
    id: str
    subscription_id: str
    channel_id: str
    inbox_item_id: str
    fingerprint: str
    status: str
    attempts: int
    last_error: Optional[str] = None
    sent_at: Optional[str] = None
    created_at: Optional[str] = None


class DeliveryListResponse(BaseModel):
    deliveries: List[DeliveryResponse]
    total: int


class TestSendResponse(BaseModel):
    ok: bool
    error: Optional[str] = None


def _mask_url(url: str) -> str:
    """Show enough to recognize, never enough to use."""
    if not url:
        return ""
    from urllib.parse import urlparse

    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/…"


def create_assurance_notifications_router(
    logger: BrutorLogger, encryption_service
) -> APIRouter:
    """Build the `/admin/notifications/*` router."""
    router = APIRouter(prefix="/admin/notifications", tags=["Assurance Notifications"])
    deliverer = NotificationDeliverer(logger)

    # One tenant-scoping rule for every admin route — see
    # app/core/admin_user/dependencies.py::resolve_tenant_id.
    _tenant_of = resolve_tenant_id

    def _channel_of(db: Session, tenant_id: str, channel_id: str) -> NotificationChannel:
        row = (
            db.query(NotificationChannel)
            .filter(
                NotificationChannel.id == channel_id,
                NotificationChannel.tenant_id == tenant_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        return row

    def _validate_config(kind: str, config: ChannelConfigInput) -> Dict[str, Any]:
        if kind == "slack_webhook":
            if not config.webhook_url or not config.webhook_url.startswith("https://"):
                raise HTTPException(
                    status_code=422,
                    detail="slack_webhook channels need an https webhook_url",
                )
            return {"webhook_url": config.webhook_url}
        if kind == "webhook":
            if not config.url or not config.url.startswith(("https://", "http://")):
                raise HTTPException(
                    status_code=422, detail="webhook channels need a url"
                )
            out: Dict[str, Any] = {"url": config.url}
            if config.secret:
                out["secret"] = config.secret
            return out
        raise HTTPException(status_code=422, detail=f"Unknown channel kind '{kind}'")

    def _channel_response(row: NotificationChannel) -> ChannelResponse:
        try:
            cfg = json.loads(encryption_service.decrypt(row.config_encrypted))
            url = cfg.get("webhook_url") or cfg.get("url") or ""
            summary = _mask_url(url)
            if row.kind == "webhook" and cfg.get("secret"):
                summary += " (signed)"
        except Exception:  # noqa: BLE001 — a bad key must not 500 the list
            summary = "(config unreadable)"
        return ChannelResponse(**row.to_dict(), config_summary=summary)

    # ── Channels ────────────────────────────────────────────────────────

    @router.get("/channels", response_model=List[ChannelResponse],
                summary="List notification channels")
    def list_channels(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.NOTIFICATION_READ)),
    ) -> List[ChannelResponse]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        rows = (
            db.query(NotificationChannel)
            .filter(NotificationChannel.tenant_id == tenant_id)
            .order_by(NotificationChannel.created_at.asc())
            .all()
        )
        return [_channel_response(r) for r in rows]

    @router.post("/channels", response_model=ChannelResponse, status_code=201,
                 summary="Create a notification channel")
    def create_channel(
        body: ChannelCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> ChannelResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        if body.kind not in CHANNEL_KINDS:
            raise HTTPException(status_code=422, detail=f"Unknown kind '{body.kind}'")
        config = _validate_config(body.kind, body.config)
        row = NotificationChannel(
            id=f"nchan-{ULID()}",
            tenant_id=tenant_id,
            name=body.name,
            kind=body.kind,
            config_encrypted=encryption_service.encrypt(json.dumps(config)),
            enabled=body.enabled,
            created_by=admin.username,
        )
        db.add(row)
        audit.log(
            action="notification.channel.create", db=db, admin=admin,
            resource_type="notification_channel", resource_id=row.id,
            tenant_id=tenant_id, details={"kind": body.kind, "name": body.name},
        )
        db.commit()
        return _channel_response(row)

    @router.patch("/channels/{channel_id}", response_model=ChannelResponse,
                  summary="Update a channel")
    def update_channel(
        channel_id: str,
        body: ChannelUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> ChannelResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _channel_of(db, tenant_id, channel_id)
        if body.name is not None:
            row.name = body.name
        if body.enabled is not None:
            row.enabled = body.enabled
            if body.enabled:
                # Re-enabling resets the failure streak: the operator is
                # asserting the destination is fixed.
                row.consecutive_failures = 0
        if body.config is not None:
            config = _validate_config(row.kind, body.config)
            row.config_encrypted = encryption_service.encrypt(json.dumps(config))
            row.consecutive_failures = 0
        audit.log(
            action="notification.channel.update", db=db, admin=admin,
            resource_type="notification_channel", resource_id=row.id,
            tenant_id=tenant_id,
        )
        db.commit()
        return _channel_response(row)

    @router.delete("/channels/{channel_id}", summary="Delete a channel")
    def delete_channel(
        channel_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> Dict[str, bool]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _channel_of(db, tenant_id, channel_id)
        db.delete(row)
        audit.log(
            action="notification.channel.delete", db=db, admin=admin,
            resource_type="notification_channel", resource_id=channel_id,
            tenant_id=tenant_id,
        )
        db.commit()
        return {"deleted": True}

    @router.post("/channels/{channel_id}/test", response_model=TestSendResponse,
                 summary="Send a test message through a channel")
    def test_channel(
        channel_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> TestSendResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = _channel_of(db, tenant_id, channel_id)
        try:
            config = json.loads(encryption_service.decrypt(row.config_encrypted))
        except Exception:  # noqa: BLE001
            return TestSendResponse(ok=False, error="Channel config unreadable")

        from app.database.models.assurance_inbox import AssuranceInboxItem

        sample = AssuranceInboxItem(
            id="inbox-test",
            tenant_id=tenant_id,
            ai_system_id=None,
            category="health_transition",
            severity="info",
            fingerprint="test",
            title="Test message from Brutor — this channel is wired up correctly.",
            evidence={},
            occurrence_count=1,
            state="open",
        )
        ok, error = deliverer.deliver(row.kind, config, sample, "created")
        audit.log(
            action="notification.channel.test", db=db, admin=admin,
            resource_type="notification_channel", resource_id=row.id,
            tenant_id=tenant_id, details={"ok": ok},
        )
        db.commit()
        return TestSendResponse(ok=ok, error=error or None)

    # ── Subscriptions ───────────────────────────────────────────────────

    @router.get("/subscriptions", response_model=List[SubscriptionResponse],
                summary="List alert subscriptions")
    def list_subscriptions(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.NOTIFICATION_READ)),
    ) -> List[SubscriptionResponse]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        rows = (
            db.query(NotificationSubscription)
            .filter(NotificationSubscription.tenant_id == tenant_id)
            .order_by(NotificationSubscription.created_at.asc())
            .all()
        )
        return [SubscriptionResponse(**r.to_dict()) for r in rows]

    @router.post("/subscriptions", response_model=SubscriptionResponse,
                 status_code=201, summary="Create an alert subscription")
    def create_subscription(
        body: SubscriptionCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> SubscriptionResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        _channel_of(db, tenant_id, body.channel_id)  # ownership check
        if body.event_class not in EVENT_CLASSES:
            raise HTTPException(
                status_code=422, detail=f"Unknown event_class '{body.event_class}'"
            )
        if body.severity_floor not in ("info", "warning", "critical"):
            raise HTTPException(
                status_code=422,
                detail=f"Unknown severity_floor '{body.severity_floor}'",
            )
        row = NotificationSubscription(
            id=f"nsub-{ULID()}",
            tenant_id=tenant_id,
            channel_id=body.channel_id,
            event_class=body.event_class,
            severity_floor=body.severity_floor,
            categories=body.categories,
            ai_system_id=body.ai_system_id,
            coalesce_seconds=body.coalesce_seconds,
            enabled=body.enabled,
            created_by=admin.username,
        )
        db.add(row)
        audit.log(
            action="notification.subscription.create", db=db, admin=admin,
            resource_type="notification_subscription", resource_id=row.id,
            tenant_id=tenant_id,
            details={"channel_id": body.channel_id, "event_class": body.event_class},
        )
        db.commit()
        return SubscriptionResponse(**row.to_dict())

    @router.patch("/subscriptions/{subscription_id}",
                  response_model=SubscriptionResponse,
                  summary="Update an alert subscription")
    def update_subscription(
        subscription_id: str,
        body: SubscriptionUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> SubscriptionResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = (
            db.query(NotificationSubscription)
            .filter(
                NotificationSubscription.id == subscription_id,
                NotificationSubscription.tenant_id == tenant_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        if body.event_class is not None:
            if body.event_class not in EVENT_CLASSES:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown event_class '{body.event_class}'",
                )
            row.event_class = body.event_class
        if body.severity_floor is not None:
            if body.severity_floor not in ("info", "warning", "critical"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown severity_floor '{body.severity_floor}'",
                )
            row.severity_floor = body.severity_floor
        if body.categories is not None:
            row.categories = body.categories or None
        if body.ai_system_id is not None:
            row.ai_system_id = body.ai_system_id or None
        if body.coalesce_seconds is not None:
            row.coalesce_seconds = body.coalesce_seconds
        if body.enabled is not None:
            row.enabled = body.enabled
        audit.log(
            action="notification.subscription.update", db=db, admin=admin,
            resource_type="notification_subscription", resource_id=row.id,
            tenant_id=tenant_id,
        )
        db.commit()
        return SubscriptionResponse(**row.to_dict())

    @router.delete("/subscriptions/{subscription_id}",
                   summary="Delete an alert subscription")
    def delete_subscription(
        subscription_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.NOTIFICATION_MANAGE)),
    ) -> Dict[str, bool]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        row = (
            db.query(NotificationSubscription)
            .filter(
                NotificationSubscription.id == subscription_id,
                NotificationSubscription.tenant_id == tenant_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        db.delete(row)
        audit.log(
            action="notification.subscription.delete", db=db, admin=admin,
            resource_type="notification_subscription", resource_id=subscription_id,
            tenant_id=tenant_id,
        )
        db.commit()
        return {"deleted": True}

    # ── Deliveries ──────────────────────────────────────────────────────

    @router.get("/deliveries", response_model=DeliveryListResponse,
                summary="Delivery history")
    def list_deliveries(
        channel_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.NOTIFICATION_READ)),
    ) -> DeliveryListResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        q = db.query(NotificationDelivery).filter(
            NotificationDelivery.tenant_id == tenant_id
        )
        if channel_id is not None:
            q = q.filter(NotificationDelivery.channel_id == channel_id)
        if status is not None:
            if status not in DELIVERY_STATUSES:
                raise HTTPException(
                    status_code=422, detail=f"Unknown status '{status}'"
                )
            q = q.filter(NotificationDelivery.status == status)
        total = q.count()
        rows = (
            q.order_by(NotificationDelivery.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return DeliveryListResponse(
            deliveries=[DeliveryResponse(**r.to_dict()) for r in rows], total=total
        )

    return router
