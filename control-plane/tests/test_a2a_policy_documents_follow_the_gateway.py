# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A policy and limit documents are refused when they are saved if the
gateway could not read them.

The gateway judges an A2A call by the policy document of every level that
states one (the caller's group, each ancestor, the agent card), and refuses
the call when one of them cannot be read: fail closed. A card whose
document was saved unreadable would have every one of its calls refused,
with nothing in the console saying why. The same documents are validated
by the same rule wherever they are saved.
"""

from __future__ import annotations

import pytest

from app.database.models.resource_group import validate_a2a_governance, validate_a2a_limits
from app.services.a2a.card_signing import InvalidCardError, validate_card_json
from tests.test_a2a_card_endpoints import VALID_CARD_BODY, harness  # noqa: F401  (fixture)

UNREADABLE_GOVERNANCE = [
    ["capability"],
    {"capability": "enabled"},
    {"capability": []},
    {"capability": {"default_access": 1}},
    {"capability": {"default_access": "sometimes"}},
    {"capability": {"approval_timeout_seconds": True}},
    {"data": "PII"},
    {"data": {"deny_classifications": "PII"}},
    {"data": {"deny_classifications": [1]}},
    {"remote": ["https://x"]},
    {"remote": {"allowed_globs": "https://x/*"}},
    {"remote": {"allowed_globs": [3]}},
    {"remote": {"allowed_globs": ["https://[x/*"]}},
    {"require_signed": True},
]
READABLE_GOVERNANCE = [
    {},
    {"capability": {"default_access": "approval_required", "approval_timeout_seconds": 900}},
    {"data": {"deny_classifications": ["PII", "PHI"]}, "remote": {"allowed_globs": ["https://*.partner.example/*"]}},
    {"capability": None, "data": None, "remote": None},
]
UNREADABLE_LIMITS = [
    ["invocations"],
    {"invocations": 5},
    {"invocations": {"daily_limit": "many"}},
    {"invocations": {"daily_limit": True}},
    {"invocations": {"daily_limit": -1}},
    {"chain": {"max_delegation_depth": 99}},
    {"frequency": {"rpm": 30}},
    {"frequency": {"max_calls_per_minute": 0}},
    {"frequency": {"max_calls_per_hour": "x"}},
    {"max_delegation_depth": 3},
]
READABLE_LIMITS = [
    {},
    {"invocations": {"daily_limit": 100, "monthly_limit": 2000}},
    {"chain": {"max_delegation_depth": 4}},
    {"frequency": {"max_calls_per_minute": 60, "max_calls_per_hour": 1000, "max_calls_per_day": 10000,
                   "burst_limit": 30, "cooldown_seconds": 60}},
]


@pytest.mark.parametrize("document", UNREADABLE_GOVERNANCE)
def test_a_policy_document_the_gateway_cannot_read_is_refused(document):
    ok, error = validate_a2a_governance(document)
    assert ok is False and error, document


@pytest.mark.parametrize("document", READABLE_GOVERNANCE)
def test_a_policy_document_the_gateway_reads_is_accepted(document):
    assert validate_a2a_governance(document) == (True, None)


@pytest.mark.parametrize("document", UNREADABLE_LIMITS)
def test_a_limits_document_the_gateway_cannot_read_is_refused(document):
    ok, error = validate_a2a_limits(document)
    assert ok is False and error, document


@pytest.mark.parametrize("document", READABLE_LIMITS)
def test_a_limits_document_the_gateway_reads_is_accepted(document):
    assert validate_a2a_limits(document) == (True, None)


def _card(harness, headers) -> str:  # noqa: F811
    r = harness.client.post("/v1/admin/agent-cards", headers=headers, json={"name": "billing", "card_json": VALID_CARD_BODY})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _stored(harness, card_id):  # noqa: F811
    from app.database.models.agent_card import AgentCard

    with harness.Factory() as s:
        card = s.get(AgentCard, card_id)
        return card.tenant_governance, card.tenant_limits


@pytest.mark.parametrize("document", UNREADABLE_GOVERNANCE[:4] + UNREADABLE_GOVERNANCE[-2:])
def test_a_card_is_not_saved_with_a_policy_the_gateway_cannot_read(harness, document):  # noqa: F811
    headers = harness.login("t1")
    card_id = _card(harness, headers)
    r = harness.client.patch(f"/v1/admin/agent-cards/{card_id}", headers=headers, json={"tenant_governance": document})
    assert r.status_code in (400, 422), r.text
    assert "tenant_governance" in r.text
    assert _stored(harness, card_id) == (None, None)


@pytest.mark.parametrize("document", UNREADABLE_LIMITS[:3] + UNREADABLE_LIMITS[-4:])
def test_a_card_is_not_saved_with_limits_the_gateway_cannot_read(harness, document):  # noqa: F811
    headers = harness.login("t1")
    card_id = _card(harness, headers)
    r = harness.client.patch(f"/v1/admin/agent-cards/{card_id}", headers=headers, json={"tenant_limits": document})
    assert r.status_code in (400, 422), r.text
    assert "tenant_limits" in r.text
    assert _stored(harness, card_id) == (None, None)


def test_a_card_is_saved_with_documents_the_gateway_reads(harness):  # noqa: F811
    headers = harness.login("t1")
    card_id = _card(harness, headers)
    body = {"tenant_governance": READABLE_GOVERNANCE[2], "tenant_limits": READABLE_LIMITS[3]}
    r = harness.client.patch(f"/v1/admin/agent-cards/{card_id}", headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert _stored(harness, card_id) == (READABLE_GOVERNANCE[2], READABLE_LIMITS[3])


@pytest.mark.parametrize("value", [0, -3, 1.5, "60", True])
@pytest.mark.parametrize("key", ["skills", "capabilities"])
def test_a_capabilitys_hourly_rate_is_a_positive_whole_number(key, value):
    card = {**VALID_CARD_BODY, "skills": None, "capabilities": None}
    card = {k: v for k, v in card.items() if v is not None}
    card[key] = [{"name": "lookup", "id": "lookup", "rate_limit_per_hour": value}]
    with pytest.raises(InvalidCardError, match="rate_limit_per_hour"):
        validate_card_json(card)


def test_a_positive_hourly_rate_and_none_are_accepted():
    for value in (1, 600, None):
        card = {**VALID_CARD_BODY, "skills": [{"name": "lookup", "id": "lookup", "rate_limit_per_hour": value}]}
        validate_card_json(card)
