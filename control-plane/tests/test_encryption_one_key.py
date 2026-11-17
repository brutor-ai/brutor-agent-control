# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Stored secrets are encrypted under the installation's encryption key, and
nothing else.

The product offers no key provider and no tenant key: every secret at rest
is a Fernet token under the key derived from `ENCRYPTION_KEY`, which the
gateway derives the same way. There is no second wire format, no setting
that selects a provider, and no key identifier on a tenant.

Whether a stored value is ciphertext is decided by its shape, as the
gateway decides it (`has_ciphertext_shape`), never by trying to decrypt
it: a value encrypted under another key does not decrypt, and must not be
taken for clear text and encrypted a second time.
"""

from __future__ import annotations

import base64
import importlib
import re
from pathlib import Path

import pytest

from app.core.logging_system import BrutorLogger
from app.services.config_manager import ConfigManager
from app.services.oauth import encryption_service as module
from app.services.oauth.encryption_service import EncryptionService, has_ciphertext_shape
from tests.core_repo import core_revision, core_source

REPO = Path(__file__).resolve().parents[1]


def _service(key: str | None = None, monkeypatch=None) -> EncryptionService:
    if key is not None:
        monkeypatch.setenv("ENCRYPTION_KEY", key)
    return EncryptionService(config_manager=ConfigManager(logger=BrutorLogger("t"), validate=False))


# --- one key, one format ------------------------------------------------------


def test_a_secret_is_a_fernet_token_under_the_encryption_key(monkeypatch):
    service = _service("k" * 44, monkeypatch)
    token = service.encrypt("s3cret")
    assert token != "s3cret" and service.decrypt(token) == "s3cret"
    assert token.startswith("gAAAA") and ":" not in token
    assert has_ciphertext_shape(token)
    # The same key in another process reads it; another key does not.
    assert _service("k" * 44, monkeypatch).decrypt(token) == "s3cret"
    with pytest.raises(ValueError):
        _service("another-key-" * 4, monkeypatch).decrypt(token)


def test_encrypt_takes_the_secret_and_nothing_else():
    import inspect

    assert list(inspect.signature(EncryptionService.encrypt).parameters) == ["self", "plaintext"]
    assert list(inspect.signature(EncryptionService.__init__).parameters) == ["self", "config_manager", "logger"]


@pytest.mark.parametrize("name", ["aws_kms_provider", "vault_provider", "envelope", "factory", "provider"])
def test_there_is_no_key_provider_but_the_local_one(name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"app.services.key_management.{name}")
    from app.services import key_management

    assert key_management.__all__ == ["LocalKeyProvider"]


def test_no_source_names_a_provider_or_the_envelope_format():
    offenders = []
    for path in [*REPO.glob("app/**/*.py"), *REPO.glob("enterprise/brutor_enterprise/**/*.py")]:
        text = path.read_text(encoding="utf-8")
        for word in ("BAEv1", "aws_kms", "hvac", "VAULT_", "BRUTOR_KEY_PROVIDER", "BRUTOR_KMS_", "KeyProviderFactory"):
            if word in text and not (word == "BAEv1" and path.name == "encryption_service.py"):
                offenders.append(f"{path.relative_to(REPO)}: {word}")
    assert offenders == []


def test_a_value_in_the_old_envelope_format_is_not_decrypted(monkeypatch):
    service = _service("k" * 44, monkeypatch)
    with pytest.raises(ValueError):
        service.decrypt("BAEv1:local:AAAA:" + service.encrypt("x"))


REMOVED_SETTINGS = [
    "BRUTOR_KEY_PROVIDER", "BRUTOR_KMS_KEY_ID", "BRUTOR_KMS_ENDPOINT_URL",
    "VAULT_ADDR", "VAULT_TOKEN", "VAULT_TRANSIT_KEY", "VAULT_TRANSIT_MOUNT",
    "KEY_PROVIDER", "KMS_KEY_ID", "AWS_KMS_ENDPOINT_URL",
]


@pytest.mark.parametrize("name", REMOVED_SETTINGS)
def test_a_key_provider_setting_is_not_read(monkeypatch, name):
    """No retired name is kept: nothing reads a key-provider setting, and
    it does not stop the start."""
    from app.env_registry import REGISTRY

    monkeypatch.setenv(name, "value-that-must-not-leak")
    view = REGISTRY.view(enterprise=REGISTRY.holds_enterprise_variables())
    assert view.find(name) is None and view.problems(missing=False) == []


# The Enterprise package's manifest and compose file:
# `enterprise/tests/test_encryption_one_key.py`, with these two checks.


def no_vault_client(name: str) -> None:
    assert "hvac" not in (REPO / name).read_text(encoding="utf-8"), name


def no_key_service(name: str) -> None:
    import yaml

    text = (REPO / name).read_text(encoding="utf-8")
    services = set(yaml.safe_load(text).get("services", {}))
    assert not services & {"localstack", "vault"}, name
    assert not re.search(r"localstack|vault|\bkms\b", text, re.I), name


def test_the_package_declares_no_vault_client():
    no_vault_client("pyproject.toml")


def test_the_development_compose_file_starts_no_key_service():
    no_key_service("docker-compose.yml")


# --- a tenant has no key ------------------------------------------------------


def test_a_tenant_has_no_key_identifier():
    from app.api.v1.models.admin.tenant_settings import TenantResponse, TenantSettingsUpdate
    from app.database.models.tenant import Tenant

    assert "kms_key_id" not in Tenant.__table__.c
    assert "kms_key_id" not in TenantSettingsUpdate.model_fields
    assert "kms_key_id" not in TenantResponse.model_fields
    tenant = Tenant(id="tenant-a", name="tenant-a", display_name="A", primary_contact_email="a@a.example")
    assert "kms_key_id" not in tenant.to_dict()
    with pytest.raises(Exception, match="kms_key_id"):
        TenantSettingsUpdate(kms_key_id="alias/mine")


# --- ciphertext is told by its shape -------------------------------------------


def test_the_shape_rule_is_the_gateways():
    rust = core_source("services", "encryption.rs").read_text(encoding="utf-8")
    body = rust[rust.index("pub fn has_ciphertext_shape"):]
    body = body[: body.index("\n}\n")]
    for part in (
        "value.starts_with(ENVELOPE_PREFIX)", "raw.len() >= FERNET_MIN_LEN",
        "(raw.len() - FERNET_OVERHEAD).is_multiple_of(16)", "raw[0] == FERNET_VERSION", "raw[1..5] == [0, 0, 0, 0]",
    ):
        assert part in body, (part, core_revision())
    assert f'const ENVELOPE_PREFIX: &str = "{module.RETIRED_ENVELOPE_PREFIX}";' in rust
    assert f"const FERNET_OVERHEAD: usize = {module.FERNET_OVERHEAD};" in rust
    assert "const FERNET_MIN_LEN: usize = FERNET_OVERHEAD + 16;" in rust
    assert module.FERNET_MIN_LEN == module.FERNET_OVERHEAD + 16
    assert "const FERNET_VERSION: u8 = 0x80;" in rust and module.FERNET_VERSION == 0x80


def test_what_has_the_shape(monkeypatch):
    service = _service("k" * 44, monkeypatch)
    for plaintext in ("x", "a longer secret " * 9, "sk-" + "A" * 48):
        token = service.encrypt(plaintext)
        assert has_ciphertext_shape(token)
        assert has_ciphertext_shape(token + "=" * (-len(token) % 4))
    assert has_ciphertext_shape("BAEv1:aws_kms:AAAA:BBBB")


@pytest.mark.parametrize(
    "clear",
    [
        "", "sk-proj-abcdefghijklmnop", "gAAAAAnotatoken", "AKIAIOSFODNN7EXAMPLE", "s3cret with spaces",
        "gAAAAA" + "A" * 91,  # base64 of the right start, of a length that is no token's
        base64.urlsafe_b64encode(b"\x81" + b"\x00" * 72).decode(),  # another version byte
        base64.urlsafe_b64encode(b"\x80\x01" + b"\x00" * 71).decode(),  # a timestamp past 2106
        "gAAAA" + "!" * 95,
    ],
)
def test_what_does_not(clear):
    assert has_ciphertext_shape(clear) is False


def test_a_secret_encrypted_under_another_key_is_not_encrypted_again(monkeypatch):
    """What trying to decrypt got wrong: the value does not decrypt, so it
    was taken for clear text and encrypted a second time."""
    from app.api.v1.endpoints.admin.guardrails import create_guardrails_router  # noqa: F401
    from app.services.guardrails import provider_secrets

    foreign = _service("another-key-" * 4, monkeypatch).encrypt("AKIA-secret")
    service = _service("k" * 44, monkeypatch)
    providers = [{"provider": "bedrock", "config": {"aws_secret_access_key": foreign, "aws_session_token": "clear-token"}}]

    provider_secrets.encrypt_config_secrets(providers, lambda value, column: service.encrypt(value), has_ciphertext_shape)

    config = providers[0]["config"]
    assert config["aws_secret_access_key"] == foreign
    assert config["aws_session_token"] != "clear-token" and service.decrypt(config["aws_session_token"]) == "clear-token"


def test_nothing_decides_by_trying_to_decrypt():
    offenders = []
    for path in [*REPO.glob("app/**/*.py"), *REPO.glob("enterprise/brutor_enterprise/**/*.py")]:
        text = path.read_text(encoding="utf-8")
        if re.search(r"def (is_)?_?already_encrypted", text):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
