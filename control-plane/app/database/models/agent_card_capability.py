# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Denormalised one-row-per-capability table.

A2A cards declare their capabilities as a JSON array inside the card
document. Matching policies against that at eval time ("deny any
outbound call to capability `billing.refund` with data_classification
= PII") would mean either:

  1. Parsing JSON on every policy decision — slow, and hides the
     capability surface from SQL-based audit queries.
  2. Duplicating capability state into a separate table with indexes.

This model picks option 2. Capability rows are regenerated atomically from
`card_json` on every upsert — see
`app/services/a2a/card_signing.py::sync_capabilities`. Admins never
edit this table directly.
"""

from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, validates
from sqlalchemy.sql import func

from app.database import Base


# JSONB on Postgres, JSON on SQLite — mirrors migration `a012_a2a_v1_schema`, which adds
# `extensions` as `postgresql.JSONB()`. Kept in step with the same constant in
# `agent_card.py`: the two schema paths (create_all on a fresh DB, migrations
# on an existing one) must produce the same column type.
_JSONB_TYPE = JSONB().with_variant(JSON(), "sqlite")


# Handler types the inbound proxy knows how to dispatch. Kept as a
# module constant rather than an enum because the column is a plain
# VARCHAR — see agent_card.py's trust_tier for the same pattern.
HANDLER_TYPE_SKILL = "skill"
HANDLER_TYPE_NOOP = "noop"
# Internal handler hard-coded into the v1.0 inbound dispatch path
# (routes/a2a_v1.rs::dispatch_multi_turn_demo). Used only by the
# seeded hello-world-a2a card to demonstrate input-required → task
# continuation → completed in the trial bundle. Not intended for
# operator-managed cards.
HANDLER_TYPE_MULTI_TURN_DEMO = "multi_turn_demo"

VALID_HANDLER_TYPES = frozenset({
    HANDLER_TYPE_SKILL,
    HANDLER_TYPE_NOOP,
    HANDLER_TYPE_MULTI_TURN_DEMO,
})


class AgentCardCapability(Base):
    """One declared capability of an Agent Card."""

    __tablename__ = "agent_card_capabilities"

    id = Column(String(255), primary_key=True)
    agent_card_id = Column(
        String(255),
        ForeignKey("agent_cards.id", ondelete="CASCADE"),
        nullable=False,
    )

    capability_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    input_schema = Column(JSON, nullable=True)
    output_schema = Column(JSON, nullable=True)

    # Governance overrides. Null means "inherit from card / server
    # default". Kept here rather than in card_json so they can be
    # indexed + filtered per-capability.
    timeout_secs = Column(Integer, nullable=True)
    rate_limit_per_hour = Column(Integer, nullable=True)

    # List of classification tags (e.g. ["PII", "PHI"]). Stored as
    # JSON array rather than a comma-separated string so the policy
    # evaluator can `@>` / contains-match cleanly on Postgres.
    data_classification = Column(JSON, nullable=True)

    # Handler binding. The inbound proxy resolves a
    # capability invocation to a local handler. Null handler_type is
    # treated as 'noop' at dispatch time so a card with no handler
    # won't silently execute anything when an external agent calls it.
    handler_type = Column(String(32), nullable=True)
    handler_ref = Column(String(255), nullable=True)

    # v1.0 upgrade — `Message.extensions[]` / `Artifact.extensions[]`
    # sourced from the card body. Null for legacy v0.3 rows.
    extensions = Column(
        _JSONB_TYPE, nullable=True,
        comment="Extension URIs declared at this capability (v1.0)."
    )

    enabled = Column(Boolean, nullable=False, default=True,
                     server_default="true")
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(), nullable=False)

    agent_card = relationship("AgentCard", back_populates="capabilities")

    __table_args__ = (
        UniqueConstraint("agent_card_id", "capability_name",
                         name="uix_agent_card_caps_name"),
        Index("ix_agent_card_caps_card", "agent_card_id"),
        Index("ix_agent_card_caps_name", "capability_name"),
    )

    @validates("handler_type")
    def _validate_handler_type(
        self, _key: str, value: Optional[str]
    ) -> Optional[str]:
        if value is None or value == "":
            return None
        if value not in VALID_HANDLER_TYPES:
            raise ValueError(
                f"Invalid handler_type {value!r}; expected one of "
                f"{sorted(VALID_HANDLER_TYPES)} or null"
            )
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_card_id": self.agent_card_id,
            "capability_name": self.capability_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "timeout_secs": self.timeout_secs,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "data_classification": self.data_classification,
            "handler_type": self.handler_type,
            "handler_ref": self.handler_ref,
            "enabled": self.enabled,
        }
