# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A model's `provider_config`: the cloud credential of the row.

One JSON object, stored encrypted. The gateway decrypts it into
`ProviderCredentials` (`brutor-gateway-core/src/services/cloud_auth.rs`) and
authenticates with it: SigV4 for Amazon Bedrock, the Google access token for
Vertex AI.

* `merge`: an update is a patch of the stored object. A key present
  replaces, a key sent as `null` removes, a key absent is kept. (An update
  used to replace the whole value: changing the region dropped the secret.)
* `problem`: why the gateway could not use a config, for Amazon Bedrock
  and Vertex AI: a key it does not read, a region, location or project that
  is not a name (`cloud_hosts`), half of an AWS credential, a service
  account `gcp_auth` could not parse.
* `enabled_row_problem`: what an enabled cloud row must have besides: its
  credential, its region (Bedrock), its project and location (Vertex AI).
* `api_key_problem`: a cloud row carries no `api_key`.
* `public_view`: what a response may say: the region, the project, the
  location, and for each secret key whether one is stored. Never a value
  of a secret key.

The names and the region rule are the gateway's;
`tests/test_model_provider_config.py` reads its source and fails when they
differ.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

# `ProviderCredentials`: every key the gateway reads from the object.
AWS_KEYS = ("aws_access_key_id", "aws_secret_access_key", "aws_region", "aws_session_token")
AZURE_KEYS = ("azure_client_id", "azure_client_secret", "azure_tenant_id")
GCP_KEYS = ("gcp_project_id", "gcp_location", "gcp_credentials_json")
KNOWN_KEYS = (*AWS_KEYS, *AZURE_KEYS, *GCP_KEYS)

# The AWS keys whose values are secret: reported as stored or not.
AWS_SECRET_KEYS = ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
AWS_REGION_KEY = "aws_region"

# The GCP keys whose values are secret: reported as stored or not.
GCP_SECRET_KEYS = ("gcp_credentials_json",)
GCP_PROJECT = "gcp_project_id"
GCP_LOCATION = "gcp_location"
GCP_CREDENTIALS = "gcp_credentials_json"
# `gcp_auth`'s `ServiceAccountKey` (the crate the gateway parses a service
# account with): the fields a service account must have, all strings.
SERVICE_ACCOUNT_FIELDS = ("private_key", "client_email", "token_uri")

# The providers whose config is validated: the gateway authenticates their
# requests with it, or refuses the row.
BEDROCK_PROVIDERS = frozenset({"bedrock", "bedrock_converse"})
VERTEX_PROVIDERS = frozenset({"vertex_ai", "vertex"})
CLOUD_PROVIDERS = BEDROCK_PROVIDERS | VERTEX_PROVIDERS


def is_host_label(name: str) -> bool:
    """`cloud_hosts::is_host_label`: 1 to 32 of `a-z 0-9 -`, not starting or
    ending with `-`. Every AWS region and Google Cloud location is one."""
    return (
        1 <= len(name) <= 32
        and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name)
        and not name.startswith("-")
        and not name.endswith("-")
    )


def is_gcp_project(project: str) -> bool:
    """`cloud_hosts::gcp_project`: 1 to 63 of `a-z 0-9 - . :`, starting with
    a letter or digit, without `..`."""
    return (
        1 <= len(project) <= 63
        and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-.:" for c in project)
        and project[0] in "abcdefghijklmnopqrstuvwxyz0123456789"
        and ".." not in project
    )


