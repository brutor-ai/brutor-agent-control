# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The secrets of a guardrail provider entry, and what a Bedrock entry must
have.

A `guardrail_configs.providers[]` entry holds two kinds of secret: its
`api_key`, and for Amazon Bedrock the credential the gateway signs
ApplyGuardrail with, in `config` (`aws_secret_access_key`,
`aws_session_token`). The gateway reads a Bedrock entry in
`brutor-gateway-core/src/services/guardrails/bedrock.rs` (`build`): the
guardrail id, the region and the credential are the entry's own, and an
entry without one of them builds no detector, so the policy's `fail_open`
decides every call.

* `bedrock_problems`: what an enabled Bedrock entry lacks to be built.
* `keep_omitted`: a secret the request did not send is the stored one (no
  response carries it, so an unchanged entry comes back without it). A key
  sent as `null` is removed.
* `public_entry`: an entry as a response gives it: no `api_key`, no secret
  of `config`, and for each secret of `config` whether one is stored.
* `encrypt_config_secrets`: the secrets of a Bedrock entry's `config`
  encrypted at rest, with the service, key and call that encrypt `api_key`
  The gateway decrypts them on load
  (`GuardrailsCache::decrypt_provider_secrets`: the entry whose `provider`
  is exactly `bedrock`; decrypt, or leave what does not decrypt).
* `decrypt_config_secrets`: the same values in clear, for the connection
  test: decrypt, or leave what does not decrypt.
* `has_secret`: whether an entry carries any secret, for the refusal of a
  save that could not encrypt one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# The keys of a provider entry's `config` whose values are secret.
CONFIG_SECRET_KEYS = ("aws_secret_access_key", "aws_session_token")
# `BedrockGuardrailDetector::build`: what it refuses to build without.
BEDROCK_REQUIRED = ("guardrail_id", "region", "aws_access_key_id", "aws_secret_access_key")


def _is_host_label(name: str) -> bool:
    from app.services.llm.provider_config import is_host_label

    return is_host_label(name)


def _provider_of(entry: Any) -> str:
    return (entry.get("provider") or "").lower() if isinstance(entry, dict) else ""


def bedrock_problems(providers: Optional[List[Any]]) -> List[str]:
    """What each enabled Bedrock entry lacks for the gateway to build its
    detector: one message per missing field, and one for a region that is
    not a region name. Nothing for a disabled entry."""
    issues: List[str] = []
    for entry in providers or []:
        if _provider_of(entry) != "bedrock" or not entry.get("enabled", True):
            continue
        config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        for field in BEDROCK_REQUIRED:
            value = config.get(field)
            if not (isinstance(value, str) and value):
                issues.append(_MISSING[field])
        region = config.get("region")
        if isinstance(region, str) and region and not _is_host_label(region):
            issues.append(
                "An enabled Bedrock provider's config.region is not an AWS region name (lower-case "
                "letters, digits and '-', such as eu-west-1)."
            )
    return issues


_MISSING = {
    "guardrail_id": (
        "An enabled Bedrock provider requires config.guardrail_id — "
        "the AWS Bedrock guardrail identifier."
    ),
    "region": "An enabled Bedrock provider requires config.region — the AWS region of the guardrail.",
    "aws_access_key_id": (
        "An enabled Bedrock provider requires config.aws_access_key_id — the guardrail's own AWS "
        "credential; the gateway signs with no other."
    ),
    "aws_secret_access_key": (
        "An enabled Bedrock provider requires config.aws_secret_access_key — the guardrail's own AWS "
        "credential; the gateway signs with no other."
    ),
}


