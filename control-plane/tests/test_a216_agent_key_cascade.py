# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a216_agent_key_cascade — an agent's key does not outlive the agent.

`api_keys.agent_id` was `ON DELETE SET NULL`: deleting an agent identity row
left its key enabled with no agent, and the gateway cannot tell such a key
from a plain key of the key's resource group (the core proved it:
`tests/integration/agent_authz_enforcement.rs`,
`f480_deleting_an_agent_identity_leaves_its_key_a_plain_group_key`). The
admin API never deletes the row, it revokes; only SQL could. The constraint
is `ON DELETE CASCADE` now: the key goes with the agent.

Revoking, the API's way, is unchanged: the agent row stays, suspended, and
its key stays bound to it, which is what the gateway refuses.

Runs on the suite's SQLite; with BRUTOR_PG_TESTS=1 on Postgres, where the
revision itself is run both ways.
"""

from __future__ import annotations


import pytest
import sqlalchemy as sa

from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_identity import AgentIdentity
from app.database.models.api_key import APIKey
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database


def test_the_model_says_cascade():
    (fk,) = APIKey.__table__.columns["agent_id"].foreign_keys
    assert fk.column.table.name == "agent_identities"
    assert fk.ondelete == "CASCADE"
    assert APIKey.__table__.columns["agent_id"].nullable is True


@pytest.fixture()
def h():
    for url in throwaway_pg_database("agent_key_cascade"):
        harness = build_admin_harness(
            [create_agent_identities_router(BrutorLogger("agent-key-cascade"))],
            logger_name="agent-key-cascade",
            database_url=url,
        )

        def fn(s):
            for agent in ("agent-a", "agent-b"):
                s.add(AgentIdentity(id=agent, tenant_id="t1", name=agent))
            s.flush()
            for key, agent in (("key-a", "agent-a"), ("key-b", "agent-b"), ("key-plain", None)):
                s.add(APIKey(id=key, tenant_id="t1", key_hash=f"hash-{key}", key_prefix="brk_test",
                             name=key, scope="global", agent_id=agent))

        harness.seed(fn)
        yield harness
        harness.close()


def _keys(h) -> dict:
    with h.Factory() as s:
        return {k.id: k.agent_id for k in s.query(APIKey).all()}


def test_deleting_the_agent_row_deletes_its_keys(h):
    with h.Factory() as s:
        s.execute(sa.text("DELETE FROM agent_identities WHERE id = 'agent-a'"))
        s.commit()
    # Not `{"key-a": None, ...}`: that was a working key of the resource group.
    assert _keys(h) == {"key-b": "agent-b", "key-plain": None}


def test_revoking_keeps_the_agent_and_its_key_bound_to_it(h):
    r = h.client.post("/v1/admin/agent-identities/agent-a/revoke", headers=h.headers())
    assert r.status_code == 200, r.text
    assert (r.json()["enabled"], r.json()["status"]) == (False, "suspended")
    assert r.json()["revoked_at"] is not None
    with h.Factory() as s:
        agent = s.get(AgentIdentity, "agent-a")
        assert (agent.enabled, agent.status) == (False, "suspended")
    # The key is still the agent's: the gateway refuses it as a fallen
    # agent's credential.
    assert _keys(h) == {"key-a": "agent-a", "key-b": "agent-b", "key-plain": None}


# --- the revision, on Postgres ------------------------------------------------
