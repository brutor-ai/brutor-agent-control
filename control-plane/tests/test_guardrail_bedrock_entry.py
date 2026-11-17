# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A Bedrock guardrail provider entry.

**Complete when enabled.** The gateway builds the Bedrock detector from the
entry's own guardrail id, region and AWS credential
(`src/services/guardrails/bedrock.rs`: `build`) and from nothing else; an
entry without one of them is a check that cannot be built, and the policy's
`fail_open` then decides every call. An enabled entry is refused at save
without them.

**Its secrets are not returned.** `config.aws_secret_access_key` and
`config.aws_session_token` came back in every response that carries the
config. They are stripped as `api_key` is, with a mark that says whether
each is stored, and a secret the request leaves out is the stored one.

**Encrypted at rest.** The two values are encrypted as `api_key` is; the gateway decrypts them on load
(`GuardrailsCache::decrypt_provider_secrets`), and a ciphertext made by the
gateway's own code is read here with the control plane's decrypt.
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.core.logging_system import BrutorLogger
from app.database.models.guardrail_config import GuardrailConfig
from app.services.guardrails import provider_probe
from app.services.guardrails import provider_secrets as ps
from app.services.guardrails.guardrails_service import GuardrailsService
from tests.admin_endpoint_harness import build_admin_harness
from tests.core_repo import core_revision, core_source

SECRET = "wJalr-guardrail-secret-SENTINEL"
TOKEN = "guardrail-session-token-SENTINEL"
CONFIG = {
    "guardrail_id": "gr-abc123",
    "guardrail_version": "1",
    "region": "eu-west-1",
    "aws_access_key_id": "AKIAGUARDRAIL",
    "aws_secret_access_key": SECRET,
}
BASE = "/v1/admin/guardrails/configs"


class Cipher:
    """A stand-in for the encryption service: the same for the same input,
    and with the shape of the service's ciphertext (a stored value is told
    from clear text by that shape, never by trying to decrypt it)."""

    def encrypt(self, value):
        import base64

        body = value.encode("utf-8")
        pad = 16 - len(body) % 16
        raw = b"\x80" + b"\x00" * 24 + body + bytes([pad]) * pad + b"\x00" * 32
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def decrypt(self, value):
        import base64

        from app.services.oauth.encryption_service import has_ciphertext_shape

        if not has_ciphertext_shape(value):
            raise ValueError("not encrypted")
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))[25:-32]
        return raw[: -raw[-1]].decode("utf-8")


@pytest.fixture()
def h():
    log = BrutorLogger("guardrail-bedrock-entry")
    harness = build_admin_harness(
        [create_guardrails_router(GuardrailsService(logger=log), log, encryption_service=Cipher())],
        logger_name="guardrail-bedrock-entry",
    )
    yield harness
    harness.close()


def _entry(**config):
    merged = {**CONFIG, **config}
    return {"provider": "bedrock", "enabled": True, "config": {k: v for k, v in merged.items() if v is not ...}}


def _create(h, providers, name="g"):
    return h.client.post(BASE, json={"name": name, "providers": providers}, headers=h.headers())


def _put(h, cid, providers):
    return h.client.patch(f"{BASE}/{cid}", json={"providers": providers}, headers=h.headers())


def _stored(h, cid, clear=True):
    """The stored providers; with `clear`, the secrets of `config` as the
    gateway reads them after it has decrypted them."""
    with h.Factory() as s:
        providers = s.get(GuardrailConfig, cid).providers
    if clear:
        providers = [{**p, "config": ps.decrypt_config_secrets(p.get("config"), Cipher().decrypt)} for p in providers]
    return providers


def _issues(r):
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "guardrail_provider_incomplete"
    assert SECRET not in r.text and TOKEN not in r.text
    return " ".join(detail["issues"])


# --- complete when enabled -----------------------------------------------------


@pytest.mark.parametrize("field", ["guardrail_id", "region", "aws_access_key_id", "aws_secret_access_key"])
@pytest.mark.parametrize("value", [..., "", None, 7])
def test_an_enabled_entry_without_a_required_field_is_refused(h, field, value):
    issues = _issues(_create(h, [_entry(**{field: value})]))
    assert f"config.{field}" in issues
    with h.Factory() as s:
        assert s.query(GuardrailConfig).filter(GuardrailConfig.name == "g").count() == 0


@pytest.mark.parametrize("region", ["EU-WEST-1", "x.attacker.example/", "-eu", "a" * 33])
def test_a_region_that_is_not_a_region_name_is_refused(h, region):
    assert "config.region is not an AWS region name" in _issues(_create(h, [_entry(region=region)]))


