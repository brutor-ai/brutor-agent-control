# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a135_contracts_autonomy — migration suite

The load-bearing part is the **partial unique index**. "One active contract
per system" is enforced by the database rather than by the promotion code
path, because promotion is a read-then-write and that is exactly the shape
that races. Two rows claiming `active` would make the question every audit
turns on — *which rules governed this run?* — unanswerable, and nothing
would error at the time.

The rest is column parity, which matters for the usual reason: fresh
installs run `create_all` from the model and never execute this file, so a
divergence produces a table that only exists correctly on one of the two
paths.
"""

from __future__ import annotations


import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.agent_identity import AgentIdentity
from app.database.models.ai_system_contract import (
    AUTONOMY_LEVELS,
    AUTONOMY_RANK,
    CONTRACT_ACTIVE,
    CONTRACT_DRAFT,
    CONTRACT_STATUSES,
    CONTRACT_SUPERSEDED,
    DEFAULT_AUTONOMY_LEVEL,
    AISystemContract,
    canonical_json,
    hash_closure,
    resolve_autonomy,
)
from app.database.models.resource_group import ResourceGroup


class TestOneActiveContract:

    def test_it_is_declared_on_the_model_too(self):
        idx = next(
            (
                i
                for i in AISystemContract.__table__.indexes
                if i.name == "uq_ai_system_contracts_one_active"
            ),
            None,
        )
        assert idx is not None, "fresh installs build indexes from the model"
        assert idx.unique

    def test_two_active_contracts_are_actually_rejected(self):
        """The behavioural half, on the fresh-install (`create_all`) path."""
        engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=engine, tables=[AISystemContract.__table__])
        session = sessionmaker(bind=engine)()

        def contract(cid: str, version: int, status: str) -> AISystemContract:
            return AISystemContract(
                id=cid,
                tenant_id="t1",
                ai_system_id="sys-1",
                version=version,
                contract_hash="sha256:" + "a" * 64,
                resolved_closure={"schema_version": 1},
                status=status,
            )

        session.add(contract("c-1", 1, CONTRACT_ACTIVE))
        session.commit()
        session.add(contract("c-2", 2, CONTRACT_ACTIVE))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # A superseded predecessor alongside one active is the normal state.
        session.add(contract("c-3", 3, CONTRACT_SUPERSEDED))
        session.add(contract("c-4", 4, CONTRACT_DRAFT))
        session.commit()
        assert session.query(AISystemContract).count() == 3
        session.close()


class TestHashing:
    def test_key_order_does_not_change_the_hash(self):
        # Two resolutions of the same configuration must be byte-identical
        # or every sweep looks like a config change.
        assert hash_closure({"a": 1, "b": 2}) == hash_closure({"b": 2, "a": 1})

    def test_a_real_difference_changes_the_hash(self):
        assert hash_closure({"a": 1}) != hash_closure({"a": 2})

    def test_nested_order_does_not_matter_either(self):
        left = {"x": {"p": 1, "q": [1, 2]}, "y": 3}
        right = {"y": 3, "x": {"q": [1, 2], "p": 1}}
        assert hash_closure(left) == hash_closure(right)

    def test_list_order_does_matter(self):
        # Lists in a closure are ordered deliberately by the resolver; two
        # different orders would mean the resolver was nondeterministic,
        # which is a bug worth surfacing rather than hashing away.
        assert hash_closure({"a": [1, 2]}) != hash_closure({"a": [2, 1]})

    def test_the_hash_is_prefixed(self):
        h = hash_closure({})
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_canonical_form_is_compact_and_sorted(self):
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_non_ascii_survives_rather_than_being_escaped(self):
        assert canonical_json({"k": "café"}) == '{"k":"café"}'

    def test_an_unserializable_value_is_refused_not_coerced(self):
        # Silently stringifying an object would make two different objects
        # hash the same.
        with pytest.raises(TypeError):
            canonical_json({"k": object()})

    def test_the_model_refuses_a_malformed_hash(self):
        with pytest.raises(ValueError):
            AISystemContract(contract_hash="deadbeef")


class TestAutonomyResolution:
    def test_min_wins(self):
        assert resolve_autonomy("autonomous", "restricted") == "restricted"

    def test_an_actor_cannot_loosen_its_system(self):
        assert resolve_autonomy("restricted", "autonomous") == "restricted"

    def test_unset_levels_do_not_vote(self):
        assert resolve_autonomy("restricted", None, None) == "restricted"

    def test_an_entirely_unset_chain_uses_the_default(self):
        assert resolve_autonomy(None, None) == DEFAULT_AUTONOMY_LEVEL
        assert resolve_autonomy() == DEFAULT_AUTONOMY_LEVEL

    def test_the_default_preserves_pre_column_behaviour(self):
        # Defaulting to approval_required would silently interpose a human
        # in every side-effecting call the moment the column landed.
        assert DEFAULT_AUTONOMY_LEVEL == "autonomous"

    def test_an_unknown_level_is_read_as_suspended(self):
        # As the gateway's `autonomy::resolve` reads it: a level it does not
        # know (a typo, one a newer release added) is the most restrictive.
        assert resolve_autonomy("restricted", "mostly_autonomous") == "suspended"
        assert resolve_autonomy("autonomous", "") == "suspended"

    def test_the_model_validator_rejects_an_unknown_level(self):
        with pytest.raises(ValueError):
            ResourceGroup(id="x", tenant_id="t", name="x", autonomy_level="freewheeling")
        with pytest.raises(ValueError):
            AgentIdentity(id="a", tenant_id="t", name="a", autonomy_level="freewheeling")


class TestWireContractWithRust:
    """Mirrored in `brutor-gateway-core/src/services/autonomy.rs`."""

    def test_levels(self):
        assert AUTONOMY_LEVELS == {
            "autonomous",
            "approval_required",
            "restricted",
            "suspended",
        }

    def test_ranking_is_restrictive_first(self):
        # The Rust side derives `Ord` in this order and resolves with
        # `min()`. If the two disagree the rule silently inverts on one side.
        assert AUTONOMY_RANK["suspended"] < AUTONOMY_RANK["restricted"]
        assert AUTONOMY_RANK["restricted"] < AUTONOMY_RANK["approval_required"]
        assert AUTONOMY_RANK["approval_required"] < AUTONOMY_RANK["autonomous"]

    def test_every_level_is_ranked(self):
        assert set(AUTONOMY_RANK) == AUTONOMY_LEVELS

    def test_contract_statuses(self):
        assert CONTRACT_STATUSES == {"draft", "active", "superseded", "rejected"}