def keep_omitted(new_providers: Optional[List[Any]], old_providers: Optional[List[Any]]) -> None:
    """Give each entry of `new_providers`, in place, the secrets of
    `config` it did not send: absent or empty is the stored value of the
    entry of the same provider, `null` removes."""
    stored: Dict[str, Dict[str, Any]] = {}
    for entry in old_providers or []:
        if _provider_of(entry) and isinstance(entry.get("config"), dict):
            stored[_provider_of(entry)] = entry["config"]
    for entry in new_providers or []:
        if not isinstance(entry, dict):
            continue
        config = entry.get("config")
        previous = stored.get(_provider_of(entry)) or {}
        if not isinstance(config, dict):
            kept = {key: previous[key] for key in CONFIG_SECRET_KEYS if previous.get(key)}
            if kept and config is None:
                entry["config"] = kept
            continue
        for key in CONFIG_SECRET_KEYS:
            if key in config and config[key] is None:
                del config[key]
            elif not config.get(key):
                if previous.get(key):
                    config[key] = previous[key]
                else:
                    config.pop(key, None)


def public_entry(entry: Any) -> Any:
    """`entry` as a response gives it: without `api_key` and without the
    secrets of `config`; `config_secrets_stored` says which of those are
    stored."""
    if not isinstance(entry, dict):
        return entry
    public = {key: value for key, value in entry.items() if key != "api_key"}
    config = entry.get("config")
    if isinstance(config, dict):
        public["config"] = {key: value for key, value in config.items() if key not in CONFIG_SECRET_KEYS}
        public["config_secrets_stored"] = {key: bool(config.get(key)) for key in CONFIG_SECRET_KEYS}
    else:
        public["config_secrets_stored"] = {key: False for key in CONFIG_SECRET_KEYS}
    return public


def with_stored_secrets(config: Optional[Dict[str, Any]], stored_entry: Any) -> Optional[Dict[str, Any]]:
    """`config` as a form sent it, with the secrets it left out taken from
    the stored entry: for the connection test of a saved provider."""
    stored = stored_entry.get("config") if isinstance(stored_entry, dict) else None
    if not isinstance(stored, dict):
        return config
    if config is None:
        return dict(stored)
    filled = dict(config)
    for key in CONFIG_SECRET_KEYS:
        if not filled.get(key) and stored.get(key):
            filled[key] = stored[key]
    return filled


def _bedrock_config(entry: Any) -> Optional[Dict[str, Any]]:
    # Exactly `bedrock`, as the gateway matches it.
    if not isinstance(entry, dict) or entry.get("provider") != "bedrock":
        return None
    return entry["config"] if isinstance(entry.get("config"), dict) else None


def has_secret(providers: Optional[List[Any]]) -> bool:
    """True when an entry carries an `api_key` or a secret of a Bedrock
    entry's `config`."""
    for entry in providers or []:
        if isinstance(entry, dict) and entry.get("api_key"):
            return True
        config = _bedrock_config(entry) or {}
        if any(config.get(key) for key in CONFIG_SECRET_KEYS):
            return True
    return False


def encrypt_config_secrets(
    providers: Optional[List[Any]],
    encrypt: Callable[[str, str], str],
    is_encrypted: Callable[[str], bool],
) -> None:
    """Encrypt, in place, each secret of each Bedrock entry's `config`
    (never stored in clear).

    `encrypt(value, column)` returns the stored form; `is_encrypted(value)`
    is true for a value that already is one, which is left alone (a
    re-save, a value `keep_omitted` carried over). The access key id, the
    region and the guardrail id and version stay in clear."""
    for entry in providers or []:
        config = _bedrock_config(entry)
        if config is None:
            continue
        for key in CONFIG_SECRET_KEYS:
            value = config.get(key)
            if isinstance(value, str) and value and not is_encrypted(value):
                config[key] = encrypt(value, f"guardrail_config.providers[].config.{key}")


def decrypt_config_secrets(config: Optional[Dict[str, Any]], decrypt: Callable[[str], str]) -> Optional[Dict[str, Any]]:
    """A copy of a Bedrock entry's `config` with its secrets in clear: a
    value that decrypts is replaced, one that does not is left (a value
    typed into the form, a row from before they were encrypted)."""
    if not isinstance(config, dict):
        return config
    clear = dict(config)
    for key in CONFIG_SECRET_KEYS:
        value = clear.get(key)
        if isinstance(value, str) and value:
            try:
                clear[key] = decrypt(value)
            except Exception:  # noqa: BLE001 - not a ciphertext of this key: left as it is
                pass
    return clear
