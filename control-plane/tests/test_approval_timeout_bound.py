# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
An approval timeout is at most seven days, wherever it is stated.

The gateway keeps a hold open for at most seven days and cuts a longer
timeout to that without saying so. A group's and a tenant's approval
window are refused above seven days when they are saved; the timeout of
a policy or of a capability is held to the same bound, so that what is
stored is what applies.
"""

from __future__ import annotations

import pytest

from app.database.models import resource_group as rg

SEVEN_DAYS = 7 * 86_400

DOCUMENTS = [
    (rg.validate_mcp_governance, lambda v: {"approval_timeout_seconds": v}),
    (rg.validate_skill_governance, lambda v: {"approval_timeout_seconds": v}),
    (rg.validate_a2a_governance, lambda v: {"capability": {"approval_timeout_seconds": v}}),
]


def test_the_bound():
    assert rg.MAX_APPROVAL_TIMEOUT_SECONDS == SEVEN_DAYS == 604_800


@pytest.mark.parametrize("validate, document", DOCUMENTS)
def test_a_policys_timeout_above_seven_days_is_refused(validate, document):
    ok, error = validate(document(SEVEN_DAYS + 1))
    assert ok is False and "604800" in error and "7 days" in error
    assert validate(document(SEVEN_DAYS)) == (True, None)
    assert validate(document(1)) == (True, None)
    for bad in (0, -5, True, "300", 1.5):
        assert validate(document(bad))[0] is False, bad


def test_a_capabilitys_timeout_above_seven_days_is_refused():
    from pydantic import ValidationError

    from app.api.v1.models.admin.resource_group import MCPCapabilityInfo

    base = {"capability_type": "tool", "capability_name": "x", "access": "approval_required"}
    assert MCPCapabilityInfo(**base, approval_timeout_seconds=SEVEN_DAYS).approval_timeout_seconds == SEVEN_DAYS
    for bad in (SEVEN_DAYS + 1, 0):
        with pytest.raises(ValidationError):
            MCPCapabilityInfo(**base, approval_timeout_seconds=bad)