def service_account_problem(document: Any) -> Optional[str]:
    """Why `document` is not a service account the gateway can make a token
    provider of (`gcp_auth::CustomServiceAccount::from_json`): JSON, an
    object with `private_key`, `client_email` and `token_uri` as strings,
    the private key a PEM `PRIVATE KEY` (PKCS#8) holding an RSA key of 2048
    to 8192 bits. Nothing else is checked, as there. Never quotes a value."""
    what = "provider_config: gcp_credentials_json"
    if not isinstance(document, str) or not document.strip():
        return f"{what} must be a non-empty string: the service-account key file, as JSON."
    try:
        account = json.loads(document)
    except ValueError:
        return f"{what} is not JSON. Paste the service-account key file as it was downloaded."
    if not isinstance(account, dict):
        return f"{what} is not a JSON object. Paste the service-account key file as it was downloaded."
    for field in SERVICE_ACCOUNT_FIELDS:
        if not isinstance(account.get(field), str):
            return (
                f"{what} has no {field}. A service-account key file has "
                f"{', '.join(SERVICE_ACCOUNT_FIELDS)}; the gateway cannot authenticate without them "
                "(provider_credential_missing)."
            )
    if account.get("project_id") is not None and not isinstance(account["project_id"], str):
        return f"{what}: project_id must be a string."
    if not _is_rsa_pkcs8_pem(account["private_key"]):
        return (
            f"{what}: private_key is not a PEM-encoded PKCS#8 RSA private key "
            "(-----BEGIN PRIVATE KEY-----) of 2048 to 8192 bits, so the gateway cannot sign with it "
            "(provider_credential_missing)."
        )
    return None


def _is_rsa_pkcs8_pem(pem: str) -> bool:
    """True for a PEM `PRIVATE KEY` section that is a PKCS#8 RSA key of 2048
    to 8192 bits: what `gcp_auth`'s signer takes (`PrivatePkcs8KeyDer`, then
    `RsaKeyPair::from_pkcs8`). Read from the DER itself, with nothing but
    the standard library: the algorithm and the size of the modulus."""
    import base64
    import binascii

    begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    start = pem.find(begin)
    stop = pem.find(end, start)
    if start < 0 or stop < 0:
        return False
    try:
        der = base64.b64decode("".join(pem[start + len(begin): stop].split()), validate=True)
        # PrivateKeyInfo ::= SEQUENCE { version, algorithm, privateKey OCTET STRING }
        info = _der_children(_der_value(der, 0x30))
        algorithm = _der_children(info[1][1])
        if info[0] != (0x02, b"\x00") or algorithm[0] != (0x06, _RSA_ENCRYPTION_OID) or info[2][0] != 0x04:
            return False
        # RSAPrivateKey ::= SEQUENCE { version, modulus, publicExponent, ... }
        key = _der_children(_der_value(info[2][1], 0x30))
        if len(key) < 9 or any(tag != 0x02 for tag, _ in key[:9]):
            return False
        bits = int.from_bytes(key[1][1], "big").bit_length()
    except (ValueError, IndexError, binascii.Error):
        return False
    return 2048 <= bits <= 8192


# 1.2.840.113549.1.1.1 (rsaEncryption), DER-encoded.
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")


