# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The audit record of a model update holds no credential.

`llm_update` records what changed, old and new. `api_key` was redacted;
`provider_config`, which holds a provider's credential (an AWS secret access
key, a service-account document), was not: the audit log got its old and new
value as stored: the ciphertext.
"""

from __future__ import annotations

import json

import pytest

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.llm_provider import LlmProvider
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness

OLD_SECRET = "old-secret-access-key-SENTINEL"
NEW_SECRET = "new-secret-access-key-SENTINEL"
OLD_KEY = "sk-old-api-key-SENTINEL"
NEW_KEY = "sk-new-api-key-SENTINEL"


class Cipher:
    """Stands for the encryption service: what it returns is what is stored."""

    def encrypt(self, value: str) -> str:
        return "enc:" + value[::-1]

    def decrypt(self, value: str) -> str:
        return value[len("enc:"):][::-1]


@pytest.fixture()
def h():
    # With an encryption service, as a server has: without one a
    # `provider_config` cannot be stored at all.
    cipher = Cipher()
    log = BrutorLogger("model-update-audit")
    manager = LLModelsManager(logger=log, encryption_service=cipher)
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=cipher)], logger_name="model-update-audit"
    )
    # A row with both credentials: Azure (an API key, and a client secret
    # in `provider_config`). A Bedrock row, which this test used, carries
    # no `api_key` any more (`tests/test_cloud_model_save_rules.py`).
    harness.seed(lambda s: s.add(LlmProvider(id="prov-t1", tenant_id="t1", key="azure", display_name="Azure")))
    harness.cipher = cipher
    yield harness
    harness.close()


def _update_record(h) -> dict:
    with h.Factory() as s:
        (row,) = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == "llm_update").all()
        return dict(row.details or {})


def test_the_update_record_holds_neither_credential(h):
    headers = h.headers("t1")
    r = h.client.post(
        "/v1/admin/llms",
        json={
            "name": "m", "provider_id": "prov-t1", "model_name": "gpt-5", "mode": "chat",
            "api_url": "https://acme.openai.azure.com",
            "api_key": OLD_KEY,
            "provider_config": {"azure_client_id": "AKIAOLD", "azure_client_secret": OLD_SECRET},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    r = h.client.patch(
        f"/v1/admin/llms/{r.json()['model']['id']}",
        json={
            "description": "changed",
            "api_key": NEW_KEY,
            "provider_config": {"azure_client_id": "AKIANEW", "azure_client_secret": NEW_SECRET},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    details = _update_record(h)
    changes = details["changes"]
    # That they changed is recorded; what they were and are is not.
    assert changes["provider_config"] == {"old": "[REDACTED]", "new": "[REDACTED]"}
    assert changes["api_key"] == {"old": "[REDACTED]", "new": "[REDACTED]"}
    assert changes["description"]["new"] == "changed"
    text = json.dumps(details)
    secrets = [OLD_SECRET, NEW_SECRET, OLD_KEY, NEW_KEY, "AKIAOLD", "AKIANEW"]
    if h.cipher is not None:
        secrets += [h.cipher.encrypt(s) for s in (OLD_KEY, NEW_KEY)]
        secrets += ["enc:", OLD_SECRET[::-1], NEW_SECRET[::-1]]
    for secret in secrets:
        assert secret not in text, secret
