# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A ticket that opens one administrator stream, once.

A browser cannot authenticate a WebSocket upgrade with a header, so the
address of the upgrade carries what opens it. That is a ticket and never
the session token: an authenticated request mints it, it lives thirty
seconds, the first connection that presents it consumes it, and it opens
only the stream it was minted for, for the administrator and tenant it
was minted by. What is stored is the ticket's SHA-256, so a row read from
the database opens nothing.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Index, String
from sqlalchemy.sql import func

from app.database import Base

# How long a ticket may wait to be presented.
TICKET_LIFETIME_SECONDS = 30

# The streams a ticket can be minted for.
STREAM_SYSTEM_LOGS_TAIL = "system-logs-tail"


class AdminStreamTicket(Base):
    __tablename__ = "admin_stream_tickets"

    ticket_hash = Column(
        String(64), primary_key=True, comment="SHA-256 of the ticket, in hex. The ticket itself is not stored."
    )
    stream = Column(String(64), nullable=False, comment="The stream the ticket opens, and no other.")
    admin_id = Column(String(255), nullable=False, comment="The administrator the ticket was minted by.")
    admin_type = Column(String(32), nullable=False, comment="The kind of administrator: its registered principal.")
    tenant_id = Column(
        String(255), nullable=True, comment="The tenant the ticket was minted for: the administrator's own, or the one a system administrator named."
    )
    session_claims = Column(
        JSON,
        nullable=False,
        comment=(
            "The claims of the session the ticket was minted in (no signature, no secret): the "
            "connection is refused unless that session is still open."
        ),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, comment="After this the ticket opens nothing.")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_admin_stream_tickets_expires_at", "expires_at"),
        {"comment": "Single-use, short-lived tickets that open an administrator stream (a WebSocket)."},
    )
