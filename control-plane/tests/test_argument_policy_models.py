# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Validates the Pydantic models for argument policies — cross-field
predicate validation, rule shape, and serialization.
"""

import pytest

from app.api.v1.models.admin.argument_policy import (
    ArgumentPolicyCreateRequest,
    ArgumentPolicyRule,
    ArgumentPolicyUpdateRequest,
    PREDICATES_BY_ANALYZER,
)


# ---------------------------------------------------------------------------
# Rule shape
# ---------------------------------------------------------------------------

def test_rule_accepts_deny_if_only():
    r = ArgumentPolicyRule(deny_if="writes")
    assert r.deny_if == "writes"
    assert r.allow_only_if is None


def test_rule_accepts_allow_only_if_only():
    r = ArgumentPolicyRule(allow_only_if="command_in", values=["ls"])
    assert r.allow_only_if == "command_in"


def test_rule_rejects_both_deny_and_allow():
    with pytest.raises(ValueError, match="exactly one"):
        ArgumentPolicyRule(deny_if="writes", allow_only_if="command_in")


def test_rule_rejects_neither_deny_nor_allow():
    with pytest.raises(ValueError, match="exactly one"):
        ArgumentPolicyRule(message="oops")


# ---------------------------------------------------------------------------
# Create request — predicate validation against analyzer
# ---------------------------------------------------------------------------

def _create(**overrides):
    base = dict(
        name="test-policy",
        surface="llm_output_tool_calls",
        target="*",
        argument_key="query",
        analyzer="sql",
        rules=[{"deny_if": "writes", "message": "no writes"}],
    )
    base.update(overrides)
    return ArgumentPolicyCreateRequest(**base)


def test_create_accepts_valid_sql_writes_predicate():
    body = _create()
    assert body.analyzer == "sql"
    assert body.rules[0].deny_if == "writes"


def test_create_rejects_predicate_invalid_for_analyzer():
    with pytest.raises(ValueError, match="not valid for analyzer"):
        _create(rules=[{"deny_if": "host_in_private_range", "message": "x"}])


def test_create_requires_values_for_predicates_that_need_them():
    with pytest.raises(ValueError, match="requires"):
        _create(
            analyzer="shell",
            rules=[{"deny_if": "command_in", "message": "x"}],  # missing values
        )


def test_create_accepts_values_for_command_in():
    body = _create(
        analyzer="shell",
        argument_key="command",
        rules=[{
            "deny_if": "command_in",
            "values": ["rm", "curl"],
            "message": "no destructive commands",
        }],
    )
    assert body.rules[0].values == ["rm", "curl"]


def test_create_accepts_url_predicate_for_url_analyzer():
    body = _create(
        analyzer="url",
        argument_key="url",
        rules=[{"deny_if": "host_in_private_range", "message": "no SSRF"}],
    )
    assert body.analyzer == "url"


def test_predicates_registry_covers_all_analyzers():
    # Sanity check: every analyzer has at least one predicate.
    for analyzer in ["sql", "url", "shell", "path", "json"]:
        assert analyzer in PREDICATES_BY_ANALYZER
        assert len(PREDICATES_BY_ANALYZER[analyzer]) > 0


# ---------------------------------------------------------------------------
# Update request
# ---------------------------------------------------------------------------

def test_update_allows_partial_patch_without_validating_predicates():
    # Only patching `description` — no cross-field check needed.
    body = ArgumentPolicyUpdateRequest(description="new description")
    assert body.description == "new description"
    assert body.analyzer is None
    assert body.rules is None


def test_update_validates_when_both_analyzer_and_rules_change():
    with pytest.raises(ValueError, match="not valid for"):
        ArgumentPolicyUpdateRequest(
            analyzer="url",
            rules=[{"deny_if": "writes", "message": "x"}],  # SQL-only predicate
        )


def test_update_passes_when_both_change_compatibly():
    body = ArgumentPolicyUpdateRequest(
        analyzer="url",
        rules=[{"deny_if": "host_in_private_range", "message": "no SSRF"}],
    )
    assert body.analyzer == "url"


# ---------------------------------------------------------------------------
# Severity / enabled defaults
# ---------------------------------------------------------------------------

def test_severity_defaults_to_deny():
    body = _create()
    assert body.severity == "deny"


def test_enabled_defaults_to_true():
    body = _create()
    assert body.enabled is True


def test_severity_accepts_warn():
    body = _create(severity="warn")
    assert body.severity == "warn"


def test_severity_rejects_unknown_value():
    with pytest.raises(ValueError):
        _create(severity="silent")  # not in Literal["deny","warn"]
