# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Portal session revocation.

The portal JWT is the session. The core proxy writes a row here on logout and refuses any portal token
whose ``jti`` is listed. The control plane owns the schema (migration
`a174_eu_revoked_tokens`) and never writes rows; the core prunes rows after their token has
expired.
"""

from sqlalchemy import Column, DateTime, Integer, String, func

from app.database import Base


class EndUserRevokedToken(Base):
    __tablename__ = "end_user_revoked_tokens"

    jti = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    end_user_id = Column(Integer, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reason = Column(String(32), server_default="logout", nullable=False)

    def to_dict(self) -> dict:
        return {
            "jti": self.jti,
            "tenant_id": self.tenant_id,
            "end_user_id": self.end_user_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "reason": self.reason,
        }
