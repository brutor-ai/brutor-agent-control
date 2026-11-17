# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A model's `provider_config`.

**An update merges.** The value is one encrypted JSON object and an update replaced it
whole: changing the region of a Bedrock model sent `{"aws_region": ...}` and
the stored access key and secret were gone, so the gateway refused the model
(`provider_credential_missing`) until somebody typed the secret again. An
update merges now: a key present replaces, a key sent as `null` removes, a
key absent is kept. The result is validated for the two Bedrock providers
with the gateway's rules (the key names it reads, `ProviderCredentials`; the
region as `cloud_hosts::aws_region` checks it). The model response says
which keys are stored and gives the parts that are no secret; no secret is
ever returned.

**No encryption service, no save.** A `provider_config` on a control plane with no encryption service
went into a text column as a dict: 500. It is refused, 503, with the reason.
"""

from __future__ import annotations

import json
import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.llm_models import create_llm_models_router
from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.services.llm import provider_config as pc
from app.services.llm.model.llm_models_manager import LLModelsManager
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source

SECRET = "wJalr-secret-access-key-SENTINEL"
TOKEN = "session-token-SENTINEL"
KEY_ID = "AKIAEXAMPLEKEYID"


class Cipher:
    """Stands for the encryption service: what it returns is what is stored."""

    def encrypt(self, value: str) -> str:
        return "enc:" + value[::-1]

    def decrypt(self, value: str) -> str:
        assert value.startswith("enc:"), value
        return value[len("enc:"):][::-1]


def _harness(cipher):
    log = BrutorLogger("model-provider-config")
    manager = LLModelsManager(logger=log, encryption_service=cipher)
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=cipher)], logger_name="model-provider-config"
    )

    def fn(s):
        for key in ("bedrock", "bedrock_converse", "vertex_ai", "azure"):
            s.add(LlmProvider(id=f"prov-{key}", tenant_id="t1", key=key, display_name=key))

    harness.seed(fn)
    harness.cipher = cipher
    return harness


@pytest.fixture()
def h():
    harness = _harness(Cipher())
    yield harness
    harness.close()


FULL = {"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET, "aws_region": "eu-west-1"}


def _create(h, provider="bedrock", **over):
    body = {
        "name": "claude",
        "provider_id": f"prov-{provider}",
        "model_name": "anthropic.claude-sonnet-5-5-v1:0",
        "mode": "chat",
        "enabled": True,
        "auth_type": "aws_iam",
        "provider_config": dict(FULL),
    }
    body.update(over)
    return h.client.post("/v1/admin/llms", json=body, headers=h.headers())


def _stored(h, model_id):
    with h.Factory() as s:
        raw = s.get(LLModel, model_id).provider_config
    return None if raw is None else json.loads(h.cipher.decrypt(raw))


def _patch(h, model_id, **body):
    return h.client.patch(f"/v1/admin/llms/{model_id}", json=body, headers=h.headers())


# --- the update merges -------------------------------------------------------


def test_changing_the_region_keeps_the_credential(h):
    r = _create(h)
    assert r.status_code == 201, r.text
    mid = r.json()["model"]["id"]
    r = _patch(h, mid, provider_config={"aws_region": "us-east-1"})
    assert r.status_code == 200, r.text
    assert _stored(h, mid) == {**FULL, "aws_region": "us-east-1"}


def test_a_key_present_replaces_null_removes_absent_is_kept(h):
    mid = _create(h, provider_config={**FULL, "aws_session_token": TOKEN}).json()["model"]["id"]
    r = _patch(h, mid, provider_config={"aws_secret_access_key": "another-secret", "aws_session_token": None})
    assert r.status_code == 200, r.text
    assert _stored(h, mid) == {**FULL, "aws_secret_access_key": "another-secret"}
    # An empty object changes nothing.
    assert _patch(h, mid, provider_config={}).status_code == 200
    assert _stored(h, mid) == {**FULL, "aws_secret_access_key": "another-secret"}
    # An update that says nothing of it leaves it as it is, byte for byte.
    with h.Factory() as s:
        before = s.get(LLModel, mid).provider_config
    assert _patch(h, mid, description="d").status_code == 200
    with h.Factory() as s:
        assert s.get(LLModel, mid).provider_config == before


def test_removing_every_key_and_null_itself_clear_the_value(h):
    # (Of a row that is switched off: an enabled one keeps its credential,
    # `tests/test_cloud_model_save_rules.py`.)
    mid = _create(h, model_name="amazon.nova-pro-v1:0", enabled=False).json()["model"]["id"]
    r = _patch(h, mid, provider_config={key: None for key in FULL})
    assert r.status_code == 200, r.text
    assert _stored(h, mid) is None
    assert r.json()["model"]["has_provider_config"] is False
    mid = _create(h, name="two", enabled=False).json()["model"]["id"]
    r = _patch(h, mid, provider_config=None)
    assert r.status_code == 200, r.text
    assert _stored(h, mid) is None


def test_the_merge_is_recorded_without_any_value(h):
    from app.database.models.admin import TenantAdminAuditLog

    mid = _create(h).json()["model"]["id"]
    assert _patch(h, mid, provider_config={"aws_region": "us-east-1"}).status_code == 200
    with h.Factory() as s:
        records = [
            json.dumps(dict(row.details or {}), default=str)
            for row in s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == "llm_update").all()
        ]
    assert records and '"provider_config"' in records[-1]
    for text in records:
        assert SECRET not in text and SECRET[::-1] not in text and KEY_ID not in text


# --- the result is validated for Bedrock, with the gateway's rules ----------


@pytest.mark.parametrize("provider", ["bedrock", "bedrock_converse"])
@pytest.mark.parametrize(
    "config, why",
    [
        ({**FULL, "aws_region": "x.attacker.example/"}, "aws_region"),
        ({**FULL, "aws_region": "EU-WEST-1"}, "aws_region"),
        ({**FULL, "aws_region": "-eu-west-1"}, "aws_region"),
        ({**FULL, "aws_region": "a" * 33}, "aws_region"),
        ({**FULL, "aws_region": ""}, "aws_region"),
        ({**FULL, "aws_secret_key": "typo"}, "aws_secret_key"),
        ({**FULL, "region": "eu-west-1"}, "'region'"),
        ({"aws_access_key_id": KEY_ID, "aws_region": "eu-west-1"}, "aws_secret_access_key"),
        ({"aws_secret_access_key": SECRET, "aws_region": "eu-west-1"}, "aws_access_key_id"),
        ({**FULL, "aws_access_key_id": 12345}, "aws_access_key_id"),
        ({**FULL, "aws_session_token": "  "}, "aws_session_token"),
    ],
)
def test_a_bedrock_config_the_gateway_could_not_use_is_refused_on_create(h, provider, config, why):
    r = _create(h, provider=provider, provider_config=config)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and "provider_config" in detail and why in detail, detail
    assert SECRET not in r.text and KEY_ID not in r.text
    with h.Factory() as s:
        assert s.query(LLModel).count() == 0


def test_the_merged_result_is_what_is_validated(h):
    mid = _create(h).json()["model"]["id"]
    # Removing half of the credential leaves a row the gateway would refuse.
    r = _patch(h, mid, provider_config={"aws_secret_access_key": None})
    assert r.status_code == 422, r.text
    assert "aws_secret_access_key" in r.json()["detail"]
    r = _patch(h, mid, provider_config={"aws_region": "eu west"})
    assert r.status_code == 422, r.text
    assert _stored(h, mid) == FULL
    # A region alone is a config of a row that is switched off.
    mid2 = _create(
        h, name="region-only", provider_config={"aws_region": "eu-west-1"}, enabled=False
    ).json()["model"]["id"]
    assert _stored(h, mid2) == {"aws_region": "eu-west-1"}


def test_moving_a_row_to_bedrock_validates_what_it_already_holds(h):
    r = _create(h, provider="azure", api_url="https://x.openai.azure.com", auth_type="azure_ad",
                provider_config={"azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t"})
    assert r.status_code == 201, r.text
    mid = r.json()["model"]["id"]
    r = _patch(h, mid, provider_id="prov-bedrock")
    assert r.status_code == 422, r.text
    assert "azure_client_id" in r.json()["detail"]


def test_other_providers_configs_are_stored_as_given(h):
    # Vertex AI is no longer one of them
    # (`tests/test_cloud_model_save_rules.py`); Azure is.
    config = {"azure_client_id": "c", "azure_client_secret": "s", "azure_tenant_id": "t", "anything": "else"}
    r = _create(h, provider="azure", api_url="https://x.openai.azure.com", auth_type="azure_ad",
                provider_config=config)
    assert r.status_code == 201, r.text
    assert _stored(h, r.json()["model"]["id"]) == config


# --- what the response says --------------------------------------------------


def test_the_response_gives_the_region_and_which_keys_are_stored_and_no_secret(h):
    r = _create(h, provider_config={**FULL, "aws_session_token": TOKEN})
    assert r.status_code == 201, r.text
    mid = r.json()["model"]["id"]
    answers = [
        r,
        h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers()),
        _patch(h, mid, description="d"),
        h.client.get("/v1/admin/llms", headers=h.headers()),
    ]
    for answer in answers:
        assert answer.status_code in (200, 201), answer.text
        for secret in (SECRET, TOKEN, KEY_ID, SECRET[::-1]):
            assert secret not in answer.text
    for answer in answers[:3]:
        model = answer.json()["model"]
        assert model["has_provider_config"] is True
        assert model["aws_region"] == "eu-west-1"
        assert model["provider_config_keys"] == {
            "aws_access_key_id": True,
            "aws_secret_access_key": True,
            "aws_session_token": True,
            "gcp_credentials_json": False,
        }
    r = _patch(h, mid, provider_config={"aws_session_token": None, "aws_region": "us-east-1"})
    model = r.json()["model"]
    assert model["aws_region"] == "us-east-1"
    assert model["provider_config_keys"] == {
        "aws_access_key_id": True,
        "aws_secret_access_key": True,
        "aws_session_token": False,
        "gcp_credentials_json": False,
    }


def test_a_row_without_a_config_says_so(h):
    r = _create(h, provider_config=None, enabled=False)
    assert r.status_code == 201, r.text
    model = r.json()["model"]
    assert model["has_provider_config"] is False
    assert model["aws_region"] is None
    assert set(model["provider_config_keys"].values()) == {False}


def test_a_stored_value_that_cannot_be_read_is_reported_as_stored_and_nothing_else(h):
    mid = _create(h).json()["model"]["id"]

    def corrupt(s):
        s.get(LLModel, mid).provider_config = "not-what-the-cipher-wrote"

    h.seed(corrupt)
    r = h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers())
    assert r.status_code == 200, r.text
    model = r.json()["model"]
    assert model["has_provider_config"] is True and model["aws_region"] is None


# --- no encryption service ---------------------------------------------------


def test_without_an_encryption_service_a_provider_config_is_refused_not_a_500():
    harness = _harness(None)
    try:
        r = _create(harness)
        assert r.status_code == 503, r.text
        assert "ENCRYPTION_KEY" in r.json()["detail"] and "provider_config" in r.json()["detail"]
        assert SECRET not in r.text
        with harness.Factory() as s:
            assert s.query(LLModel).count() == 0
        # A row without one is saved, and giving it one is refused as well.
        r = _create(harness, provider_config=None, enabled=False)
        assert r.status_code == 201, r.text
        r = _patch(harness, r.json()["model"]["id"], provider_config={"aws_region": "eu-west-1"})
        assert r.status_code == 503, r.text
    finally:
        harness.close()


# --- the rules ----------------------------------------------------------------


def test_merge():
    assert pc.merge(None, {"a": "1"}) == {"a": "1"}
    assert pc.merge({"a": "1", "b": "2"}, {"b": None, "c": "3"}) == {"a": "1", "c": "3"}
    assert pc.merge({"a": "1"}, {}) == {"a": "1"}
    assert pc.merge({"a": "1"}, {"a": None}) == {}


@pytest.mark.parametrize("region", ["eu-west-1", "us-gov-west-1", "us", "a", "a" * 32, "ap-southeast-4"])
def test_a_region_name_is_one(region):
    assert pc.is_host_label(region)


@pytest.mark.parametrize("region", ["", "a" * 33, "-a", "a-", "Eu-west-1", "eu_west_1", "eu.west", "eu/west", "é"])
def test_a_value_that_is_not_a_region_name_is_none(region):
    assert not pc.is_host_label(region)


def test_the_rules_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    hosts = core_source("services", "cloud_hosts.rs").read_text(encoding="utf-8")
    rule = re.search(r"fn is_host_label\(name: &str\) -> bool \{(.*?)\n\}", hosts, re.S)
    assert rule, "cloud_hosts::is_host_label is gone" + read
    assert re.sub(r"\s+", " ", rule.group(1)).strip() == (
        "(1..=32).contains(&name.len()) && name .bytes() "
        ".all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-') "
        "&& !name.starts_with('-') && !name.ends_with('-')"
    ), "the gateway's region rule changed: bring app/services/llm/provider_config.py into line" + read
    assert "pub fn aws_region(region: &str) -> Result<&str, String> {\n    if is_host_label(region) {" in hosts, read
    auth = core_source("services", "cloud_auth.rs").read_text(encoding="utf-8")
    struct = auth[auth.index("pub struct ProviderCredentials {"):]
    struct = struct[: struct.index("\n}\n")]
    fields = re.findall(r"pub (\w+): Option<String>,", struct)
    assert [f for f in fields if f.startswith("aws_")] == list(pc.AWS_KEYS), "ProviderCredentials changed" + read
    assert set(fields) == set(pc.KNOWN_KEYS), read
    backend = core_source("services", "anthropic_adapter", "backend", "bedrock.rs").read_text(encoding="utf-8")
    # The credential is read from `provider_config` and nowhere else, and is
    # the key id and the secret together, neither of them blank.
    credential = backend[backend.index("pub(crate) fn from_model(model: &LlmModel) -> Option<Self> {"):]
    credential = re.sub(r"\s+", " ", credential[: credential.index("\n    }\n")])
    for piece in (
        "serde_json::from_str::<crate::services::cloud_auth::ProviderCredentials>( model.provider_config.as_deref()?, )",
        "let nonempty = |v: Option<String>| v.filter(|s| !s.trim().is_empty());",
        "access_key_id: nonempty(creds.aws_access_key_id)?,",
        "secret_access_key: nonempty(creds.aws_secret_access_key)?,",
        "session_token: nonempty(creds.aws_session_token),",
    ):
        assert piece in credential, f"AwsCredentials::from_model changed in the gateway ({piece!r})" + read
    assert "api_key" not in credential, read
