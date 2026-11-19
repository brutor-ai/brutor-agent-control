# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The identity policy resolver against the cases the core runs.

The core keeps the one copy of the shared cases
(`tests/fixtures/identity_policy_resolver.json`) and runs them against its
resolver; this file runs the same resolver and validation cases against
`app/services/identity_policy.py`, read from the core's committed HEAD
(`tests/core_repo.py`), so the two planes cannot resolve a policy
differently. The writer's Pydantic models are held to the validation cases
as well: they are what the routes accept.
"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.identity_policy import IdentityPolicyDefaultModel, IdentityPolicyModel
from app.constants.identity import CREDENTIAL_LEVELS, POLICY_FIELDS
from app.services import identity_policy as ip
from tests.core_repo import core_repo_path, core_revision

SCHEMA = "brutor/identity-policy-resolver-cases/1"


@pytest.fixture(scope="module")
def cases() -> dict:
    path = core_repo_path() / "tests" / "fixtures" / "identity_policy_resolver.json"
    if not path.exists():
        pytest.fail(f"{path} is not in the core ({core_revision()}): the shared cases cannot run")
    data = json.loads(path.read_text())
    assert data["schema"] == SCHEMA
    return data


class TestVocabulary:
    def test_the_ladder_and_the_members_are_the_cores(self, cases):
        assert tuple(cases["ladder"]) == CREDENTIAL_LEVELS
        assert tuple(cases["policy_fields"]) == POLICY_FIELDS

    def test_the_constants_are_the_cores(self, cases):
        constants = cases["constants"]
        assert constants["development_default"] == ip.DEVELOPMENT_DEFAULT
        assert tuple(constants["production_stages"]) == ip.PRODUCTION_STAGES
        assert constants["production_floor"] == ip.PRODUCTION_FLOOR
        assert tuple(constants["high_risk_tiers"]) == ip.HIGH_RISK_TIERS
        assert constants["high_risk_floor"] == ip.HIGH_RISK_FLOOR
        assert constants["high_risk_production_floor"] == ip.HIGH_RISK_PRODUCTION_FLOOR
        assert constants["token_lifetime_ceiling_s"] == ip.TOKEN_LIFETIME_CEILING_S
        assert constants["default_max_user_key_ttl_days"] == ip.DEFAULT_MAX_USER_KEY_TTL_DAYS


class TestResolverCases:
    def test_there_are_cases(self, cases):
        assert len(cases["resolver_cases"]) >= 24

    def test_every_case(self, cases):
        failures = []
        for case in cases["resolver_cases"]:
            given = case["input"]
            got = ip.resolve(
                tenant_default=given.get("tenant_default"),
                chain=given["chain"],
                attributed_ai_system_id=given.get("attributed_ai_system_id"),
            )
            expect = case["expect"]
            for member in ("effective", "sources", "inherited_floor", "max_user_key_ttl_days", "put_refused_fields"):
                if got[member] != expect[member]:
                    failures.append(f"{case['name']}: {member}: got {got[member]!r}, expected {expect[member]!r}")
        assert not failures, "\n".join(failures)

    def test_the_resolver_does_not_change_its_input(self, cases):
        for case in cases["resolver_cases"]:
            given = copy.deepcopy(case["input"])
            ip.resolve(
                tenant_default=given.get("tenant_default"),
                chain=given["chain"],
                attributed_ai_system_id=given.get("attributed_ai_system_id"),
            )
            assert given == case["input"], case["name"]


class TestValidationCases:
    def test_there_are_cases(self, cases):
        assert len(cases["validation_cases"]) >= 12

    def test_the_writer_rules(self, cases):
        for case in cases["validation_cases"]:
            if case["valid"]:
                ip.validate_policy(case["policy"])
            else:
                with pytest.raises(ip.InvalidIdentityPolicy) as refused:
                    ip.validate_policy(case["policy"])
                assert refused.value.field == case["field"], case["name"]

    def test_the_route_model_refuses_what_the_writer_refuses(self, cases):
        for case in cases["validation_cases"]:
            if case["valid"]:
                IdentityPolicyModel.model_validate(case["policy"])
                continue
            with pytest.raises(ValidationError) as refused:
                IdentityPolicyModel.model_validate(case["policy"])
            fields = {str(e["loc"][0]) for e in refused.value.errors() if e["loc"]}
            assert case["field"] in fields, (case["name"], refused.value.errors())

    def test_the_tenant_default_takes_the_personal_key_ceiling(self, cases):
        policy = dict(cases["validation_cases"][0]["policy"])
        ip.validate_policy_default(dict(policy, max_user_key_ttl_days=30))
        IdentityPolicyDefaultModel.model_validate(dict(policy, max_user_key_ttl_days=30))
        for bad in (None, 0, 3651, "30", True):
            with pytest.raises(ip.InvalidIdentityPolicy) as refused:
                ip.validate_policy_default(dict(policy, max_user_key_ttl_days=bad))
            assert refused.value.field == "max_user_key_ttl_days"
            with pytest.raises(ValidationError):
                IdentityPolicyDefaultModel.model_validate(dict(policy, max_user_key_ttl_days=bad))
        with pytest.raises(ip.InvalidIdentityPolicy) as refused:
            ip.validate_policy_default(policy)
        assert refused.value.field == "max_user_key_ttl_days"


class TestBeyondTheCases:
    def test_a_stored_invalid_policy_reads_as_the_strictest(self):
        chain = [{"id": "rg", "is_ai_system": False, "lifecycle_stage": None, "risk_tier": None,
                  "identity_policy": {"mode": "loose"}}]
        got = ip.resolve(tenant_default=None, chain=chain)
        assert got["effective"] == ip.STRICTEST
        bad_default = {"mode": "audit"}
        got = ip.resolve(tenant_default=bad_default, chain=[])
        assert got["effective"] == ip.STRICTEST
        assert got["max_user_key_ttl_days"] == 1

    def test_an_empty_chain_is_the_development_default(self):
        got = ip.resolve(tenant_default=None, chain=[])
        assert got["effective"] == ip.DEVELOPMENT_DEFAULT
        assert got["put_refused_fields"] == []

    def test_the_platform_service_key_is_outside_the_ladder(self):
        # Nothing a policy says reaches the platform's own credential.
        assert ip.judged_kind("platform_service") is False
        assert ip.judged_kind("human_session") is False
        for kind in ("api_key", "workload_token", "x509_svid", "brutor_token", "anonymous"):
            assert ip.judged_kind(kind) is True

    def test_level_order(self):
        assert ip.level_rank("key") < ip.level_rank("bound_key") < ip.level_rank("workload") < ip.level_rank("workload_pop")
        assert ip.levels_below("workload") == ["key", "bound_key"]
        assert ip.levels_below("key") == []
