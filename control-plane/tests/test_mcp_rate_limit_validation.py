# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for MCP rate-limit structure validation
(`ResourceGroup.validate_mcp_limits_structure` and the
`_validate_mcp_frequency_block` helper in
app/database/models/resource_group.py).

These guard the per-tool MCP rate-limit feature. Both the group-level
`mcp-global-limits` endpoint and the per-server `mcp-servers/{id}/limits`
endpoint run an untyped limits dict through this validator before
persistence, so it is the single structural gate for the token-bucket /
quota / cooldown / per_tool configuration.

The validator is a pure @staticmethod — no DB, no app, no fixtures.

Run: pytest tests/test_mcp_rate_limit_validation.py -v
"""

import pytest

from app.database.models.resource_group import ResourceGroup

validate = ResourceGroup.validate_mcp_limits_structure


# --- valid structures ---------------------------------------------------

def test_none_and_empty_are_valid():
    assert validate(None) == (True, None)
    assert validate({}) == (True, None)


def test_full_frequency_block_valid():
    ok, err = validate({
        "frequency": {
            "max_calls_per_minute": 10,
            "max_calls_per_hour": 100,
            "max_calls_per_day": 1000,
            "burst_limit": 20,
            "cooldown_seconds": 30,
            "daily_warning_percent": 80,
        }
    })
    assert ok, err


def test_per_tool_block_valid():
    ok, err = validate({
        "per_tool": {
            "generate_report": {"max_calls_per_hour": 2},
            "search": {"max_calls_per_minute": 5, "burst_limit": 10},
        }
    })
    assert ok, err


def test_execution_and_data_sections_valid():
    ok, err = validate({
        "execution": {"timeout_seconds": 30, "max_concurrency_per_user": 4},
        "data": {"max_payload_size_mb": 5},
    })
    assert ok, err


def test_minute_le_hour_le_day_accepted():
    ok, err = validate({
        "frequency": {
            "max_calls_per_minute": 1,
            "max_calls_per_hour": 60,
            "max_calls_per_day": 1440,
        }
    })
    assert ok, err


# --- rejected: removed section -----------------------------------------

def test_cost_section_rejected():
    ok, err = validate({"cost": {"daily_limit_usd": 1}})
    assert not ok
    assert "cost" in err


# --- rejected: bad frequency values ------------------------------------

@pytest.mark.parametrize("field", [
    "max_calls_per_minute", "max_calls_per_hour", "max_calls_per_day",
    "burst_limit", "cooldown_seconds",
])
def test_zero_frequency_value_rejected(field):
    ok, err = validate({"frequency": {field: 0}})
    assert not ok
    assert field in err


@pytest.mark.parametrize("field", [
    "max_calls_per_minute", "max_calls_per_hour", "max_calls_per_day",
])
def test_negative_frequency_value_rejected(field):
    ok, _ = validate({"frequency": {field: -1}})
    assert not ok


def test_boolean_frequency_value_rejected():
    # bool is a subclass of int in Python — it must not slip through.
    ok, _ = validate({"frequency": {"max_calls_per_hour": True}})
    assert not ok


def test_daily_warning_percent_out_of_range_rejected():
    ok, _ = validate({"frequency": {"daily_warning_percent": 150}})
    assert not ok


def test_frequency_block_not_a_dict_rejected():
    ok, _ = validate({"frequency": 42})
    assert not ok


# --- rejected: cross-field sanity --------------------------------------

def test_minute_above_hour_rejected():
    ok, err = validate({
        "frequency": {"max_calls_per_minute": 100, "max_calls_per_hour": 10}
    })
    assert not ok
    assert "max_calls_per_minute" in err


def test_hour_above_day_rejected():
    ok, _ = validate({
        "frequency": {"max_calls_per_hour": 1000, "max_calls_per_day": 10}
    })
    assert not ok


# --- rejected: bad per_tool structure ----------------------------------

def test_per_tool_not_a_dict_rejected():
    ok, err = validate({"per_tool": ["generate_report"]})
    assert not ok
    assert "per_tool" in err


def test_per_tool_empty_key_rejected():
    ok, _ = validate({"per_tool": {"   ": {"max_calls_per_hour": 2}}})
    assert not ok


def test_per_tool_bad_value_rejected():
    ok, err = validate({
        "per_tool": {"generate_report": {"max_calls_per_hour": 0}}
    })
    assert not ok
    assert "generate_report" in err


def test_per_tool_cross_field_rejected():
    ok, _ = validate({
        "per_tool": {"x": {"max_calls_per_minute": 99, "max_calls_per_hour": 1}}
    })
    assert not ok