def test_a_complete_entry_is_saved_and_a_disabled_one_needs_nothing(h):
    r = _create(h, [_entry()])
    assert r.status_code in (200, 201), r.text
    r = _create(h, [{"provider": "bedrock", "enabled": False, "config": {}}], name="off")
    assert r.status_code in (200, 201), r.text
    # Enabling it as it is: refused, on update as on create.
    cid = r.json()["config"]["id"]
    issues = _issues(_put(h, cid, [{"provider": "bedrock", "enabled": True, "config": {}}]))
    for field in ps.BEDROCK_REQUIRED:
        assert f"config.{field}" in issues


def test_the_required_fields_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    source = core_source("services", "guardrails", "bedrock.rs").read_text(encoding="utf-8")
    build = source[source.index("pub fn build(entry: &Value, http: HttpClient)"):]
    build = build[: build.index("\n    }\n")]
    required = re.findall(r'cfg_str\("(\w+)"\)\s*\.ok_or\(DetectorError::MissingProviderConfig', build)
    required += re.findall(r'cfg_str\("(\w+)"\)\.ok_or\(DetectorError::MissingProviderConfig', build)
    assert sorted(set(required)) == sorted(ps.BEDROCK_REQUIRED), (
        "the gateway's Bedrock guardrail entry changed: bring "
        "app/services/guardrails/provider_secrets.py into line" + read
    )
    # An empty string is no value, and the region is a region name.
    assert ".filter(|s| !s.is_empty())" in build, read
    assert "crate::services::cloud_hosts::aws_region(&region).map_err(invalid_region)?;" in build, read
    assert 'let session_token = cfg_str("aws_session_token");' in build, read


# --- the secrets are not returned ----------------------------------------------


def test_no_response_carries_the_secret_or_the_session_token(h):
    r = _create(h, [_entry(aws_session_token=TOKEN)])
    assert r.status_code in (200, 201), r.text
    cid = r.json()["config"]["id"]
    answers = [
        r,
        h.client.get(f"{BASE}/{cid}", headers=h.headers()),
        h.client.get(BASE, headers=h.headers()),
        _put(h, cid, [_entry(guardrail_version="2")]),
    ]
    for answer in answers:
        assert answer.status_code in (200, 201), answer.text
        assert SECRET not in answer.text and TOKEN not in answer.text
        assert "aws_secret_access_key\":\"" not in answer.text.replace(" ", "")
    for answer in (answers[0], answers[1], answers[3]):
        (provider,) = answer.json()["config"]["providers"]
        assert provider["config"]["aws_access_key_id"] == "AKIAGUARDRAIL"
        assert provider["config"]["region"] == "eu-west-1"
        assert "aws_secret_access_key" not in provider["config"]
        assert "aws_session_token" not in provider["config"]
    assert answers[1].json()["config"]["providers"][0]["config_secrets_stored"] == {
        "aws_secret_access_key": True,
        "aws_session_token": True,
    }


def test_the_model_s_own_dictionary_carries_none_either(h):
    cid = _create(h, [_entry(aws_session_token=TOKEN)]).json()["config"]["id"]
    with h.Factory() as s:
        text = str(s.get(GuardrailConfig, cid).to_dict())
    assert SECRET not in text and TOKEN not in text


def test_a_secret_left_out_is_kept_and_null_removes(h):
    cid = _create(h, [_entry(aws_session_token=TOKEN)]).json()["config"]["id"]
    # The form sends back what it was given: no secret, no token.
    r = _put(h, cid, [_entry(aws_secret_access_key=..., region="us-east-1")])
    assert r.status_code == 200, r.text
    (stored,) = _stored(h, cid)
    assert stored["config"]["aws_secret_access_key"] == SECRET
    assert stored["config"]["aws_session_token"] == TOKEN
    assert stored["config"]["region"] == "us-east-1"
    # Empty is left out too; a new value replaces; null removes the token.
    r = _put(h, cid, [_entry(aws_secret_access_key="", aws_session_token=None)])
    assert r.status_code == 200, r.text
    (stored,) = _stored(h, cid)
    assert stored["config"]["aws_secret_access_key"] == SECRET
    assert "aws_session_token" not in stored["config"]
    r = _put(h, cid, [_entry(aws_secret_access_key="another-secret")])
    assert _stored(h, cid)[0]["config"]["aws_secret_access_key"] == "another-secret"
    assert r.json()["config"]["providers"][0]["config_secrets_stored"] == {
        "aws_secret_access_key": True,
        "aws_session_token": False,
    }
    # Removing the secret of an enabled entry leaves one the gateway cannot build.
    assert "config.aws_secret_access_key" in _issues(_put(h, cid, [_entry(aws_secret_access_key=None)]))


