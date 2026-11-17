# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a213_drop_rg_unread_fields — the six routing-group fields nothing reads are gone.

The gateway reads a routing group's strategy, fallbacks, retries, limits and
`cooldown_time`. It never read these six, so the API accepted values that
changed nothing:

    allowed_fails, cooldown_window, enable_pre_call_checks, region_filter,
    min_context_window, timeout (the group-level one)

They are removed from the table, the ORM model, the request and response
models, `to_dict()` and the router settings. A model's own `allowed_fails` and
`timeout` (`llm_model`) are read by the gateway and stay.
"""

from __future__ import annotations


import pytest

from app.api.v1.models.admin.llm_routing_group import (
    RoutingGroupCreate,
    RoutingGroupListItem,
    RoutingGroupResponse,
    RoutingGroupUpdate,
)
from app.database.models.llm_model import LLModel
from app.database.models.llm_routing_group import LLMRoutingGroup

REMOVED = (
    "allowed_fails",
    "cooldown_window",
    "enable_pre_call_checks",
    "region_filter",
    "min_context_window",
    "timeout",
)


@pytest.mark.parametrize("name", REMOVED)
def test_the_table_has_no_such_column(name):
    assert name not in LLMRoutingGroup.__table__.columns


@pytest.mark.parametrize("model", [RoutingGroupCreate, RoutingGroupUpdate, RoutingGroupResponse, RoutingGroupListItem])
@pytest.mark.parametrize("name", REMOVED)
def test_the_api_models_have_no_such_field(model, name):
    assert name not in model.model_fields


def test_the_list_row_has_no_indicator_of_a_removed_field():
    assert "has_pre_call_checks" not in RoutingGroupListItem.model_fields


def test_to_dict_and_the_router_settings_carry_none_of_them():
    group = LLMRoutingGroup(id="rg-1", tenant_id="t1", name="g", enabled=True)
    assert not set(REMOVED) & set(group.to_dict())
    assert not set(REMOVED) & set(group.to_router_settings())


def test_the_fields_the_gateway_reads_stay():
    for name in ("cooldown_time", "disable_cooldowns", "num_retries", "retry_after", "stream_timeout",
                 "fallback_models", "max_fallbacks", "rpm_limit", "tpm_limit", "max_parallel_requests"):
        assert name in LLMRoutingGroup.__table__.columns, name
    # The model's own breaker and timeout are read by the gateway.
    for name in ("allowed_fails", "timeout", "cooldown_time"):
        assert name in LLModel.__table__.columns, name


def test_a_request_naming_a_removed_field_carries_nothing():
    """A removed field is an unknown key like any other: it is not part of
    the request model, so nothing of it reaches the service or the row. An
    update refuses it by name; a creation carries nothing of it."""
    from pydantic import ValidationError

    for name, value in (("allowed_fails", 3), ("cooldown_window", 30), ("enable_pre_call_checks", True),
                        ("region_filter", "eu"), ("min_context_window", 1000), ("timeout", 30.0)):
        with pytest.raises(ValidationError, match=name):
            RoutingGroupUpdate(**{name: value})
        created = RoutingGroupCreate(id="g", name="g", mode="chat", **{name: value}).model_dump()
        assert name not in created