def _der_element(data: bytes, offset: int) -> tuple:
    """(tag, value, next offset) of the DER element at `offset`."""
    tag, first = data[offset], data[offset + 1]
    offset += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if not 1 <= count <= 4:
            raise ValueError("DER length")
        length = int.from_bytes(data[offset: offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise ValueError("DER element runs past the end")
    return tag, data[offset: offset + length], offset + length


def _der_value(data: bytes, tag: int) -> bytes:
    """The value of `data`, which is one DER element of `tag` and nothing
    after it."""
    found, value, end = _der_element(data, 0)
    if found != tag or end != len(data):
        raise ValueError("DER tag")
    return value


def _der_children(data: bytes) -> list:
    """The (tag, value) elements `data` is made of."""
    children, offset = [], 0
    while offset < len(data):
        tag, value, offset = _der_element(data, offset)
        children.append((tag, value))
    return children


def merge(stored: Optional[Mapping[str, Any]], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """`stored` with `patch` applied: present replaces, `None` removes,
    absent is kept."""
    merged = dict(stored or {})
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _key_problems(config: Mapping[str, Any], keys: tuple, cloud: str) -> Optional[str]:
    unknown = sorted(key for key in config if key not in keys)
    if unknown:
        return (
            f"provider_config: {', '.join(repr(k) for k in unknown)} "
            f"{'is' if len(unknown) == 1 else 'are'} not read by the gateway for {cloud} model. "
            f"The keys are {', '.join(keys)}."
        )
    for key in keys:
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            return f"provider_config: {key} must be a non-empty string."
    return None


def problem(provider: Optional[str], config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Why `config` cannot be the credential of a row of `provider`; `None`
    for one the gateway can use, for no config and for a provider whose
    config is not validated here. What a row that is enabled must have
    besides is `enabled_row_problem`. Never quotes a value."""
    if not config:
        return None
    if provider in VERTEX_PROVIDERS:
        return _vertex_problem(config)
    if provider not in BEDROCK_PROVIDERS:
        return None
    wrong = _key_problems(config, AWS_KEYS, "an Amazon Bedrock")
    if wrong:
        return wrong
    if AWS_REGION_KEY in config and not is_host_label(config[AWS_REGION_KEY]):
        return (
            "provider_config: aws_region is not an AWS region name (lower-case letters, digits and '-', "
            "such as eu-west-1). The gateway would refuse every request for the model "
            "(provider_endpoint_invalid)."
        )
    has_id, has_secret = "aws_access_key_id" in config, "aws_secret_access_key" in config
    if has_id != has_secret:
        missing = "aws_secret_access_key" if has_id else "aws_access_key_id"
        return (
            f"provider_config: {missing} is missing. The gateway signs with the access key id and the "
            "secret access key together, and refuses a model that has one without the other "
            "(provider_credential_missing)."
        )
    return None


def _vertex_problem(config: Mapping[str, Any]) -> Optional[str]:
    wrong = _key_problems(config, GCP_KEYS, "a Vertex AI")
    if wrong:
        return wrong
    if GCP_LOCATION in config and not is_host_label(config[GCP_LOCATION]):
        return (
            "provider_config: gcp_location is not a Google Cloud location name (lower-case letters, digits "
            "and '-', such as europe-west4). The gateway would refuse every request for the model "
            "(provider_endpoint_invalid)."
        )
    if GCP_PROJECT in config and not is_gcp_project(config[GCP_PROJECT].strip()):
        return (
            "provider_config: gcp_project_id is not a Google Cloud project id (lower-case letters, digits, "
            "'-', '.' and ':', starting with a letter or digit). The gateway would refuse every request "
            "for the model (provider_endpoint_invalid)."
        )
    if GCP_CREDENTIALS in config:
        return service_account_problem(config[GCP_CREDENTIALS])
    return None


def api_key_problem(provider: Optional[str], api_key: Any) -> Optional[str]:
    """Why a row of `provider` cannot carry `api_key`: the gateway reads no
    `api_key` of an Amazon Bedrock or Vertex AI row (
    `ACCESS_KEY_ID:SECRET_ACCESS_KEY` and the base64 service account in it
    went out in an API-key header), so one stored there is a credential
    kept for nothing. `None` for every other row and for no key."""
    if provider not in CLOUD_PROVIDERS or not (isinstance(api_key, str) and api_key.strip()):
        return None
    if provider in VERTEX_PROVIDERS:
        return (
            "api_key: a Vertex AI model has no API key. The gateway authenticates it with the model's own "
            "service account and reads its api_key nowhere: put the service-account key file in "
            "provider_config.gcp_credentials_json and send no api_key (null clears a stored one)."
        )
    return (
        "api_key: an Amazon Bedrock model has no API key. The gateway signs its requests with the model's "
        "own AWS credential and reads its api_key nowhere: put the credential in provider_config "
        "(aws_access_key_id, aws_secret_access_key) and send no api_key (null clears a stored one)."
    )


def enabled_row_problem(
    provider: Optional[str],
    model_name: Optional[str],
    api_url: Optional[str],
    config: Optional[Mapping[str, Any]],
    model_metadata: Any = None,
) -> Optional[str]:
    """Why the gateway would refuse every request for an ENABLED cloud row
    with this config (`BedrockBackend::row_problem`,
    `bedrock_runtime::row_problem`, `VertexBackend::row_problem`): no
    credential of its own (501 `provider_credential_missing`), or, on Vertex
    AI, no project or a location that is not one (501
    `provider_endpoint_invalid`). `None` for a row it can authenticate and
    for a row of another provider. A row that is switched off is never
    dispatched and may be saved without (the catalog and the seed hold such
    rows until somebody gives them a credential)."""
    config = config or {}
    metadata = model_metadata if isinstance(model_metadata, dict) else {}
    if provider in BEDROCK_PROVIDERS:
        if not (config.get("aws_access_key_id") and config.get("aws_secret_access_key")):
            return (
                "provider_config: an enabled Amazon Bedrock model needs its own AWS credential "
                "(aws_access_key_id and aws_secret_access_key). The gateway signs with the model's "
                "credential and no other, and would refuse every request for it "
                "(provider_credential_missing). Give the credential, or save the model disabled."
            )
        # `BedrockBackend::region`: the config, then the metadata, then the
        # host of a Bedrock Runtime `api_url`. There is no default,
        # and the region is asked with an `api_url` too: it is part of the
        # signature.
        region = config.get(AWS_REGION_KEY)
        if region is None and isinstance(metadata.get("aws_region"), str):
            region = metadata["aws_region"]
        if region is None and "bedrock-runtime." in (api_url or ""):
            after = api_url.split("bedrock-runtime.", 1)[1]
            if "." in after:
                region = after.split(".", 1)[0]
        if region is None:
            return (
                "provider_config: an enabled Amazon Bedrock model needs its AWS region (aws_region). "
                "The gateway has no default region and would refuse every request for the model "
                "(provider_endpoint_invalid)."
            )
        if not is_host_label(region):
            return (
                "The model's AWS region (model_metadata.aws_region, or the host of api_url) is not a region "
                "name. The gateway would refuse every request for the model (provider_endpoint_invalid). "
                "Set provider_config.aws_region."
            )
        return None
    if provider not in VERTEX_PROVIDERS:
        return None
    if not config.get(GCP_CREDENTIALS):
        return (
            "provider_config: an enabled Vertex AI model needs its own service account "
            "(gcp_credentials_json). The gateway authenticates with the model's service account and no "
            "other, and would refuse every request for it (provider_credential_missing). Give the "
            "service-account key file, or save the model disabled."
        )
    # `VertexBackend::row_problem`: a Claude row with an `api_url` is sent
    # there and builds no host.
    if "claude" in (model_name or "").lower() and (api_url or ""):
        return None
    project = config.get(GCP_PROJECT)
    if project is None and isinstance(metadata.get("project"), str):
        project = metadata["project"]
    project = (project or "").strip()
    if not project:
        return (
            "provider_config: an enabled Vertex AI model needs a Google Cloud project (gcp_project_id). "
            "The gateway has no default project and would refuse every request for the model "
            "(provider_endpoint_invalid)."
        )
    if not is_gcp_project(project):
        return (
            "The model's Google Cloud project (model_metadata.project) is not a project id. The gateway "
            "would refuse every request for the model (provider_endpoint_invalid). Set "
            "provider_config.gcp_project_id."
        )
    location = config.get(GCP_LOCATION)
    if location is None and isinstance(metadata.get("region"), str):
        location = metadata["region"]
    location = (location or "").strip()
    if not location:
        return (
            "provider_config: an enabled Vertex AI model needs a location (gcp_location: a region such as "
            "europe-west4, or global, us or eu). The gateway has no default location and would refuse "
            "every request for the model (provider_endpoint_invalid)."
        )
    if not is_host_label(location):
        return (
            "The model's Google Cloud location (model_metadata.region) is not a location name. The gateway "
            "would refuse every request for the model (provider_endpoint_invalid). Set "
            "provider_config.gcp_location."
        )
    return None


def public_view(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """What a response says of a config: the region, the project and the
    location, and which secret keys are stored."""
    config = config or {}

    def text(key: str) -> Optional[str]:
        return config[key] if isinstance(config.get(key), str) else None

    return {
        "aws_region": text(AWS_REGION_KEY),
        "gcp_project_id": text(GCP_PROJECT),
        "gcp_location": text(GCP_LOCATION),
        "provider_config_keys": {key: bool(config.get(key)) for key in (*AWS_SECRET_KEYS, *GCP_SECRET_KEYS)},
    }


def read(stored: Optional[str], encryption_service: Any) -> Optional[Dict[str, Any]]:
    """The object in a stored value; `None` for no value and for one that
    cannot be read (no encryption service, another key, not an object)."""
    if not stored or encryption_service is None:
        return None
    try:
        value = json.loads(encryption_service.decrypt(stored))
    except Exception:  # noqa: BLE001 - a value this process cannot read
        return None
    return value if isinstance(value, dict) else None
