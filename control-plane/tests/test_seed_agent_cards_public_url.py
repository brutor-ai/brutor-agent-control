# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The hello-world A2A sample agent card is seeded only where the installation
says where the sample runs: `BRUTOR_A2A_SAMPLE_PUBLIC_URL`. The variable has
no default. The Community package ships no sample and does not set it, so a
Community installation has no card pointing at a server that does not exist
(it used to get one at `host.docker.internal:9100`, a private address the
outbound policy refuses by default). The Enterprise trial ships the sample
(`trial_bundle/samples/a2a`) and sets it.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.agent_card import AgentCard
from app.database.models.agent_card_capability import AgentCardCapability
from app.database.models.llm_model import LLModel
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
)
from app.database.models.tenant import Tenant
from app.database.seed_agent_cards import (
    SAMPLE_CARD_NAME,
    seed_sample_agent_cards,
)


# Tables required to exercise the seed end-to-end. LLModel is pulled
# in because Tenant has an FK to it (`Tenant.default_embedding_model_id`),
# matching the pattern in test_seed_caches.py. Plus the
# EndUserGroup trio for the ResourceGroup.end_user_groups selectin.
REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    ResourceGroup.__table__,
    AgentCard.__table__,
    AgentCardCapability.__table__,
    resource_group_agent_cards,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    db: Session = Factory()
    logger = BrutorLogger("test-seed-agent-cards-deployment-mode")

    db.add(
        Tenant(
            id="t-test",
            name="test-tenant",
            display_name="Test Tenant",
            primary_contact_email="ops@example.com",
            is_default=True,
        )
    )
    db.add(
        ResourceGroup(
            id="rg-default",
            tenant_id="t-test",
            name="Default",
            description="Default group",
        )
    )
    db.commit()

    yield db, logger
    db.close()


class TestTheSampleCard:
    def test_the_configured_public_url_is_stamped_into_the_card(self, harness, monkeypatch):
        monkeypatch.setenv(
            "BRUTOR_A2A_SAMPLE_PUBLIC_URL", "http://hello-world-a2a.brutor-internal:9100"
        )
        db, logger = harness
        seed_sample_agent_cards(
            db=db,
            tenant_id="t-test",
            default_group_id="rg-default",
            logger=logger,
        )
        db.commit()

        card = (
            db.query(AgentCard)
            .filter(AgentCard.tenant_id == "t-test")
            .one()
        )
        assert card.name == SAMPLE_CARD_NAME
        assert card.supported_interfaces == [
            {
                "protocolBinding": "JSONRPC",
                "url": "http://hello-world-a2a.brutor-internal:9100",
            }
        ]

    def test_without_one_no_card_is_seeded(self, harness, monkeypatch):
        monkeypatch.delenv("BRUTOR_A2A_SAMPLE_PUBLIC_URL", raising=False)
        db, logger = harness
        seed_sample_agent_cards(
            db=db,
            tenant_id="t-test",
            default_group_id="rg-default",
            logger=logger,
        )
        db.commit()

        assert db.query(AgentCard).count() == 0
        assert db.query(AgentCardCapability).count() == 0
        assert db.execute(resource_group_agent_cards.select()).first() is None

    def test_the_variable_has_no_default_and_no_docker_host(self):
        from app.env_vars import BRUTOR_A2A_SAMPLE_PUBLIC_URL

        assert BRUTOR_A2A_SAMPLE_PUBLIC_URL.default is None
        assert "host.docker.internal" not in BRUTOR_A2A_SAMPLE_PUBLIC_URL.description
