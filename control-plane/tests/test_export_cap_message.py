# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A download that is too large says what the administrator can do.

The refusal names the two numbers and what narrows the download. It
offers a scheduled export only where there is one: proxy log rows can be
delivered by an audit-export destination; the admin audit log cannot.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services import admin_export


def _refusal(resource: str) -> str:
    with pytest.raises(HTTPException) as refused:
        admin_export.assert_within_cap(admin_export.EXPORT_ROW_CAP + 1, resource=resource)
    assert refused.value.status_code == 413
    return refused.value.detail


@pytest.mark.parametrize("resource", ["proxy log rows", "audit log rows"])
def test_it_says_how_many_and_how_to_narrow(resource):
    detail = _refusal(resource)
    assert str(admin_export.EXPORT_ROW_CAP + 1) in detail and str(admin_export.EXPORT_ROW_CAP) in detail
    assert resource in detail
    assert "shorter time range" in detail and "filters" in detail
    # Nothing that does not exist.
    for phrase in ("asynchronous", "pipeline"):
        assert phrase not in detail


def test_proxy_logs_can_also_be_delivered_on_a_schedule():
    assert "audit-export destination" in _refusal("proxy log rows")
    assert "/v1/admin/audit-export-destinations" in _refusal("proxy log rows")


def test_the_admin_audit_log_is_offered_nothing_it_does_not_have():
    assert "destination" not in _refusal("audit log rows")


def test_within_the_cap_nothing_is_refused():
    admin_export.assert_within_cap(admin_export.EXPORT_ROW_CAP, resource="proxy log rows")


def test_the_route_descriptions_do_not_promise_another_export_either():
    from tests.app_factory import create_app

    text = str(create_app().openapi())
    assert "asynchronous export" not in text and "export pipeline" not in text
