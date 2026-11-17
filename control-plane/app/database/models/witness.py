# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Witnesses — the transparency services tree heads are registered with

A witness is an RFC 9943 transparency service (`scitt`,
returning an RFC 9942 receipt) or an RFC 3161 timestamp authority
(`rfc3161`, date only). Platform-scope rows are configured by a system
admin and apply to every tenant; tenant-scope rows are a tenant's own
additions — typically an auditor's instance — with a pinned key and an
operator label so the receipt says whose word it carries.

`same_operator` is the honesty flag: the trial bundle ships its own
`brutor-witness`, which proves ordering but not independence, and a head
witnessed only by it grades `self_witnessed` rather than `witnessed`.

Written by the control plane; read by the core's witness registrar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, Index, String, Text, func
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_log import WITNESS_KINDS
from app.database.models.resource_mixins import ResourceAuthorshipMixin

SCOPE_PLATFORM = "platform"
SCOPE_TENANT = "tenant"
WITNESS_SCOPES = frozenset({SCOPE_PLATFORM, SCOPE_TENANT})

URL_SCHEMES = ("http://", "https://")


class Witness(Base, ResourceAuthorshipMixin):
    """One transparency service or timestamp authority."""

    __tablename__ = "witnesses"

    id = Column(String(64), primary_key=True)
    scope = Column(String(16), nullable=False, comment="platform | tenant")
    tenant_id = Column(String(255), nullable=True, comment="Set for scope=tenant only")
    kind = Column(String(16), nullable=False, comment="scitt | rfc3161")
    url = Column(Text, nullable=False, comment="Registration endpoint; subject to the outbound policy")
    operator_label = Column(String(255), nullable=False, comment="Who runs it — shown on every receipt")
    pinned_key = Column(
        Text,
        nullable=True,
        comment=(
            "Key the receipts must verify under; a receipt under any other key is "
            "`invalid`. scitt: hex public key; rfc3161: TSA certificate PEM or "
            "SHA-256 hex of its DER (TEXT: a PEM does not fit 128 chars)"
        ),
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    same_operator = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Run by the same operator as the gateway — proves ordering, not independence",
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_witnesses_tenant", "tenant_id"),)

    @validates("scope")
    def _validate_scope(self, _key, value):
        if value not in WITNESS_SCOPES:
            raise ValueError(f"scope must be one of {sorted(WITNESS_SCOPES)}, got {value!r}")
        return value

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in WITNESS_KINDS:
            raise ValueError(f"kind must be one of {sorted(WITNESS_KINDS)}, got {value!r}")
        return value

    @validates("url")
    def _validate_url(self, _key, value):
        if not isinstance(value, str) or not value.strip().lower().startswith(URL_SCHEMES):
            raise ValueError("url must be an http:// or https:// URL")
        return value.strip()

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
            "kind": self.kind,
            "url": self.url,
            "operator_label": self.operator_label,
            "pinned_key": self.pinned_key,
            "enabled": bool(self.enabled),
            "same_operator": bool(self.same_operator),
        }
        data.update(self.authorship_dict())
        return data


__all__ = [
    "Witness",
    "WITNESS_SCOPES",
    "SCOPE_PLATFORM",
    "SCOPE_TENANT",
    "URL_SCHEMES",
]
