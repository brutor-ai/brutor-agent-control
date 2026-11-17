# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a180_rg_sensitive_data — resource_groups.sensitive_data + witnesses.pinned_key TEXT

The privileged-read flag must exist identically on both
install paths (fresh create_all and migrated) because the Rust core reads
the column directly when deciding whether a read-only tool call is sealed,
and it must survive every API layer (ORM → to_dict → Pydantic response
models strip undeclared fields). The witness pin is widened so an rfc3161
TSA certificate PEM fits.
"""

from __future__ import annotations



import app.database.models  # noqa: F401 — registers every model on Base
from app.api.v1.endpoints.admin import evidence as evidence_endpoints
from app.api.v1.models.admin import resource_group as rg_models
from app.database.models.resource_group import ResourceGroup


def test_witness_request_models_accept_a_pem_pin():
    pem = "-----BEGIN CERTIFICATE-----\n" + ("A" * 1400) + "\n-----END CERTIFICATE-----\n"
    body = evidence_endpoints.WitnessCreateRequest(
        kind="rfc3161", url="https://tsa.example", operator_label="TSA", pinned_key=pem
    )
    assert body.pinned_key == pem
    assert evidence_endpoints.WitnessUpdateRequest(pinned_key=pem).pinned_key == pem


def test_to_dict_surfaces_sensitive_data():
    rg = ResourceGroup(id="rg-x", tenant_id="t", name="x", sensitive_data=True)
    assert rg.to_dict()["sensitive_data"] is True
    rg.sensitive_data = None  # transient object before the server default
    assert rg.to_dict()["sensitive_data"] is False