def test_the_connection_test_of_a_saved_entry_uses_the_stored_secret(h, monkeypatch):
    cid = _create(h, [_entry(aws_session_token=TOKEN)]).json()["config"]["id"]
    seen = {}

    async def probe(provider, endpoint, api_key, config):
        seen.update(config or {})
        from app.services.guardrails.provider_probe import ProbeResult

        return ProbeResult(status="healthy", detail="ok")

    monkeypatch.setattr(provider_probe, "probe_provider", probe)
    form = {k: v for k, v in CONFIG.items() if k != "aws_secret_access_key"}
    r = h.client.post(
        "/v1/admin/guardrails/providers/test",
        json={"provider": "bedrock", "config": form, "config_id": cid},
        headers=h.headers(),
    )
    assert r.status_code == 200, r.text
    # In clear, as AWS needs them: decrypted from what is stored.
    assert seen["aws_secret_access_key"] == SECRET and seen["aws_session_token"] == TOKEN
    assert SECRET not in r.text


# --- the rules -------------------------------------------------------------------


def test_keep_omitted():
    old = [{"provider": "bedrock", "config": {**CONFIG, "aws_session_token": TOKEN}},
           {"provider": "lakera", "api_key": "k"}]
    new = [{"provider": "Bedrock", "config": {"guardrail_id": "g2"}}, {"provider": "lakera"}]
    ps.keep_omitted(new, old)
    assert new[0]["config"] == {"guardrail_id": "g2", "aws_secret_access_key": SECRET, "aws_session_token": TOKEN}
    assert new[1] == {"provider": "lakera"}
    # Nothing stored: nothing appears.
    fresh = [{"provider": "bedrock", "config": {"aws_secret_access_key": ""}}]
    ps.keep_omitted(fresh, None)
    assert fresh == [{"provider": "bedrock", "config": {}}]


def test_public_entry():
    entry = {"provider": "bedrock", "api_key": "k", "config": {**CONFIG, "aws_session_token": ""}}
    public = ps.public_entry(entry)
    assert "api_key" not in public and SECRET not in str(public)
    assert public["config_secrets_stored"] == {"aws_secret_access_key": True, "aws_session_token": False}
    assert entry["config"]["aws_secret_access_key"] == SECRET  # the stored entry is not touched
    assert ps.public_entry({"provider": "lakera"})["config_secrets_stored"] == {
        "aws_secret_access_key": False, "aws_session_token": False,
    }


# --- encryption at rest ------------------------------------------------------------


def _encrypt_with(cipher, providers):
    def is_encrypted(value):
        try:
            cipher.decrypt(value)
            return True
        except Exception:  # noqa: BLE001
            return False

    columns = []

    def encrypt(value, column):
        columns.append(column)
        return cipher.encrypt(value)

    ps.encrypt_config_secrets(providers, encrypt, is_encrypted)
    return columns


def test_encrypt_config_secrets_encrypts_the_two_secrets_and_nothing_else():
    cipher = Cipher()
    providers = [
        {"provider": "bedrock", "config": {**CONFIG, "aws_session_token": TOKEN}},
        {"provider": "lakera", "api_key": "k", "config": None},
        # Only a Bedrock entry's: the gateway decrypts no other entry's config.
        {"provider": "custom", "config": {"header": "x", "aws_secret_access_key": "not-bedrock"}},
        "not an entry",
    ]
    columns = _encrypt_with(cipher, providers)
    config = providers[0]["config"]
    assert cipher.decrypt(config["aws_secret_access_key"]) == SECRET
    assert cipher.decrypt(config["aws_session_token"]) == TOKEN
    assert {k: v for k, v in config.items() if k not in ps.CONFIG_SECRET_KEYS} == {
        k: v for k, v in CONFIG.items() if k not in ps.CONFIG_SECRET_KEYS
    }
    assert providers[1:] == [
        {"provider": "lakera", "api_key": "k", "config": None},
        {"provider": "custom", "config": {"header": "x", "aws_secret_access_key": "not-bedrock"}},
        "not an entry",
    ]
    assert columns == [
        "guardrail_config.providers[].config.aws_secret_access_key",
        "guardrail_config.providers[].config.aws_session_token",
    ]
    # A second pass (a re-save, a value carried over) changes nothing.
    before = dict(config)
    assert _encrypt_with(cipher, providers) == []
    assert config == before


