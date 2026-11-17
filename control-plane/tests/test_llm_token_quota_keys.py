# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The LLM limits validator knows the token quota's input and output sides.

The gateway enforces `tokens.{daily,monthly}_{input,output}_limit` beside
the totals. A value it cannot read is not a limit at all, so a mistyped
one is refused when it is saved rather than stored as a limit that never
applies.
"""

from __future__ import annotations

import pytest

from app.database.models.resource_group import ResourceGroup

KEYS = ["daily_input_limit", "daily_output_limit", "monthly_input_limit", "monthly_output_limit"]


def _validate(tokens):
    return ResourceGroup(id="g", tenant_id="t", name="g", llm_global_limits={"tokens": tokens}).validate_llm_limits()


@pytest.mark.parametrize("key", KEYS + ["daily_limit", "monthly_limit"])
@pytest.mark.parametrize("value", [-1, 1.5, "1000", True, [1]])
def test_a_token_quota_is_a_whole_number_that_is_not_negative(key, value):
    ok, error = _validate({key: value})
    assert ok is False, (key, value)
    assert key in error


@pytest.mark.parametrize("key", KEYS + ["daily_limit", "monthly_limit"])
@pytest.mark.parametrize("value", [0, 1, 250000, None])
def test_a_token_quota_is_accepted(key, value):
    assert _validate({key: value}) == (True, None)


def test_the_keys_are_the_gateways():
    from tests.core_repo import core_source

    limits = core_source("services", "limits.rs").read_text(encoding="utf-8")
    assert 'f("tokens", &format!("{field}_{side}_limit"))' in limits
    assert '("tokin", "input", Measure::InTokens)' in limits and '("tokout", "output", Measure::OutTokens)' in limits
    assert '(QuotaWindow::Day, "daily"), (QuotaWindow::Month, "monthly")' in limits


def test_the_provider_drop_list_is_applied_unless_a_model_says_otherwise():
    """The gateway reads an unset `drop_params` as on; a model created
    without the field is stored on, as its description says."""
    from app.api.v1.models.admin import llm_model
    from app.database.models.llm_model import LLModel

    create = next(
        model for name, model in vars(llm_model).items()
        if isinstance(model, type) and "drop_params" in getattr(model, "model_fields", {})
        and model.model_fields["drop_params"].default is not None
    )
    assert create.model_fields["drop_params"].default is True
    assert "On by default" in create.model_fields["drop_params"].description
    assert LLModel.__table__.c.drop_params.default.arg is True
    from tests.core_repo import core_source

    assert "model.drop_params.unwrap_or(true)" in core_source("services", "providers.rs").read_text(encoding="utf-8")
