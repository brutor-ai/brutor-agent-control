# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Closure resolution, contracts and the lifecycle gate

Three properties carry the phase:

  * **The closure is deterministic.** It is hashed, so two resolutions of an
    unchanged configuration must be byte-identical. If they are not, every
    sweep reports a config change and the version history becomes noise
    nobody reads.

  * **The diff answers the approver's question.** Not "what changed" but
    "does this give the system something it did not have before". A flat
    change list is how a reviewer learns to click approve without reading,
    so widening and narrowing are separated and the gate keys on the former.

  * **The gate refuses with the whole list.** A gate that reports the first
    missing requirement sends an operator round the loop once per
    requirement, and by the third trip they are looking for the override.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.end_user import EndUser
from app.database.models.idp_server import IdPServer
from app.database.models.agent_identity import (
    AgentGrant,
    AgentIdentity,
    ResourceGroupAgentMember,
)
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.ai_system_contract import (
    CONTRACT_ACTIVE,
    CONTRACT_DRAFT,
    CONTRACT_REJECTED,
    CONTRACT_SUPERSEDED,
    AISystemContract,
    hash_closure,
)
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_replay import (
    AISystemReplayCase,
    AISystemReplayRun,
    AISystemReplaySuite,
    REPLAY_COMPLETED,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    GroupType,
    ResourceGroup,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant
from app.services import ai_system_contract_service as contracts
from app.services import ai_system_lifecycle_gate as gate
from app.services.ai_system_closure import (
    inheritance_chain,
    merge_restrictive,
    resolve_closure,
)

# Named rather than imported one by one: the closure touches every
# association hanging off a resource group, and a missing one shows up as
# an opaque "no such table" from a relationship load.
_BY_NAME = [
    "llm_catalog",
    "llm_routing_group",
    "llm_providers",
    "llm_provider_catalog",
    "end_user_groups",
    "resource_group_end_user_groups",
    "guardrail_configs",
    "resource_group_guardrails",
    "argument_policies",
    "resource_group_argument_policies",
    "semantic_cache_configs",
    "resource_group_cache_configs",
    # The closure snapshots response policies (they are part of the
    # contract), so resolving it reads this table.
    "ai_system_response_policies",
    # Part of the closure once set.
    "ai_system_data_scopes",
    "ai_system_reach_rules",
    # GuardrailConfig carries an mcp_servers FK (per-server scoping).
    "mcp_servers",
    # The identity floor of every way into `active` reads the calls and the
    # credentials that made them.
    "proxy_logs",
    "api_keys",
]

REQUIRED_TABLES = [Base.metadata.tables[n] for n in _BY_NAME] + [
    LLModel.__table__,
    Tenant.__table__,
    ResourceGroup.__table__,
    ServerConfig.__table__,
    AgentSkill.__table__,
    AgentIdentity.__table__,
    AgentGrant.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    ResourceGroupAgentMember.__table__,
    AISystemContract.__table__,
    AISystemLivenessExpectation.__table__,
    AISystemRun.__table__,
    AISystemReplaySuite.__table__,
    AISystemReplayCase.__table__,
    AISystemReplayRun.__table__,
    resource_group_llm_models,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
    resource_group_agent_skills,
]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # a184_composite_ai_systems — composition, liveness and conformance read these.
    _extra = [Base.metadata.tables[n] for n in ['ai_system_run_segments', 'ai_system_dependencies', 'agent_cards', 'ai_system_liveness_expectations', 'kb_collections', 'resource_group_kb_collections']
              if Base.metadata.tables[n] not in REQUIRED_TABLES]
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES + _extra)
    session = sessionmaker(bind=engine, future=True)()
    session.add(
        Tenant(
            id="t1", name="t1", display_name="T1", organization_name="T1",
            primary_contact_email="a@b.c", is_active=True, is_default=False,
            is_trial=False, suspended=False, billing_plan="self_hosted", features={},
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def make_system(db, gid="sys", parent=None, inherits=True, **kwargs):
    g = ResourceGroup(
        id=gid,
        tenant_id="t1",
        name=gid,
        hierarchy_level=0 if parent is None else 1,
        inherit_resources=inherits,
        is_active=True,
        is_system=False,
        group_type=GroupType.AI_SYSTEM,
        system_kind="agent",
        parent_group_id=parent,
        **kwargs,
    )
    db.add(g)
    db.flush()
    return g


def _provider(db):
    """One shared provider — llm_model.provider_id is NOT NULL."""
    from app.database.models.llm_provider import LlmProvider

    if db.get(LlmProvider, "prov-1") is None:
        db.add(
            LlmProvider(
                id="prov-1", tenant_id="t1", key="openai", display_name="OpenAI",
                enabled=True, is_system=False, import_enabled=False,
            )
        )
        db.flush()
    return "prov-1"


def bind_model(db, group_id, model_id, **cols):
    db.add(
        LLModel(
            id=model_id, tenant_id="t1", name=model_id, model_name=model_id,
            provider_id=_provider(db),
        )
    )
    db.flush()
    db.execute(
        resource_group_llm_models.insert().values(
            id=f"{group_id}-{model_id}",
            resource_group_id=group_id,
            llm_model_id=model_id,
            **cols,
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# Closure
# ---------------------------------------------------------------------------


class TestInheritanceChain:
    def test_the_system_itself_is_always_first(self, db):
        sys = make_system(db)
        assert [g.id for g in inheritance_chain(sys)] == ["sys"]

    def test_it_walks_up_while_each_group_inherits(self, db):
        make_system(db, "root")
        make_system(db, "mid", parent="root")
        child = make_system(db, "child", parent="mid")
        assert [g.id for g in inheritance_chain(child)] == ["child", "mid", "root"]

    def test_it_stops_at_the_first_non_inheriting_group(self, db):
        make_system(db, "root")
        make_system(db, "mid", parent="root", inherits=False)
        child = make_system(db, "child", parent="mid")
        # `mid`'s own bindings still apply; `root`'s do not.
        assert [g.id for g in inheritance_chain(child)] == ["child", "mid"]

    def test_a_non_inheriting_system_sees_only_itself(self, db):
        make_system(db, "root")
        child = make_system(db, "child", parent="root", inherits=False)
        assert [g.id for g in inheritance_chain(child)] == ["child"]


class TestRestrictiveMerge:
    def test_numbers_take_the_minimum(self):
        assert merge_restrictive([{"cap": 10}, {"cap": 3}]) == {"cap": 3}

    def test_the_parent_cannot_be_loosened_by_the_child(self):
        # Nearest-first order, so the child is the first blob.
        assert merge_restrictive([{"cap": 99}, {"cap": 5}]) == {"cap": 5}

    def test_a_key_only_the_parent_sets_still_applies(self):
        assert merge_restrictive([{"a": 1}, {"b": 2}]) == {"a": 1, "b": 2}

    def test_booleans_take_the_restrictive_value(self):
        assert merge_restrictive([{"block": False}, {"block": True}]) == {"block": True}

    def test_booleans_are_not_treated_as_numbers(self):
        # bool subclasses int in Python; a naive `min` would answer the
        # wrong question and silently unblock.
        assert merge_restrictive([{"x": True}, {"x": False}])["x"] is True

    def test_nested_dicts_recurse(self):
        merged = merge_restrictive([{"n": {"a": 9}}, {"n": {"a": 2, "b": 1}}])
        assert merged == {"n": {"a": 2, "b": 1}}

    def test_none_does_not_win(self):
        assert merge_restrictive([{"cap": None}, {"cap": 5}]) == {"cap": 5}
        assert merge_restrictive([{"cap": 5}, {"cap": None}]) == {"cap": 5}

    def test_non_blobs_are_skipped_rather_than_crashing(self):
        assert merge_restrictive([None, {"a": 1}, "nonsense"]) == {"a": 1}


class TestClosureDetermination:
    def test_resolution_is_stable(self, db):
        sys = make_system(db, owner="team", intended_use="do a thing")
        bind_model(db, "sys", "m1")
        bind_model(db, "sys", "m2")
        assert hash_closure(resolve_closure(db, sys)) == hash_closure(
            resolve_closure(db, sys)
        )

    def test_binding_order_does_not_change_the_hash(self, db):
        a = make_system(db, "a")
        bind_model(db, "a", "m2")
        bind_model(db, "a", "m1")
        b = make_system(db, "b")
        bind_model(db, "b", "m1x")
        first = resolve_closure(db, a)["composition"]["llm_models"]
        assert [m["id"] for m in first] == ["m1", "m2"], "sorted, not insertion order"

    def test_a_real_change_moves_the_hash(self, db):
        sys = make_system(db)
        before = hash_closure(resolve_closure(db, sys))
        bind_model(db, "sys", "m1")
        db.expire_all()
        assert hash_closure(resolve_closure(db, sys)) != before

    def test_inherited_resources_are_marked_as_such(self, db):
        make_system(db, "parent")
        child = make_system(db, "child", parent="parent")
        bind_model(db, "parent", "shared")
        bind_model(db, "child", "own")
        models = {m["id"]: m for m in resolve_closure(db, child)["composition"]["llm_models"]}
        assert models["shared"]["inherited"] is True
        assert models["own"]["inherited"] is False

    def test_a_resource_bound_twice_is_one_grant(self, db):
        # Bound at both the system and its parent: one grant, and the
        # nearer binding is the one whose limits apply.
        make_system(db, "parent")
        child = make_system(db, "child", parent="parent")
        db.add(
            LLModel(
                id="m", tenant_id="t1", name="m", model_name="m",
                provider_id=_provider(db),
            )
        )
        db.flush()
        for gid, limits in (("parent", {"cap": 10}), ("child", {"cap": 2})):
            db.execute(
                resource_group_llm_models.insert().values(
                    id=f"{gid}-m", resource_group_id=gid, llm_model_id="m", limits=limits
                )
            )
        db.flush()
        models = resolve_closure(db, child)["composition"]["llm_models"]
        assert len(models) == 1
        assert models[0]["bound_via"] == "child"
        assert models[0]["limits"] == {"cap": 2}

    def test_a_non_inheriting_system_does_not_see_the_parents_resources(self, db):
        make_system(db, "parent")
        child = make_system(db, "child", parent="parent", inherits=False)
        bind_model(db, "parent", "hidden")
        assert resolve_closure(db, child)["composition"]["llm_models"] == []

    def test_limits_merge_restrictively_up_the_chain(self, db):
        make_system(db, "parent", llm_global_limits={"max_tokens": 100})
        child = make_system(db, "child", parent="parent",
                            llm_global_limits={"max_tokens": 900})
        assert resolve_closure(db, child)["limits"]["llm"]["max_tokens"] == 100

    def test_autonomy_resolves_across_the_chain(self, db):
        make_system(db, "parent", autonomy_level="restricted")
        child = make_system(db, "child", parent="parent", autonomy_level="autonomous")
        assert resolve_closure(db, child)["autonomy_level"] == "restricted"

    def test_eu_ai_act_role_is_sealed_only_when_declared(self, db):
        # Conditional key, like the operating envelope: a system without a
        # declared role keeps its existing canonical JSON (no fleet-wide
        # drift), one that declares it carries the term an approver signs.
        plain = make_system(db, "plain")
        assert "eu_ai_act_role" not in resolve_closure(db, plain)["ai_system"]
        deployer = make_system(db, "deployer", eu_ai_act_role="deployer")
        sealed = resolve_closure(db, deployer)["ai_system"]
        assert sealed["eu_ai_act_role"] == "deployer"
        assert sealed["owner"] == deployer.owner
        assert sealed["intended_use"] == deployer.intended_use

    def test_the_closure_is_self_describing(self, db):
        sys = make_system(db)
        closure = resolve_closure(db, sys)
        assert closure["schema_version"] >= 1
        assert closure["inheritance_chain"] == ["sys"]


# ---------------------------------------------------------------------------
# Minting and diffing
# ---------------------------------------------------------------------------


class TestMinting:
    def test_the_first_contract_is_version_one(self, db):
        sys = make_system(db)
        contract, created = contracts.mint(db, sys)
        assert created and contract.version == 1
        assert contract.status == CONTRACT_DRAFT
        assert contract.diff_from_version is None

    def test_minting_an_unchanged_config_does_not_add_a_version(self, db):
        # Otherwise every save piles up an identical version and the
        # history — the thing an auditor scrolls — becomes unreadable.
        sys = make_system(db)
        first, _ = contracts.mint(db, sys)
        again, created = contracts.mint(db, sys)
        assert not created
        assert again.id == first.id
        assert db.query(AISystemContract).count() == 1

    def test_promoting_a_contract_does_not_invalidate_it(self, db):
        """The bug this guards: `lifecycle_stage` used to live in the closure.

        Promoting a contract is exactly what advances the stage, so the
        moment a contract went live the running config no longer matched
        the closure that had just been signed — every AI System reported
        "running configuration nobody approved" forever, and each attempt
        to clear it minted another empty version.
        """
        sys = make_system(db, lifecycle_stage="approved")
        contract, _ = contracts.mint(db, sys)
        contracts.approve(db, contract, approved_by="alice")
        contracts.promote(db, contract, lifecycle_stage=sys.lifecycle_stage)

        # The promotion moves the system on, as promotions do.
        sys.lifecycle_stage = "active"
        db.flush()
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()

        assert contracts.is_config_drifted(db, sys) is False

    def test_a_stage_change_alone_mints_nothing(self, db):
        # The same fact from the operator's side: walking the lifecycle is
        # not a configuration change, so it must not spawn versions.
        sys = make_system(db, lifecycle_stage="proposed")
        contracts.mint(db, sys)
        for stage in ("approved", "active", "deprecated"):
            sys.lifecycle_stage = stage
            db.flush()
            db.expire_all()
            sys = db.query(ResourceGroup).filter_by(id="sys").one()
            _, created = contracts.mint(db, sys)
            assert created is False, f"minted a version just for stage={stage}"
        assert db.query(AISystemContract).count() == 1

    def test_the_closure_does_not_carry_the_lifecycle_stage(self, db):
        # Asserted on the closure itself, not just its effects — the stage
        # is the system's position in its own workflow, not a control.
        sys = make_system(db, lifecycle_stage="active")
        contract, _ = contracts.mint(db, sys)
        assert "lifecycle_stage" not in contract.resolved_closure["ai_system"]
        assert contract.resolved_closure["schema_version"] >= 2

    def test_force_mints_an_empty_version_for_re_attestation(self, db):
        # An approver can only sign a draft, so a new owner who wants their
        # own name against unchanged config needs a version to approve.
        sys = make_system(db)
        first, _ = contracts.mint(db, sys)
        again, created = contracts.mint(db, sys, created_by="bob", force=True)
        assert created is True
        assert again.id != first.id
        assert again.version == first.version + 1
        assert again.contract_hash == first.contract_hash
        assert again.diff_summary["widened_count"] == 0
        assert again.diff_summary["narrowed_count"] == 0
        assert again.diff_summary["changed_count"] == 0

    def test_a_config_change_produces_a_new_version_with_a_diff(self, db):
        sys = make_system(db)
        contracts.mint(db, sys)
        bind_model(db, "sys", "m1")
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        second, created = contracts.mint(db, sys)
        assert created and second.version == 2
        assert second.diff_from_version == 1
        assert second.diff_summary["has_widened_permissions"] is True

    def test_a_revert_is_a_new_version_not_a_reused_one(self, db):
        sys = make_system(db, owner="a")
        contracts.mint(db, sys)
        sys.owner = "b"
        db.flush()
        contracts.mint(db, sys)
        sys.owner = "a"
        db.flush()
        third, created = contracts.mint(db, sys)
        assert created and third.version == 3
        # The hash legitimately matches v1 — a fact worth being able to see.
        v1 = db.query(AISystemContract).filter_by(version=1).one()
        assert third.contract_hash == v1.contract_hash


_POLICY = {
    "mode": "enforce",
    "min_assurance": "workload",
    "allow_user_keys": False,
    "allowed_issuers": None,
    "max_token_lifetime_s": 900,
    "max_key_ttl_days": 30,
}


class TestIdentityPolicyInClosure:
    """A contract records the identity policy it was minted under: the
    system's stored policy, null when unset. Not the effective policy: that
    moves with the lifecycle stage, and a promotion would read as drift."""

    def _reload(self, db):
        db.flush()
        db.expire_all()
        return db.query(ResourceGroup).filter_by(id="sys").one()

    def test_an_unset_policy_is_recorded_as_null(self, db):
        sys = make_system(db)
        closure = resolve_closure(db, sys)
        assert "identity_policy" in closure
        assert closure["identity_policy"] is None

    def test_the_stored_policy_is_recorded_as_stored(self, db):
        sys = make_system(db, identity_policy=dict(_POLICY))
        assert resolve_closure(db, sys)["identity_policy"] == _POLICY

    def test_setting_and_changing_the_policy_moves_the_hash(self, db):
        sys = make_system(db)
        unset = hash_closure(resolve_closure(db, sys))
        sys.identity_policy = dict(_POLICY)
        sys = self._reload(db)
        set_once = hash_closure(resolve_closure(db, sys))
        assert set_once != unset
        sys.identity_policy = {**_POLICY, "max_token_lifetime_s": 600}
        sys = self._reload(db)
        assert hash_closure(resolve_closure(db, sys)) not in (unset, set_once)

    def test_only_the_policy_member_changes(self, db):
        sys = make_system(db)
        before = resolve_closure(db, sys)
        sys.identity_policy = dict(_POLICY)
        sys = self._reload(db)
        after = resolve_closure(db, sys)
        assert {k for k in after if after[k] != before.get(k)} == {"identity_policy"}
        assert set(after) == set(before)

    def test_the_stage_does_not_move_the_recorded_policy(self, db):
        # The effective policy of a production system is stricter than the
        # stored one; recording it would drift the contract on promotion.
        sys = make_system(db, lifecycle_stage="approved", identity_policy={
            **_POLICY, "mode": "audit", "min_assurance": "key",
        })
        before = resolve_closure(db, sys)
        sys.lifecycle_stage = "active"
        sys = self._reload(db)
        after = resolve_closure(db, sys)
        assert after["identity_policy"] == before["identity_policy"]
        assert after["identity_policy"]["min_assurance"] == "key"
        assert hash_closure(after) == hash_closure(before)

    def test_a_policy_change_mints_a_new_contract_hash(self, db):
        sys = make_system(db)
        first, _ = contracts.mint(db, sys)
        assert first.resolved_closure["identity_policy"] is None
        sys.identity_policy = dict(_POLICY)
        sys = self._reload(db)
        assert contracts.is_config_drifted(db, sys) is False  # no active contract yet
        second, created = contracts.mint(db, sys)
        assert created and second.version == 2
        assert second.contract_hash != first.contract_hash
        assert second.resolved_closure["identity_policy"] == _POLICY
        assert second.contract_hash == hash_closure(resolve_closure(db, sys))

    def test_a_policy_change_after_promotion_is_drift(self, db):
        sys = make_system(db, lifecycle_stage="approved")
        contract, _ = contracts.mint(db, sys)
        contracts.approve(db, contract, approved_by="alice")
        contracts.promote(db, contract, lifecycle_stage=sys.lifecycle_stage)
        sys = self._reload(db)
        assert contracts.is_config_drifted(db, sys) is False
        sys.identity_policy = dict(_POLICY)
        sys = self._reload(db)
        assert contracts.is_config_drifted(db, sys) is True


_DEV_DEFAULT = {
    "mode": "audit",
    "min_assurance": "key",
    "allow_user_keys": True,
    "allowed_issuers": None,
    "max_token_lifetime_s": 3600,
    "max_key_ttl_days": None,
}
_WITH_ISSUERS = {**_POLICY, "allowed_issuers": ["idp-a", "idp-b"]}


class TestIdentityPolicyDiff:
    """An approver sees an identity-policy change: loosening is a widening,
    tightening a narrowing, field by field in the policy's member order;
    each entry names its field."""

    def _diff(self, before, after):
        return contracts.diff_closures({"identity_policy": before}, {"identity_policy": after})

    def _entries(self, diff, bucket):
        return [(e["field"], e["from"], e["to"]) for e in diff[bucket] if e["kind"] == "identity_policy"]

    def _only(self, before, after, bucket):
        diff = self._diff(before, after)
        others = {"widened", "narrowed", "changed"} - {bucket}
        for other in others:
            assert self._entries(diff, other) == [], other
        return self._entries(diff, bucket)

    @pytest.mark.parametrize(
        "field, stricter, looser",
        [
            ("min_assurance", "workload", "bound_key"),
            ("mode", "enforce", "audit"),
            ("allow_user_keys", False, True),
            ("max_token_lifetime_s", 900, 1800),
            ("max_key_ttl_days", 30, 90),
            ("max_key_ttl_days", 30, None),
        ],
    )
    def test_a_looser_field_is_a_widening(self, field, stricter, looser):
        before = {**_POLICY, field: stricter}
        after = {**_POLICY, field: looser}
        assert self._only(before, after, "widened") == [(field, stricter, looser)]

    @pytest.mark.parametrize(
        "field, stricter, looser",
        [
            ("min_assurance", "workload_pop", "workload"),
            ("mode", "enforce", "audit"),
            ("allow_user_keys", False, True),
            ("max_token_lifetime_s", 600, 900),
            ("max_key_ttl_days", 7, 30),
            ("max_key_ttl_days", 30, None),
        ],
    )
    def test_a_stricter_field_is_a_narrowing(self, field, stricter, looser):
        before = {**_POLICY, field: looser}
        after = {**_POLICY, field: stricter}
        assert self._only(before, after, "narrowed") == [(field, looser, stricter)]

    def test_added_issuers_are_a_widening(self):
        after = {**_WITH_ISSUERS, "allowed_issuers": ["idp-a", "idp-b", "idp-c"]}
        assert self._only(_WITH_ISSUERS, after, "widened") == [
            ("allowed_issuers", ["idp-a", "idp-b"], ["idp-a", "idp-b", "idp-c"])
        ]

    def test_removing_the_issuer_list_is_a_widening(self):
        # null = any issuer.
        after = {**_WITH_ISSUERS, "allowed_issuers": None}
        assert self._only(_WITH_ISSUERS, after, "widened") == [
            ("allowed_issuers", ["idp-a", "idp-b"], None)
        ]

    def test_removed_issuers_are_a_narrowing(self):
        after = {**_WITH_ISSUERS, "allowed_issuers": ["idp-a"]}
        assert self._only(_WITH_ISSUERS, after, "narrowed") == [
            ("allowed_issuers", ["idp-a", "idp-b"], ["idp-a"])
        ]

    def test_a_new_issuer_list_is_a_narrowing(self):
        assert self._only(_POLICY, _WITH_ISSUERS, "narrowed") == [
            ("allowed_issuers", None, ["idp-a", "idp-b"])
        ]

    def test_a_swapped_issuer_is_a_widening(self):
        # A newly accepted IdP is let in, whatever else was removed.
        after = {**_WITH_ISSUERS, "allowed_issuers": ["idp-a", "idp-c"]}
        assert self._only(_WITH_ISSUERS, after, "widened") == [
            ("allowed_issuers", ["idp-a", "idp-b"], ["idp-a", "idp-c"])
        ]

    def test_removing_a_policy_stricter_than_the_default_is_a_widening(self):
        # No explicit policy contributes the development default.
        assert self._only(_POLICY, None, "widened") == [
            ("mode", "enforce", "audit"),
            ("min_assurance", "workload", "key"),
            ("allow_user_keys", False, True),
            ("max_token_lifetime_s", 900, 3600),
            ("max_key_ttl_days", 30, None),
        ]

    def test_setting_a_policy_stricter_than_the_default_is_a_narrowing(self):
        assert self._only(None, _POLICY, "narrowed") == [
            ("mode", "audit", "enforce"),
            ("min_assurance", "key", "workload"),
            ("allow_user_keys", True, False),
            ("max_token_lifetime_s", 3600, 900),
            ("max_key_ttl_days", None, 30),
        ]

    def test_setting_or_removing_a_policy_equal_to_the_default_is_a_change(self):
        assert self._only(None, dict(_DEV_DEFAULT), "changed") == [("policy", None, _DEV_DEFAULT)]
        assert self._only(dict(_DEV_DEFAULT), None, "changed") == [("policy", _DEV_DEFAULT, None)]

    def test_a_mixed_change_reports_each_field_in_member_order(self):
        after = {
            **_POLICY,
            "min_assurance": "workload_pop",  # tightened
            "allow_user_keys": True,  # loosened
            "allowed_issuers": ["idp-a"],  # tightened (was any)
            "max_token_lifetime_s": 3600,  # loosened
        }
        diff = self._diff(_POLICY, after)
        assert self._entries(diff, "widened") == [
            ("allow_user_keys", False, True),
            ("max_token_lifetime_s", 900, 3600),
        ]
        assert self._entries(diff, "narrowed") == [
            ("min_assurance", "workload", "workload_pop"),
            ("allowed_issuers", None, ["idp-a"]),
        ]
        assert self._entries(diff, "changed") == []
        assert diff["has_widened_permissions"] is True
        assert all(e["detail"] for e in diff["widened"] + diff["narrowed"])

    def test_an_unchanged_policy_diffs_to_nothing(self):
        diff = self._diff(_POLICY, dict(_POLICY))
        assert diff["widened_count"] == diff["narrowed_count"] == diff["changed_count"] == 0
        assert self._diff(None, None)["changed_count"] == 0

    def test_a_loosened_policy_mints_a_widening_version(self, db):
        sys = make_system(db, identity_policy=dict(_POLICY))
        contracts.mint(db, sys)
        sys.identity_policy = {**_POLICY, "mode": "audit"}
        db.flush()
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        second, created = contracts.mint(db, sys)
        assert created and second.diff_summary["has_widened_permissions"] is True


class TestDiffDirection:
    def _diff(self, before, after):
        return contracts.diff_closures(before, after)

    def test_an_added_resource_is_a_widening(self):
        d = self._diff(
            {"composition": {"mcp_servers": [{"id": "a"}]}},
            {"composition": {"mcp_servers": [{"id": "a"}, {"id": "b"}]}},
        )
        assert d["widened_count"] == 1
        assert d["narrowed_count"] == 0
        assert d["has_widened_permissions"] is True

    def test_a_removed_resource_is_a_narrowing(self):
        d = self._diff(
            {"composition": {"skills": [{"id": "a"}, {"id": "b"}]}},
            {"composition": {"skills": [{"id": "a"}]}},
        )
        assert d["narrowed_count"] == 1
        assert d["has_widened_permissions"] is False

    def test_a_raised_cap_is_a_widening(self):
        d = self._diff({"limits": {"llm": {"cap": 10}}}, {"limits": {"llm": {"cap": 99}}})
        assert d["widened_count"] == 1

    def test_a_lowered_cap_is_a_narrowing(self):
        d = self._diff({"limits": {"llm": {"cap": 99}}}, {"limits": {"llm": {"cap": 10}}})
        assert d["narrowed_count"] == 1
        assert d["has_widened_permissions"] is False

    def test_a_removed_cap_is_the_widest_change_of_all(self):
        # "no limit" reads as harmless in a plain change list and is
        # anything but.
        d = self._diff({"limits": {"llm": {"cap": 10}}}, {"limits": {"llm": {}}})
        assert d["widened_count"] == 1
        assert "removed" in d["widened"][0]["detail"]

    def test_an_added_cap_is_a_narrowing(self):
        d = self._diff({"limits": {"llm": {}}}, {"limits": {"llm": {"cap": 10}}})
        assert d["narrowed_count"] == 1

    def test_relaxing_autonomy_is_a_widening(self):
        d = self._diff({"autonomy_level": "restricted"}, {"autonomy_level": "autonomous"})
        assert d["widened_count"] == 1
        assert d["has_widened_permissions"] is True

    def test_tightening_autonomy_is_a_narrowing(self):
        d = self._diff({"autonomy_level": "autonomous"}, {"autonomy_level": "suspended"})
        assert d["narrowed_count"] == 1
        assert d["has_widened_permissions"] is False

    def test_a_widening_alongside_a_narrowing_still_flags(self):
        # The failure this guards: counting changes instead of reading
        # direction, so "3 changes" hides the one that mattered.
        d = self._diff(
            {"composition": {"skills": [{"id": "a"}]}, "limits": {"llm": {"cap": 99}}},
            {"composition": {"skills": [{"id": "a"}, {"id": "b"}]},
             "limits": {"llm": {"cap": 5}}},
        )
        assert d["has_widened_permissions"] is True
        assert d["widened_count"] == 1 and d["narrowed_count"] == 1

    def test_documentation_edits_are_neither(self):
        d = self._diff(
            {"ai_system": {"owner": "a", "intended_use": "x"}},
            {"ai_system": {"owner": "b", "intended_use": "y"}},
        )
        assert d["widened_count"] == 0 and d["narrowed_count"] == 0
        assert d["changed_count"] == 2

    def test_an_identical_closure_diffs_to_nothing(self):
        same = {"composition": {"skills": [{"id": "a"}]}, "limits": {"llm": {"cap": 1}}}
        d = self._diff(same, dict(same))
        assert d["widened_count"] == d["narrowed_count"] == d["changed_count"] == 0


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


class TestPromotion:
    def test_promoting_supersedes_the_predecessor(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        sys.owner = "changed"
        db.flush()
        v2, _ = contracts.mint(db, sys)
        contracts.approve(db, v2, approved_by="alice")
        contracts.promote(db, v2)
        db.commit()

        assert db.query(AISystemContract).filter_by(version=1).one().status == (
            CONTRACT_SUPERSEDED
        )
        assert db.query(AISystemContract).filter_by(version=2).one().status == (
            CONTRACT_ACTIVE
        )

    def test_a_superseded_contract_is_kept_as_evidence(self, db):
        # It is the only record of what the rules were while historical runs
        # executed; deleting it would rewrite the past.
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        sys.owner = "changed"
        db.flush()
        v2, _ = contracts.mint(db, sys)
        contracts.approve(db, v2, approved_by="alice")
        contracts.promote(db, v2)
        db.commit()
        assert db.query(AISystemContract).count() == 2

    def test_only_one_contract_is_active(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        sys.owner = "changed"
        db.flush()
        v2, _ = contracts.mint(db, sys)
        contracts.approve(db, v2, approved_by="alice")
        contracts.promote(db, v2)
        db.commit()
        assert (
            db.query(AISystemContract).filter_by(status=CONTRACT_ACTIVE).count() == 1
        )

    def test_a_promoted_contract_cannot_be_promoted_again(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        with pytest.raises(contracts.ContractError):
            contracts.promote(db, v1)

    def test_a_rejected_contract_cannot_be_promoted(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.reject(db, v1, reason="too broad")
        assert v1.status == CONTRACT_REJECTED
        with pytest.raises(contracts.ContractError):
            contracts.promote(db, v1)

    def test_an_unapproved_contract_cannot_be_promoted(self, db):
        # The active contract is the one nobody may have signed otherwise:
        # the lifecycle gate checks `approver_recorded`, but promotion is
        # its own write path and used to take any draft.
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        assert not v1.is_approved
        with pytest.raises(contracts.ContractError, match="not been approved"):
            contracts.promote(db, v1)
        assert v1.status == CONTRACT_DRAFT
        assert v1.promoted_at is None
        assert contracts.current_contract(db, "t1", "sys") is None

    def test_approval_is_recorded_on_the_contract(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        assert v1.approved_by == "alice" and v1.approved_at is not None
        assert v1.is_approved


class TestConformance:
    def test_a_freshly_promoted_contract_is_not_drifted(self, db):
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        db.commit()
        assert contracts.is_config_drifted(db, sys) is False

    def test_changing_config_after_promotion_is_drift(self, db):
        # This is the conformance question, and it is answerable only
        # because contracts are generated rather than authored.
        sys = make_system(db)
        v1, _ = contracts.mint(db, sys)
        contracts.approve(db, v1, approved_by="alice")
        contracts.promote(db, v1)
        db.commit()
        bind_model(db, "sys", "new-model")
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        assert contracts.is_config_drifted(db, sys) is True

    def test_a_system_with_no_active_contract_is_not_reported_as_drifted(self, db):
        # "Never pinned" is a different state from "changed since pinning",
        # and conflating them would flag the entire fleet on day one.
        sys = make_system(db)
        assert contracts.is_config_drifted(db, sys) is False


# ---------------------------------------------------------------------------
# The lifecycle gate
# ---------------------------------------------------------------------------


class TestGateRequirements:
    def test_promotion_to_approved_needs_contract_owner_and_use(self, db):
        sys = make_system(db, lifecycle_stage="proposed")
        assert gate.required_keys(sys, "approved") == [
            gate.REQ_CONTRACT_MINTED,
            gate.REQ_OWNER_SET,
            gate.REQ_INTENDED_USE,
        ]

    def test_high_risk_always_adds_human_approval(self, db):
        sys = make_system(db, lifecycle_stage="proposed", eu_ai_act_risk_tier="high")
        assert gate.REQ_HUMAN_APPROVAL in gate.required_keys(sys, "approved")

    def test_a_tenant_policy_cannot_remove_the_high_risk_floor(self, db):
        # A configurable control that can be configured away is decoration.
        sys = make_system(db, lifecycle_stage="proposed", eu_ai_act_risk_tier="high")
        keys = gate.required_keys(
            sys, "approved", tenant_policy={"proposed->approved": []}
        )
        # Both floors survive an emptying override: the named human, and
        # (a146_ai_system_evidence) the impact assessment at the approval step.
        assert keys == [gate.REQ_HUMAN_APPROVAL, gate.REQ_IMPACT_ASSESSMENT]

    def test_a_tenant_policy_can_relax_the_rest(self, db):
        sys = make_system(db, lifecycle_stage="proposed")
        keys = gate.required_keys(
            sys, "approved", tenant_policy={"proposed->approved": ["owner_set"]}
        )
        assert keys == [gate.REQ_OWNER_SET]

    def test_going_backwards_is_always_allowed(self, db):
        # A gate that traps a system in `active` is a gate people route
        # around.
        sys = make_system(db, lifecycle_stage="active")
        assert gate.required_keys(sys, "approved") == []

    def test_skipping_a_stage_is_refused(self, db):
        # Otherwise the way to bypass every gate is to jump two stages.
        sys = make_system(db, lifecycle_stage="proposed")
        assert gate.required_keys(sys, "retired") == [gate.REQ_VALID_TRANSITION]
        assert not gate.is_known_transition("proposed", "retired")

    def test_a_no_op_transition_is_known(self, db):
        assert gate.is_known_transition("active", "active")


class TestGateEvaluation:
    def _ready_system(self, db):
        sys = make_system(
            db, lifecycle_stage="proposed", owner="team", intended_use="does a thing"
        )
        contracts.mint(db, sys)
        db.commit()
        return sys

    def test_a_fully_evidenced_transition_is_allowed(self, db):
        sys = self._ready_system(db)
        assert gate.evaluate(db, sys, "approved").allowed

    def test_every_missing_requirement_is_reported_at_once(self, db):
        # The property this whole module is shaped around: one round trip,
        # not one per requirement.
        sys = make_system(db, lifecycle_stage="proposed")
        decision = gate.evaluate(db, sys, "approved")
        assert not decision.allowed
        assert {r.key for r in decision.unmet} == {
            gate.REQ_CONTRACT_MINTED,
            gate.REQ_OWNER_SET,
            gate.REQ_INTENDED_USE,
        }

    def test_a_refusal_says_what_to_do_not_just_what_failed(self, db):
        sys = make_system(db, lifecycle_stage="proposed")
        decision = gate.evaluate(db, sys, "approved")
        message = decision.refusal_message()
        assert "owner" in message.lower()
        assert all(r.detail for r in decision.unmet), (
            "every unmet requirement needs an actionable detail line"
        )

    def test_satisfied_requirements_are_still_listed(self, db):
        # The UI shows the full checklist, not only the failures.
        sys = make_system(db, lifecycle_stage="proposed", owner="team")
        decision = gate.evaluate(db, sys, "approved")
        assert any(r.satisfied for r in decision.requirements)
        assert len(decision.requirements) == 3

    def test_a_stale_contract_does_not_satisfy_the_requirement(self, db):
        sys = self._ready_system(db)
        latest = contracts.latest_contract(db, "t1", "sys")
        contracts.approve(db, latest, approved_by="alice")
        contracts.promote(db, latest)
        db.commit()
        bind_model(db, "sys", "added-later")
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        decision = gate.evaluate(db, sys, "approved")
        assert not decision.allowed
        assert any("changed since" in (r.detail or "") for r in decision.unmet)

    def test_activation_needs_an_approver_and_a_liveness_decision(self, db):
        sys = make_system(
            db, lifecycle_stage="approved", owner="team", intended_use="x"
        )
        contracts.mint(db, sys)
        db.commit()
        decision = gate.evaluate(db, sys, "active")
        assert {r.key for r in decision.unmet} == {
            gate.REQ_APPROVER_RECORDED,
            gate.REQ_LIVENESS_SET,
        }

    def test_a_disabled_liveness_expectation_counts_as_a_decision(self, db):
        # What the gate refuses is the *absence* of a decision. A disabled
        # row is the explicit opt-out.
        sys = make_system(
            db, lifecycle_stage="approved", owner="team", intended_use="x"
        )
        contract, _ = contracts.mint(db, sys)
        contracts.approve(db, contract, approved_by="alice")
        db.add(
            AISystemLivenessExpectation(
                id="lv-1", tenant_id="t1", ai_system_id="sys",
                mode="on_demand", enabled=False,
            )
        )
        db.commit()
        assert gate.evaluate(db, sys, "active").allowed

    def test_deprecation_needs_a_successor_decision(self, db):
        sys = make_system(db, lifecycle_stage="active")
        assert not gate.evaluate(db, sys, "deprecated").allowed
        assert gate.evaluate(db, sys, "deprecated", successor_id="sys-2").allowed
        assert gate.evaluate(db, sys, "deprecated", no_successor=True).allowed

    def test_retirement_needs_a_quiet_period(self, db):
        sys = make_system(db, lifecycle_stage="deprecated")
        db.add(
            AISystemRun(
                id="r1", tenant_id="t1", root_task_id="r1", ai_system_id="sys",
                started_at=datetime.now(timezone.utc) - timedelta(days=1),
                terminal_state="completed", finalized_by="idle",
            )
        )
        db.commit()
        decision = gate.evaluate(db, sys, "retired")
        assert not decision.allowed
        assert "break a live caller" in decision.unmet[0].detail

    def test_a_long_silent_system_may_retire(self, db):
        sys = make_system(db, lifecycle_stage="deprecated")
        db.add(
            AISystemRun(
                id="r1", tenant_id="t1", root_task_id="r1", ai_system_id="sys",
                started_at=datetime.now(timezone.utc) - timedelta(days=400),
                terminal_state="completed", finalized_by="idle",
            )
        )
        db.commit()
        assert gate.evaluate(db, sys, "retired").allowed

    def test_unapproved_widening_blocks_an_active_config_change(self, db):
        sys = make_system(db, lifecycle_stage="active", owner="team", intended_use="x")
        contracts.mint(db, sys)
        bind_model(db, "sys", "new-power")
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        contracts.mint(db, sys)
        db.commit()
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("widening" in (r.detail or "").lower() for r in decision.unmet)

    def test_approving_the_widening_unblocks_it(self, db):
        sys = make_system(db, lifecycle_stage="active", owner="team", intended_use="x")
        contracts.mint(db, sys)
        bind_model(db, "sys", "new-power")
        db.expire_all()
        sys = db.query(ResourceGroup).filter_by(id="sys").one()
        second, _ = contracts.mint(db, sys)
        contracts.approve(db, second, approved_by="alice")
        db.commit()
        assert gate.evaluate(db, sys, "active").allowed


# ---------------------------------------------------------------------------
# The replay gate
# ---------------------------------------------------------------------------


class TestReplayGate:
    """Three states, and the distinction between them is the whole point.

    "No suite bound", "bound but not runnable" and "bound and passed" are
    different facts, and collapsing any two of them produces a gate that
    either blocks every system on day one or waves through the least
    testable ones.
    """

    def _ready(self, db, **suite_kwargs):
        sys = make_system(
            db, lifecycle_stage="approved", owner="team", intended_use="x"
        )
        contract, _ = contracts.mint(db, sys)
        contracts.approve(db, contract, approved_by="alice")
        db.add(
            AISystemLivenessExpectation(
                id="lv-1", tenant_id="t1", ai_system_id="sys",
                mode="on_demand", enabled=True,
            )
        )
        if suite_kwargs:
            db.add(
                AISystemReplaySuite(
                    id="suite-1", tenant_id="t1", ai_system_id="sys",
                    name="prod", **suite_kwargs
                )
            )
            sys.replay_suite_id = "suite-1"
        db.commit()
        return sys

    def _run(self, db, **kwargs):
        base = dict(
            id="rr-1", tenant_id="t1", suite_id="suite-1", ai_system_id="sys",
            status=REPLAY_COMPLETED, pass_rate=1.0, critical_outcome_changed=0,
        )
        base.update(kwargs)
        db.add(AISystemReplayRun(**base))
        db.commit()

    def test_no_suite_bound_does_not_block(self, db):
        # Requiring a suite would block every system before anyone has
        # recorded traffic to build one from.
        sys = self._ready(db)
        assert gate.evaluate(db, sys, "active").allowed

    def test_but_the_requirement_is_still_listed(self, db):
        # "Nothing to replay" and "replay passed" are different facts; the
        # checklist must not go silent on the difference.
        sys = self._ready(db)
        decision = gate.evaluate(db, sys, "active")
        assert any(r.key == gate.REQ_REPLAY_PASSED for r in decision.requirements)

    def test_a_bound_suite_that_never_ran_blocks(self, db):
        sys = self._ready(db, pass_threshold=0.9)
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("never been run" in (r.detail or "") for r in decision.unmet)

    def test_a_passing_run_satisfies_it(self, db):
        sys = self._ready(db, pass_threshold=0.9)
        self._run(db, pass_rate=0.95)
        assert gate.evaluate(db, sys, "active").allowed

    def test_a_run_below_threshold_blocks_with_the_number(self, db):
        sys = self._ready(db, pass_threshold=0.9)
        self._run(db, pass_rate=0.7)
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("70%" in (r.detail or "") for r in decision.unmet)

    def test_one_broken_critical_case_blocks_a_high_pass_rate(self, db):
        sys = self._ready(db, pass_threshold=0.9)
        self._run(db, pass_rate=0.98, critical_outcome_changed=1)
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("critical case" in (r.detail or "") for r in decision.unmet)

    def test_an_unreplayable_suite_is_not_a_pass(self, db):
        # Treating it as one would let the least testable systems through
        # the strictest gate.
        sys = self._ready(
            db, replayable=False, not_replayable_reason="depends on external memory"
        )
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("not replayable" in (r.detail or "") for r in decision.unmet)
        assert any("external memory" in (r.detail or "") for r in decision.unmet)

    def test_a_disabled_suite_blocks_rather_than_passing(self, db):
        sys = self._ready(db, enabled=False)
        assert not gate.evaluate(db, sys, "active").allowed

    def test_a_dangling_suite_reference_blocks(self, db):
        sys = self._ready(db)
        sys.replay_suite_id = "gone"
        db.commit()
        decision = gate.evaluate(db, sys, "active")
        assert not decision.allowed
        assert any("no longer exists" in (r.detail or "") for r in decision.unmet)

    def test_the_most_recent_run_is_the_one_that_counts(self, db):
        from datetime import datetime, timedelta, timezone

        sys = self._ready(db, pass_threshold=0.9)
        now = datetime.now(timezone.utc)
        self._run(db, id="rr-old", pass_rate=1.0, started_at=now - timedelta(days=2))
        self._run(db, id="rr-new", pass_rate=0.2, started_at=now)
        assert not gate.evaluate(db, sys, "active").allowed


class TestContractPinsGovernanceContent:
    """The contract must pin what a control DOES, not just that it exists.

    Regression: guardrails entered the closure as an id list, so editing a
    guardrail's rules — turning PII redaction off, say — changed nothing in
    the hash. `config_drifted` stayed false and an approver's diff was blank
    through the most safety-relevant class of change there is. Same for a
    guardrail bound to the *parent*: it governs this system at runtime
    (governance inheritance is unconditional) but was absent from the
    contract entirely.
    """

    def _guardrail(self, db, gid="g1", tenant="t1", **cols):
        from app.database.models.guardrail_config import GuardrailConfig

        g = GuardrailConfig(id=gid, tenant_id=tenant, name=gid, **cols)
        db.add(g)
        db.flush()
        return g

    def _bind(self, db, group_id, guardrail_id):
        from app.database.models.guardrail_config import resource_group_guardrails

        db.execute(
            resource_group_guardrails.insert().values(
                resource_group_id=group_id, guardrail_config_id=guardrail_id
            )
        )
        db.flush()

    def test_editing_guardrail_content_changes_the_hash(self, db):
        from app.services.ai_system_closure import hash_closure as closure_hash, resolve_closure

        sys = make_system(db, owner="team", intended_use="x")
        g = self._guardrail(db, enabled=True, pii_detection_enabled=True)
        self._bind(db, "sys", "g1")
        before = closure_hash(resolve_closure(db, sys))

        # The canonical nightmare edit: the config keeps its id and name,
        # and PII detection quietly goes off.
        g.pii_detection_enabled = False
        db.flush()
        db.expire_all()
        after = closure_hash(resolve_closure(db, sys))
        assert before != after, (
            "a guardrail's content changed but the contract hash did not — "
            "the contract is naming controls, not pinning them"
        )

    def test_a_parents_guardrail_is_in_the_childs_contract(self, db):
        from app.services.ai_system_closure import resolve_closure

        make_system(db, gid="parent")
        child = make_system(db, gid="child", parent="parent")
        self._guardrail(db, gid="pg")
        self._bind(db, "parent", "pg")

        closure = resolve_closure(db, child)
        rails = closure["composition"]["guardrails"]
        assert any(r["id"] == "pg" and r["inherited"] for r in rails), (
            "governance flows down unconditionally; a contract that omits a "
            "parent-bound guardrail certifies less than what is enforced"
        )

    def test_response_policies_are_part_of_the_contract(self, db):
        from app.database.models.ai_system_response_policy import (
            AISystemResponsePolicy,
        )
        from app.services.ai_system_closure import hash_closure as closure_hash, resolve_closure

        sys = make_system(db, owner="team", intended_use="x")
        before = closure_hash(resolve_closure(db, sys))

        db.add(
            AISystemResponsePolicy(
                id="pol-1", tenant_id="t1", ai_system_id="sys",
                name="p", enabled=True, signal_kind="drift",
                severity_at_least="high",
                actions=[{"kind": "notify", "targets": ["a@b.c"]}],
            )
        )
        db.flush()
        db.expire_all()
        after = closure_hash(resolve_closure(db, sys))
        assert before != after, (
            "response policies are part of the contract — arming an "
            "automatic actuator must be a contract change"
        )

    def test_audit_bookkeeping_does_not_move_the_hash(self, db):
        from datetime import datetime, timezone

        from app.services.ai_system_closure import hash_closure as closure_hash, resolve_closure

        sys = make_system(db, owner="team", intended_use="x")
        g = self._guardrail(db)
        self._bind(db, "sys", "g1")
        before = closure_hash(resolve_closure(db, sys))

        g.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
        db.flush()
        db.expire_all()
        after = closure_hash(resolve_closure(db, sys))
        assert before == after, (
            "who touched a row and when is not a control change; a hash that "
            "moves on bookkeeping makes every diff noisy and teaches "
            "approvers to stop reading"
        )



# ---------------------------------------------------------------------------
# Agent grants in the closure
# ---------------------------------------------------------------------------


def _agent_with_grants(db, group_id, agent_id="agent-1", grants=()):
    db.add(
        AgentIdentity(
            id=agent_id, tenant_id="t1", name=agent_id, enabled=True,
            status="active", default_deny=True, enforcement_mode="enforce",
        )
    )
    db.add(
        ResourceGroupAgentMember(
            tenant_id="t1", agent_id=agent_id, group_id=group_id, role="member"
        )
    )
    db.flush()
    for i, (action_type, target, effect) in enumerate(grants):
        db.add(
            AgentGrant(
                id=f"grant-{agent_id}-{i}", tenant_id="t1", agent_id=agent_id,
                action_type=action_type, target=target, effect=effect,
                enabled=True,
            )
        )
    db.flush()


class TestAgentGrantsInClosure:
    """What an agent may DO belongs in the contract.

    Identities were already covered; the grants that decide allow / deny /
    approval on every action were not, so an operator could relax an agent
    from `approval_required` to `allow` and nothing would drift, mint, or
    need re-approving.
    """

    def test_a_system_without_agents_is_byte_for_byte_unchanged(self, db):
        """The key must not appear unconditionally — that would rewrite the
        canonical JSON of every contract in the fleet on upgrade day."""
        sys = make_system(db, "sys-none")
        closure = resolve_closure(db, sys)
        assert "agent_grants" not in closure

    def test_agents_without_grants_do_not_add_the_key_either(self, db):
        sys = make_system(db, "sys-bare")
        _agent_with_grants(db, "sys-bare", "agent-bare", grants=())
        assert "agent_grants" not in resolve_closure(db, sys)

    def test_grants_are_captured_with_their_effect(self, db):
        sys = make_system(db, "sys-a")
        _agent_with_grants(db, "sys-a", "agent-a", grants=[
            ("mcp_tool", "claims_lookup", "allow"),
            ("mcp_tool", "claims_update_queue", "approval_required"),
        ])
        grants = resolve_closure(db, sys)["agent_grants"]
        assert [(g["action_type"], g["target"], g["effect"]) for g in grants] == [
            ("mcp_tool", "claims_lookup", "allow"),
            ("mcp_tool", "claims_update_queue", "approval_required"),
        ]

    def test_relaxing_an_effect_moves_the_hash(self, db):
        sys = make_system(db, "sys-b")
        _agent_with_grants(db, "sys-b", "agent-b", grants=[
            ("mcp_tool", "pay", "approval_required"),
        ])
        before = hash_closure(resolve_closure(db, sys))
        db.query(AgentGrant).filter(AgentGrant.id == "grant-agent-b-0").update(
            {"effect": "allow"}
        )
        db.flush()
        assert hash_closure(resolve_closure(db, sys)) != before

    def test_disabling_a_grant_moves_the_hash(self, db):
        sys = make_system(db, "sys-c")
        _agent_with_grants(db, "sys-c", "agent-c", grants=[
            ("mcp_tool", "pay", "allow"),
        ])
        before = hash_closure(resolve_closure(db, sys))
        db.query(AgentGrant).filter(AgentGrant.id == "grant-agent-c-0").update(
            {"enabled": False}
        )
        db.flush()
        assert hash_closure(resolve_closure(db, sys)) != before

    def test_the_hash_does_not_depend_on_the_clock(self, db):
        """An expiring grant must not silently flip a system to drifted.

        Filtering expired grants would make the closure a function of
        `now()`: a grant lapsing overnight would report config drift with
        nobody having changed anything, and nothing an operator could do to
        explain it. The expiry is carried as a term instead.
        """
        from datetime import datetime, timedelta, timezone

        sys = make_system(db, "sys-d")
        _agent_with_grants(db, "sys-d", "agent-d", grants=[
            ("mcp_tool", "pay", "allow"),
        ])
        past = datetime.now(timezone.utc) - timedelta(days=1)
        db.query(AgentGrant).filter(AgentGrant.id == "grant-agent-d-0").update(
            {"expires_at": past}
        )
        db.flush()
        grants = resolve_closure(db, sys)["agent_grants"]
        assert len(grants) == 1, "an expired grant is still a term of the contract"
        assert grants[0]["expires_at"] is not None
        assert hash_closure(resolve_closure(db, sys)) == hash_closure(
            resolve_closure(db, sys)
        )

    def test_insertion_order_does_not_change_the_hash(self, db):
        a = make_system(db, "sys-e")
        _agent_with_grants(db, "sys-e", "agent-e", grants=[
            ("mcp_tool", "b", "allow"), ("mcp_tool", "a", "allow"),
        ])
        first = hash_closure(resolve_closure(db, a))
        assert first == hash_closure(resolve_closure(db, a))
        grants = resolve_closure(db, a)["agent_grants"]
        assert [g["target"] for g in grants] == ["a", "b"]


class TestAgentGrantDiff:
    """A widened grant is the line an approver must not miss."""

    def _closure(self, grants):
        return {"agent_grants": [
            {"agent_id": "agent-1", "agent_name": "claims-worker",
             "action_type": a, "target": t, "effect": e, "constraints": c,
             "expires_at": None}
            for a, t, e, c in grants
        ]}

    def test_relaxing_approval_to_allow_is_widened(self):
        before = self._closure([("mcp_tool", "pay", "approval_required", None)])
        after = self._closure([("mcp_tool", "pay", "allow", None)])
        d = contracts.diff_closures(before, after)
        assert d["has_widened_permissions"] is True
        assert any(w.get("kind") == "agent_grant" for w in d["widened"])

    def test_tightening_allow_to_approval_is_narrowed(self):
        before = self._closure([("mcp_tool", "pay", "allow", None)])
        after = self._closure([("mcp_tool", "pay", "approval_required", None)])
        d = contracts.diff_closures(before, after)
        assert d["has_widened_permissions"] is False
        assert any(n.get("kind") == "agent_grant" for n in d["narrowed"])

    def test_a_new_allow_is_widened_and_a_revoked_one_is_narrowed(self):
        d = contracts.diff_closures({}, self._closure([("a2a_call", "peer", "allow", None)]))
        assert d["has_widened_permissions"] is True
        d2 = contracts.diff_closures(self._closure([("a2a_call", "peer", "allow", None)]), {})
        assert d2["has_widened_permissions"] is False
        assert d2["narrowed_count"] == 1

    def test_a_new_deny_does_not_read_as_widening(self):
        d = contracts.diff_closures({}, self._closure([("mcp_tool", "shell", "deny", None)]))
        assert d["has_widened_permissions"] is False

    def test_constraint_changes_are_reported_not_swallowed(self):
        before = self._closure([("a2a_call", "peer", "allow", {"max_delegation_depth": 1})])
        after = self._closure([("a2a_call", "peer", "allow", {"max_delegation_depth": 3})])
        d = contracts.diff_closures(before, after)
        assert d["narrowed_count"] == 1
