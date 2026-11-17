# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A field with a default is optional in the API description.

A client generated from the description must not be made to send what the
server fills in. The description lists a property under `required` only
when the model has no default for it, and a model that is both sent and
answered has one schema (no `-Input` / `-Output` pair, in the second of
which a defaulted field would be required).

A generator may still decide by itself that a property with a `default`
is never absent (openapi-typescript does so unless it is run with
`--default-non-nullable=false`): that is the generator's setting, not
this document's.
"""

from __future__ import annotations

import json

import pytest

from tests.openapi_documents import COMMUNITY_SNAPSHOT_PATH


# The Enterprise document: `enterprise/tests/test_enterprise_openapi_document.py`.
@pytest.mark.parametrize("recording", [COMMUNITY_SNAPSHOT_PATH], ids=["community"])
def test_no_schema_requires_a_property_that_has_a_default(recording):
    schemas = json.loads(recording.read_text(encoding="utf-8"))["components"]["schemas"]
    assert len(schemas) > 700
    required_with_default = sorted(
        (name, prop)
        for name, schema in schemas.items()
        for prop in schema.get("required", [])
        if "default" in schema.get("properties", {}).get(prop, {})
    )
    assert required_with_default == []
    assert [name for name in schemas if name.endswith(("-Input", "-Output"))] == []


def test_a_request_model_with_defaults_is_described_with_them_optional():
    from app.api.v1.models.admin.llm_routing_group import RoutingGroupCreate
    from app.api.v1.models.admin.proxy_log import LogQueryRequest

    schema = RoutingGroupCreate.model_json_schema()
    assert schema["required"] == ["id", "name", "mode"]
    assert schema["properties"]["cooldown_time"]["default"] == 60
    assert "required" not in LogQueryRequest.model_json_schema()
