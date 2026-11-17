# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Sample Agent Card Seeding

Seeds a single hello-world A2A v1.0 agent card per tenant, only where
the installation says where the sample runs: `BRUTOR_A2A_SAMPLE_PUBLIC_URL`
(no default). The Enterprise trial ships the matching sample
(`trial_bundle/samples/a2a`: `server.py` for the outbound flow, `inbound.py`
for the inbound one, which needs the card but not the server) and sets the
variable; the Community package ships no sample and seeds no card. Calling
the card through the proxy before the server is up answers 502, and before
the outbound policy allows the sample's host, a refusal naming
`BRUTOR_OUTBOUND_ALLOW_HOSTS`.

Idempotent: keyed on `(tenant_id, name)` via the unique constraint
plus the stable id `agent-card-hello-world-a2a-{tenant_id}`.

Wired into TenantProvisioner.provision_tenant() after MCP catalog
seeding and bound to the tenant's Default resource group so any API
key issued against that group can invoke it.
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard, TRUST_TIER_UNSIGNED
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
)
from app.database.models.resource_mixins import SYSTEM_ACTOR
from app.services.a2a.card_signing import sync_capabilities
from app.env_vars import BRUTOR_A2A_SAMPLE_PUBLIC_URL


SAMPLE_CARD_NAME = "hello-world-a2a"
SAMPLE_CARD_CAPABILITY = "hello"
SAMPLE_CARD_MULTI_TURN_CAPABILITY = "multi-turn-hello"

# URL the gateway core uses to reach the sample server; None: no sample
# card. Native-mode developers (gateway-core on the host) set
# `BRUTOR_A2A_SAMPLE_PUBLIC_URL=http://localhost:9100` before the first start.
def _sample_card_public_url() -> Optional[str]:
    return BRUTOR_A2A_SAMPLE_PUBLIC_URL.string()


def _build_card_json(public_url: str) -> dict:
    """Produce the verbatim A2A v1.0 card JSON.

    Mirrors the shape that `trial_bundle/samples/a2a/server.py` emits at
    `/.well-known/agent-card.json`, so the gateway and the running
    server agree on identity. `skills[]` drives capability row
    creation via `sync_capabilities`.
    """
    return {
        "name": SAMPLE_CARD_NAME,
        "description": (
            "Sample hello-world A2A v1.0 agent. The matching server "
            "ships under trial_bundle/samples/a2a — start it and call "
            "this card through POST /v1/proxy/a2a/outbound to see "
            "Brutor proxy-log an A2A roundtrip."
        ),
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "capabilities": {
            "streaming": True,
        },
        "supportedInterfaces": [
            {
                "protocolBinding": "JSONRPC",
                "url": public_url,
            }
        ],
        "skills": [
            {
                "id": SAMPLE_CARD_CAPABILITY,
                "name": SAMPLE_CARD_CAPABILITY,
                "description": "Echo a greeting back to the caller.",
                "tags": ["hello", "demo"],
                "examples": ["hi", "hello", "say hello"],
                # Bind the inbound A2A capability to the seeded
                # `hello-world` Agent Skill. Both the legacy
                # `/v1/proxy/a2a/inbound` route and the v1.0
                # `/a2a/{tenant}/{card}/message:send` route read
                # handler_type + handler_ref to dispatch — single-shot
                # request/response.
                "handler_type": "skill",
                "handler_ref": "hello-world",
            },
            {
                # Multi-turn demo capability. Bound to the proxy's
                # built-in `multi_turn_demo` handler (no Agent Skill
                # row needed) which walks a tiny two-question dialog
                # via the `input-required` task state. Lets the
                # trial-bundle inbound sample show task continuation
                # working end-to-end — the same task_id is sent
                # across turns until the handler returns `completed`
                # with a final greeting artifact.
                "id": SAMPLE_CARD_MULTI_TURN_CAPABILITY,
                "name": SAMPLE_CARD_MULTI_TURN_CAPABILITY,
                "description": (
                    "Multi-turn demo: asks the caller their name, "
                    "then their mood, then returns a final greeting. "
                    "Uses the A2A v1.0 input-required task state."
                ),
                "tags": ["hello", "demo", "multi-turn"],
                "examples": ["hi"],
                "handler_type": "multi_turn_demo",
                "handler_ref": "multi-turn-hello",
            },
        ],
    }


def seed_sample_agent_cards(
    db: Session,
    tenant_id: str,
    default_group_id: Optional[str],
    logger: BrutorLogger,
) -> None:
    """Seed sample A2A agent cards for a tenant.

    Strictly **create-once**. If the card already exists for this
    tenant we do nothing to its body — any field on the card may have
    been edited by an operator via the admin UI (interface URL is the
    obvious one), and stomping that on every gateway restart is worse
    than the seed becoming stale.

    Earlier versions tried to refresh the seeded body when the
    operator hadn't "touched" the card, detected by comparing the
    description against the seeded description. That was wrong:
    operators edit `card_json.supportedInterfaces[].url` without
    touching `description`, so the seed kept reverting URL changes
    back to the seeded address on every restart. To change the
    seeded shape now (e.g. add a sample skill, or pick a different
    URL via `BRUTOR_A2A_SAMPLE_PUBLIC_URL`), an operator must either
    delete the card via the admin UI and let the seed recreate it, or
    edit the card body directly.

    The resource-group binding is still upserted on every call —
    that's a deployment concern (the Default group exists per tenant
    and the binding is idempotent on `(group_id, card_id)`), not an
    operator-facing edit.
    """
    public_url = _sample_card_public_url()
    if public_url is None:
        # No sample in this installation (the Community package ships
        # none): no card pointing at a server that does not exist.
        return

    stable_id = f"agent-card-hello-world-a2a-{tenant_id}"

    card = db.query(AgentCard).filter(AgentCard.id == stable_id).first()
    if card is None:
        seeded_card_json = _build_card_json(public_url)
        card = AgentCard(
            id=stable_id,
            tenant_id=tenant_id,
            name=SAMPLE_CARD_NAME,
            version="1.0.0",
            description=seeded_card_json["description"],
            source_url=None,
            icon_url=None,
            card_json=seeded_card_json,
            protocol_version="1.0",
            supported_interfaces=seeded_card_json["supportedInterfaces"],
            trust_tier=TRUST_TIER_UNSIGNED,
            enabled=True,
            created_by=SYSTEM_ACTOR,
        )
        db.add(card)
        db.flush()
        # Capability rows are derived from card_json.skills; only
        # populate on create. Sync on every startup would race with
        # `sync_capabilities()` calls from the admin endpoints when
        # an operator edits the card.
        sync_capabilities(db, card)
        logger.info(
            "Seeded sample agent card",
            context={"card_id": card.id, "tenant_id": tenant_id},
        )

    # ── Bind to Default resource group (idempotent) ────────────────────
    if default_group_id:
        existing = db.execute(
            resource_group_agent_cards.select().where(
                (resource_group_agent_cards.c.resource_group_id == default_group_id)
                & (resource_group_agent_cards.c.agent_card_id == card.id)
            )
        ).first()
        if existing is None:
            group = db.query(ResourceGroup).filter(
                ResourceGroup.id == default_group_id
            ).first()
            if group is not None:
                db.execute(
                    resource_group_agent_cards.insert().values(
                        id=f"rgac-hello-world-a2a-{tenant_id}",
                        resource_group_id=default_group_id,
                        agent_card_id=card.id,
                        created_by=SYSTEM_ACTOR,
                    )
                )
                logger.info(
                    "Bound sample agent card to Default group",
                    context={
                        "card_id": card.id,
                        "group_id": default_group_id,
                    },
                )
