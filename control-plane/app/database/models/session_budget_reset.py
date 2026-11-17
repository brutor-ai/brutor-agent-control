# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy import Column, DateTime, Index, String, Text, func

from app.database import Base


class SessionBudgetReset(Base):
    """A watermark: spend for one conversation counts only from here.

    Session spend is summed from the usage ledger rather than a counter,
    because a conversation outlives any cache and a restart must not hand
    the user a fresh allowance. That choice makes "reset this conversation's
    budget" awkward: you cannot zero a sum.

    The wrong fix is to delete the ledger rows. They are the audit record —
    erasing spend history to satisfy a counter would destroy the evidence
    that the spend happened at all, which for an assurance product is a far
    worse outcome than a budget that cannot be reset.

    So the rows stay and the *query* moves: one row here, and
    `usage_for_session` sums only calls after `reset_at`.

    Why a table rather than a Redis key: the same durability argument that
    put spend in the ledger. A watermark in a cache is lost on restart, and
    losing it silently un-resets a budget an admin deliberately cleared —
    the user is refused again with no explanation and no record of why.

    One row per (tenant, session): a later reset overwrites the earlier
    watermark, because only the most recent one bounds the sum.
    """

    __tablename__ = "session_budget_resets"

    tenant_id = Column(String(255), primary_key=True)
    #: The DERIVED session key (HMAC of the client's asserted chat id), the
    #: same value stored on `proxy_logs.end_user_session_id` — never the raw
    #: client assertion, which one caller could use to name another's chat.
    session_id = Column(String(255), primary_key=True)

    #: Spend before this instant is excluded from the session's total.
    reset_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    #: Who cleared it, and why. A budget reset moves money; an unattributed
    #: one is exactly the kind of gap an auditor asks about.
    reset_by = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_session_budget_resets_tenant", "tenant_id"),
    )