def test_the_two_secrets_are_stored_encrypted_and_nothing_else_is(h):
    cid = _create(h, [_entry(aws_session_token=TOKEN)]).json()["config"]["id"]
    (stored,) = _stored(h, cid, clear=False)
    config = stored["config"]
    assert config["aws_secret_access_key"] == Cipher().encrypt(SECRET)
    assert config["aws_session_token"] == Cipher().encrypt(TOKEN)
    assert {k: config[k] for k in ("guardrail_id", "guardrail_version", "region", "aws_access_key_id")} == {
        k: CONFIG[k] for k in ("guardrail_id", "guardrail_version", "region", "aws_access_key_id")
    }
    with h.Factory() as s:
        assert SECRET not in str(s.get(GuardrailConfig, cid).providers)
    # A save that leaves them out keeps the stored bytes: not encrypted twice.
    r = _put(h, cid, [_entry(aws_secret_access_key=..., region="us-east-1")])
    assert r.status_code == 200, r.text
    assert _stored(h, cid, clear=False)[0]["config"]["aws_secret_access_key"] == config["aws_secret_access_key"]
    assert _stored(h, cid, clear=False)[0]["config"]["aws_session_token"] == config["aws_session_token"]


def test_without_an_encryption_service_a_provider_with_a_secret_is_refused():
    # Nothing is stored in clear, on any path.
    log = BrutorLogger("guardrail-bedrock-entry")
    harness = build_admin_harness(
        [create_guardrails_router(GuardrailsService(logger=log), log, encryption_service=None)],
        logger_name="guardrail-bedrock-entry",
    )
    try:
        for providers in ([_entry()], [{"provider": "lakera", "api_key": "lk-SENTINEL"}]):
            r = _create(harness, providers)
            assert r.status_code == 503, r.text
            assert "ENCRYPTION_KEY" in r.json()["detail"]
            assert SECRET not in r.text and "lk-SENTINEL" not in r.text
        with harness.Factory() as s:
            assert s.query(GuardrailConfig).filter(GuardrailConfig.name == "g").count() == 0
        # A provider without a secret is saved.
        r = _create(harness, [{"provider": "presidio", "endpoint": "http://presidio:3000"}])
        assert r.status_code in (200, 201), r.text
    finally:
        harness.close()


def test_an_api_key_is_encrypted_on_create_too(h):
    r = _create(h, [{"provider": "lakera", "api_key": "lk-SENTINEL"}])
    assert r.status_code in (200, 201), r.text
    (stored,) = _stored(h, r.json()["config"]["id"], clear=False)
    assert stored["api_key"] == Cipher().encrypt("lk-SENTINEL")


def test_the_control_plane_reads_what_the_gateway_encrypts(monkeypatch):
    # A ciphertext made by the gateway's own `EncryptionService` (every
    # value made up), read with the service that encrypts at save.
    from app.services.config_manager import ConfigManager
    from app.services.oauth.encryption_service import EncryptionService

    monkeypatch.setenv("ENCRYPTION_KEY", "made-up-master-key-for-the-f1027-vector")
    service = EncryptionService(config_manager=ConfigManager())
    vector = (
        "gAAAAABq9mnXjZTZMpv3ftzNUergmKFi_cVaabbwXpQ7XnD-8MFRVMY5XrFhM-WeWVqD0tasmqOEHQ1hosBB6D2HOCuS-JbtDN9Wjf3qLZQ4dLsZ"
        "xmfH4siyC-WQXeQU20UlZOYbM38_"
    )
    assert service.decrypt(vector) == "made-up/AWS+secret-access-key-F1027"
    # And what it writes for the column has the same form and reads back.
    stored = service.encrypt("made-up/AWS+secret-access-key-F1027")
    assert stored.startswith("gAAAAA") and service.decrypt(stored) == "made-up/AWS+secret-access-key-F1027"
    clear = ps.decrypt_config_secrets({"aws_secret_access_key": vector, "aws_session_token": "typed"}, service.decrypt)
    assert clear == {"aws_secret_access_key": "made-up/AWS+secret-access-key-F1027", "aws_session_token": "typed"}


def test_the_gateway_decrypts_these_fields_of_this_entry():
    read = f" [the gateway's source read: {core_revision()}]"
    source = core_source("services", "guardrails", "mod.rs").read_text(encoding="utf-8")
    fields = re.search(r"const BEDROCK_ENCRYPTED_CONFIG_FIELDS: [^=]*=\s*&?\[(.*?)\];", source, re.S)
    assert fields, "the gateway does not decrypt a Bedrock entry's config" + read
    assert re.findall(r'"(\w+)"', fields.group(1)) == list(ps.CONFIG_SECRET_KEYS), read
    squeezed = re.sub(r"\s+", " ", source)
    # `api_key` of every entry; the two config fields of the entry whose
    # provider is exactly `bedrock`. (A ciphertext that does not decrypt is
    # removed and the entry's checks are not built: what the control plane
    # writes is always a ciphertext of its own key.)
    assert (
        'let mut unreadable = self.open_field(obj, "api_key").err(); '
        'if obj.get("provider").and_then(|v| v.as_str()) == Some("bedrock") {'
    ) in squeezed, read
    assert (
        "for field in BEDROCK_ENCRYPTED_CONFIG_FIELDS { if let Err(field) = self.open_field(config, field) {"
    ) in squeezed, read
