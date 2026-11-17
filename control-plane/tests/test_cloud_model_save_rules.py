# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a cloud model row must be to be saved.

The gateway serves an Amazon Bedrock or a Vertex AI row with the row's own
cloud credential, read from `provider_config` and nowhere else, or refuses
it at dispatch (`providers::dispatch_refusal`): 501
`provider_credential_missing`, 501 `provider_endpoint_invalid`. The control
plane refuses at save what the gateway refuses at dispatch:

* a credential in `api_key` of a cloud row (either legacy shape): the
  gateway reads no `api_key` of such a row;
* a Vertex AI `provider_config` the gateway could not use: a key it does not
  read, a location or project that is not a name, a service account
  `gcp_auth` could not parse;
* an ENABLED cloud row without its credential (and, on Vertex AI, without a
  project). A row that is switched off is never dispatched: it may be saved
  without, which is how the catalog and the seed hold their cloud rows.
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
KEY_ID = "AKIAEXAMPLEKEYID"
AWS = {"aws_access_key_id": KEY_ID, "aws_secret_access_key": SECRET, "aws_region": "eu-west-1"}


def _pem(bits: int = 2048, kind: str = "pkcs8") -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    key = ec.generate_private_key(ec.SECP256R1()) if kind == "ec" else rsa.generate_private_key(65537, bits)
    fmt = (
        serialization.PrivateFormat.TraditionalOpenSSL if kind == "pkcs1" else serialization.PrivateFormat.PKCS8
    )
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode()


PEM = _pem()


def _account(**over) -> str:
    account = {
        "type": "service_account",
        "project_id": "acme-prod",
        "private_key_id": "0123",
        "private_key": PEM,
        "client_email": "gateway@acme-prod.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    account.update(over)
    return json.dumps({k: v for k, v in account.items() if v is not ...})


GCP = {"gcp_project_id": "acme-prod", "gcp_location": "europe-west4", "gcp_credentials_json": _account()}


class Cipher:
    def encrypt(self, value: str) -> str:
        return "enc:" + value[::-1]

    def decrypt(self, value: str) -> str:
        assert value.startswith("enc:"), value
        return value[len("enc:"):][::-1]


@pytest.fixture()
def h():
    cipher = Cipher()
    log = BrutorLogger("cloud-model-save")
    manager = LLModelsManager(logger=log, encryption_service=cipher)
    harness = build_admin_harness(
        [create_llm_models_router(manager, log, encryption_service=cipher)], logger_name="cloud-model-save"
    )

    def fn(s):
        for key in ("bedrock", "bedrock_converse", "vertex_ai", "vertex", "openai"):
            s.add(LlmProvider(id=f"prov-{key}", tenant_id="t1", key=key, display_name=key))

    harness.seed(fn)
    harness.cipher = cipher
    yield harness
    harness.close()


NAMES = {
    "bedrock": "anthropic.claude-sonnet-5-5-v1:0",
    "bedrock_converse": "amazon.nova-pro-v1:0",
    "vertex_ai": "gemini-2.5-pro",
    "vertex": "claude-sonnet-5-5",
    "openai": "gpt-5",
}


def _create(h, provider, **over):
    vertex = provider in ("vertex_ai", "vertex")
    body = {
        "name": f"m-{provider}",
        "provider_id": f"prov-{provider}",
        "model_name": NAMES[provider],
        "mode": "chat",
        "enabled": True,
        "provider_config": dict(GCP if vertex else AWS),
    }
    if provider == "openai":
        body["provider_config"] = None
    body.update(over)
    return h.client.post("/v1/admin/llms", json=body, headers=h.headers())


def _patch(h, model_id, **body):
    return h.client.patch(f"/v1/admin/llms/{model_id}", json=body, headers=h.headers())


def _row(h, model_id):
    with h.Factory() as s:
        row = s.get(LLModel, model_id)
        config = None if row.provider_config is None else json.loads(h.cipher.decrypt(row.provider_config))
        return row.api_key, config, row.enabled


def _count(h):
    with h.Factory() as s:
        return s.query(LLModel).count()


def _refused(r, *words):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str), detail
    for word in words:
        assert word in detail, detail
    for secret in (SECRET, KEY_ID, PEM[40:80]):
        assert secret not in r.text
    return detail


# --- 1a: no credential in `api_key` of a cloud row --------------------------


@pytest.mark.parametrize(
    "provider, key",
    [
        ("bedrock", f"{KEY_ID}:{SECRET}"),
        ("bedrock_converse", f"{KEY_ID}:{SECRET}"),
        ("vertex_ai", "eyJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCJ9"),
        ("vertex", "eyJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCJ9"),
        ("bedrock", "anything"),
    ],
)
def test_a_cloud_row_with_an_api_key_is_refused_on_create(h, provider, key):
    _refused(_create(h, provider, api_key=key), "api_key", "provider_config")
    assert _count(h) == 0


def test_a_cloud_row_is_not_given_an_api_key_on_update(h):
    mid = _create(h, "bedrock").json()["model"]["id"]
    _refused(_patch(h, mid, api_key=f"{KEY_ID}:{SECRET}"), "api_key")
    assert _row(h, mid)[0] is None
    # Blank and null are no key.
    assert _patch(h, mid, api_key="").status_code == 200
    assert _patch(h, mid, api_key=None).status_code == 200


def test_a_row_with_an_api_key_does_not_become_a_cloud_row_with_it(h):
    r = _create(h, "openai", api_key="sk-openai")
    assert r.status_code == 201, r.text
    mid = r.json()["model"]["id"]
    move = {"provider_id": "prov-bedrock", "model_name": NAMES["bedrock"], "provider_config": dict(AWS)}
    _refused(_patch(h, mid, **move), "api_key")
    # Cleared in the same update, the row moves.
    r = _patch(h, mid, api_key=None, **move)
    assert r.status_code == 200, r.text
    assert _row(h, mid)[0] is None


def test_another_providers_row_keeps_its_api_key(h):
    r = _create(h, "openai", api_key="sk-openai")
    assert r.status_code == 201, r.text
    assert _row(h, r.json()["model"]["id"])[0] == "enc:" + "sk-openai"[::-1]


# --- 1b: the Vertex AI config -----------------------------------------------


@pytest.mark.parametrize("provider", ["vertex_ai", "vertex"])
def test_a_vertex_row_with_its_service_account_and_project_is_saved(h, provider):
    r = _create(h, provider)
    assert r.status_code == 201, r.text
    assert _row(h, r.json()["model"]["id"])[1] == GCP


@pytest.mark.parametrize(
    "config, why",
    [
        ({**GCP, "aws_region": "eu-west-1"}, "'aws_region'"),
        ({**GCP, "project": "acme"}, "'project'"),
        ({**GCP, "gcp_location": "x.attacker.example/"}, "gcp_location"),
        ({**GCP, "gcp_location": "Europe-West4"}, "gcp_location"),
        ({**GCP, "gcp_location": ""}, "gcp_location"),
        ({**GCP, "gcp_project_id": "acme/../other"}, "gcp_project_id"),
        ({**GCP, "gcp_project_id": "Acme"}, "gcp_project_id"),
        ({**GCP, "gcp_project_id": "-acme"}, "gcp_project_id"),
        ({**GCP, "gcp_project_id": "a" * 64}, "gcp_project_id"),
        ({**GCP, "gcp_project_id": 7}, "gcp_project_id"),
        ({**GCP, "gcp_credentials_json": "{}"}, "private_key"),
        ({**GCP, "gcp_credentials_json": "not json"}, "not JSON"),
        ({**GCP, "gcp_credentials_json": "[1]"}, "not a JSON object"),
        ({**GCP, "gcp_credentials_json": {"private_key": PEM}}, "gcp_credentials_json"),
        ({**GCP, "gcp_credentials_json": _account(private_key=...)}, "private_key"),
        ({**GCP, "gcp_credentials_json": _account(client_email=...)}, "client_email"),
        ({**GCP, "gcp_credentials_json": _account(token_uri=...)}, "token_uri"),
        ({**GCP, "gcp_credentials_json": _account(client_email=5)}, "client_email"),
        ({**GCP, "gcp_credentials_json": _account(project_id=5)}, "project_id"),
        ({**GCP, "gcp_credentials_json": _account(private_key="not a key")}, "private_key"),
        ({**GCP, "gcp_credentials_json": _account(private_key=_pem(kind="pkcs1"))}, "private_key"),
        ({**GCP, "gcp_credentials_json": _account(private_key=_pem(kind="ec"))}, "private_key"),
        ({**GCP, "gcp_credentials_json": _account(private_key=_pem(bits=1024))}, "private_key"),
    ],
)
def test_a_vertex_config_the_gateway_could_not_use_is_refused(h, config, why):
    _refused(_create(h, "vertex_ai", provider_config=config), "provider_config", why)
    assert _count(h) == 0


def test_what_gcp_auth_does_not_check_is_not_checked(h):
    # No `type`, no `project_id`, unknown fields: `ServiceAccountKey` has
    # `private_key`, `client_email`, `token_uri` and an optional project.
    account = json.dumps({"private_key": PEM, "client_email": "x", "token_uri": "y", "extra": [1]})
    r = _create(h, "vertex_ai", provider_config={**GCP, "gcp_credentials_json": account})
    assert r.status_code == 201, r.text


def test_the_merged_vertex_config_is_what_is_validated(h):
    mid = _create(h, "vertex_ai").json()["model"]["id"]
    _refused(_patch(h, mid, provider_config={"gcp_location": "EU"}), "gcp_location")
    r = _patch(h, mid, provider_config={"gcp_location": "us-east5"})
    assert r.status_code == 200, r.text
    assert _row(h, mid)[1] == {**GCP, "gcp_location": "us-east5"}


def test_the_response_gives_project_and_location_and_never_the_key(h):
    r = _create(h, "vertex_ai")
    mid = r.json()["model"]["id"]
    answers = [
        r,
        h.client.get(f"/v1/admin/llms/{mid}", headers=h.headers()),
        _patch(h, mid, description="d"),
        h.client.get("/v1/admin/llms", headers=h.headers()),
    ]
    for answer in answers:
        assert answer.status_code in (200, 201), answer.text
        assert PEM[40:80] not in answer.text and "private_key" not in answer.text
        assert "gateway@acme-prod" not in answer.text
    for answer in answers[:3]:
        model = answer.json()["model"]
        assert model["gcp_project_id"] == "acme-prod"
        assert model["gcp_location"] == "europe-west4"
        assert model["aws_region"] is None
        assert model["provider_config_keys"] == {
            "aws_access_key_id": False,
            "aws_secret_access_key": False,
            "aws_session_token": False,
            "gcp_credentials_json": True,
        }
    r = _create(h, "bedrock")
    model = r.json()["model"]
    assert model["gcp_project_id"] is None and model["gcp_location"] is None
    assert model["provider_config_keys"]["gcp_credentials_json"] is False


# --- 1c: an enabled cloud row has its credential -----------------------------


@pytest.mark.parametrize(
    "provider, config, why",
    [
        ("bedrock", None, "aws_access_key_id"),
        ("bedrock_converse", None, "aws_access_key_id"),
        ("bedrock", {"aws_region": "eu-west-1"}, "aws_access_key_id"),
        ("vertex_ai", None, "gcp_credentials_json"),
        ("vertex", None, "gcp_credentials_json"),
        ("vertex_ai", {"gcp_project_id": "acme-prod"}, "gcp_credentials_json"),
        ("vertex_ai", {k: v for k, v in GCP.items() if k != "gcp_project_id"}, "gcp_project_id"),
    ],
)
def test_an_enabled_cloud_row_without_its_credential_is_refused(h, provider, config, why):
    detail = _refused(_create(h, provider, provider_config=config), why)
    assert "provider_credential_missing" in detail or "provider_endpoint_invalid" in detail
    assert _count(h) == 0


@pytest.mark.parametrize("provider", ["bedrock", "bedrock_converse", "vertex_ai", "vertex"])
def test_a_disabled_cloud_row_is_saved_without_one_and_cannot_be_enabled_so(h, provider):
    r = _create(h, provider, provider_config=None, enabled=False)
    assert r.status_code == 201, r.text
    mid = r.json()["model"]["id"]
    _refused(_patch(h, mid, enabled=True), "provider_config")
    assert _row(h, mid)[2] is False
    # Given its credential in the same update, it is enabled.
    config = GCP if provider.startswith("vertex") else AWS
    r = _patch(h, mid, enabled=True, provider_config=config)
    assert r.status_code == 200, r.text
    assert _row(h, mid)[2] is True


def test_an_enabled_cloud_row_does_not_lose_its_credential(h):
    mid = _create(h, "bedrock").json()["model"]["id"]
    _refused(_patch(h, mid, provider_config=None), "aws_access_key_id")
    assert _row(h, mid)[1] == AWS
    # Switched off in the same update, it may.
    r = _patch(h, mid, provider_config=None, enabled=False)
    assert r.status_code == 200, r.text
    assert _row(h, mid)[1] is None
    mid = _create(h, "vertex_ai").json()["model"]["id"]
    _refused(_patch(h, mid, provider_config={"gcp_project_id": None}), "gcp_project_id")
    _refused(_patch(h, mid, provider_config={"gcp_credentials_json": None}), "gcp_credentials_json")


def test_a_project_in_the_metadata_is_one_as_in_the_gateway(h):
    config = {k: v for k, v in GCP.items() if k != "gcp_project_id"}
    r = _create(h, "vertex_ai", provider_config=config, model_metadata={"project": "acme-prod"})
    assert r.status_code == 201, r.text
    _refused(
        _create(h, "vertex_ai", name="two", provider_config=config, model_metadata={"project": "Not A Project"}),
        "project",
    )
    # A Claude row with its own `api_url` is sent there: no host is built.
    r = _create(h, "vertex", name="three", provider_config=config,
                api_url="https://europe-west4-aiplatform.googleapis.com/v1/projects/p/locations/l/publishers/anthropic/models/m")
    assert r.status_code == 201, r.text


def test_an_enabled_bedrock_row_names_its_region(h):
    # No default region: `provider_config`, then the metadata, then
    # the host of a Bedrock Runtime `api_url`, as the gateway reads it.
    keys = {k: v for k, v in AWS.items() if k != "aws_region"}
    detail = _refused(_create(h, "bedrock", provider_config=keys), "aws_region")
    assert "provider_endpoint_invalid" in detail
    _refused(_create(h, "bedrock", provider_config=keys, api_url="https://bedrock.internal.example"), "aws_region")
    _refused(_create(h, "bedrock", provider_config=keys, model_metadata={"aws_region": "EU WEST"}), "region")
    for n, extra in enumerate((
        {"model_metadata": {"aws_region": "eu-west-1"}},
        {"api_url": "https://bedrock-runtime.eu-west-1.amazonaws.com"},
    )):
        r = _create(h, "bedrock", name=f"r{n}", provider_config=keys, **extra)
        assert r.status_code == 201, r.text
    # Switched off, it is saved without.
    assert _create(h, "bedrock", name="off", provider_config=keys, enabled=False).status_code == 201
    mid = _create(h, "bedrock_converse").json()["model"]["id"]
    _refused(_patch(h, mid, provider_config={"aws_region": None}), "aws_region")


def test_an_enabled_vertex_row_names_its_location(h):
    # No default location.
    config = {k: v for k, v in GCP.items() if k != "gcp_location"}
    detail = _refused(_create(h, "vertex_ai", provider_config=config), "gcp_location")
    assert "provider_endpoint_invalid" in detail
    r = _create(h, "vertex_ai", provider_config=config, model_metadata={"region": "europe-west4"})
    assert r.status_code == 201, r.text
    for n, location in enumerate(("global", "us", "eu", "us-east5")):
        r = _create(h, "vertex_ai", name=f"l{n}", provider_config={**GCP, "gcp_location": location})
        assert r.status_code == 201, r.text
    # A Claude row with its own `api_url` builds no host.
    r = _create(h, "vertex", name="claude-own", provider_config={"gcp_credentials_json": GCP["gcp_credentials_json"]},
                api_url="https://proxy.example.com/v1/claude")
    assert r.status_code == 201, r.text


def test_an_enabled_row_does_not_become_a_cloud_row_without_a_credential(h):
    mid = _create(h, "openai").json()["model"]["id"]
    _refused(_patch(h, mid, provider_id="prov-bedrock", model_name=NAMES["bedrock"]), "aws_access_key_id")


def test_an_update_that_touches_none_of_it_is_not_asked(h):
    # A row from before the rule (the seed's, an import's): what it lacks is
    # asked when it is enabled or its credential is changed, not when its
    # description is.
    def fn(s):
        s.add(LLModel(id="llm-old", tenant_id="t1", name="old", provider_id="prov-bedrock",
                      model_name=NAMES["bedrock"], mode="chat", enabled=True, api_key="enc:legacy"))

    h.seed(fn)
    r = _patch(h, "llm-old", description="d")
    assert r.status_code == 200, r.text


def test_with_no_save_check_registered_nothing_more_is_asked(h, monkeypatch):
    # `LLM_MODEL_SAVE_CHECKS`: the Community Edition registers none (the
    # Enterprise Edition's is in `enterprise/tests/test_batch_providers.py`).
    from app.composition import hooks

    monkeypatch.setattr(hooks.HOOKS, "_specs", hooks.HookRegistry()._specs)
    r = _create(h, "bedrock", supports_batch=True)
    assert r.status_code == 201, r.text
    asked = []
    registry = hooks.HookRegistry(
        [hooks.HookSpec("a_check", hooks.LLM_MODEL_SAVE_CHECKS, lambda **kw: asked.append(kw))]
    )
    monkeypatch.setattr(hooks.HOOKS, "_specs", registry._specs)
    assert _create(h, "openai", name="two", supports_batch=True).status_code == 201
    assert asked == [{"provider": "openai", "row": {"supports_batch": True}}]


# --- the rules are the gateway's ---------------------------------------------


def test_the_rules():
    assert pc.problem("vertex_ai", GCP) is None and pc.problem("vertex", GCP) is None
    assert pc.problem("openai", {"anything": 1}) is None
    assert pc.enabled_row_problem("openai", "gpt-5", None, None) is None
    assert pc.enabled_row_problem("bedrock", "m", None, AWS) is None
    assert pc.enabled_row_problem("vertex_ai", "gemini-2.5-pro", None, GCP) is None
    assert pc.api_key_problem("openai", "sk") is None
    assert pc.api_key_problem("bedrock", "  ") is None and pc.api_key_problem("vertex", None) is None
    for project in ("acme-prod", "example.com:acme", "a", "1a", "a" * 63):
        assert pc.is_gcp_project(project), project
    for project in ("", "a" * 64, "Acme", "-a", ".a", "a..b", "a/b", "a b", "a%2f"):
        assert not pc.is_gcp_project(project), project


def test_the_vertex_rules_are_the_gateways():
    read = f" [the gateway's source read: {core_revision()}]"
    squeeze = lambda text: re.sub(r"\s+", " ", text)  # noqa: E731
    hosts = core_source("services", "cloud_hosts.rs").read_text(encoding="utf-8")
    assert "pub fn gcp_location(location: &str) -> Result<&str, String> {\n    if is_host_label(location) {" in hosts, read
    rule = re.search(r"pub fn gcp_project\(project: &str\) -> Result<&str, String> \{\s*let ok = (.*?);\n", hosts, re.S)
    assert rule, "cloud_hosts::gcp_project is gone" + read
    assert squeeze(rule.group(1)) == (
        "(1..=63).contains(&project.len()) && project.bytes().all(|b| { b.is_ascii_lowercase() || "
        "b.is_ascii_digit() || matches!(b, b'-' | b'.' | b':') }) && project .bytes() .next() "
        ".is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit()) && !project.contains(\"..\")"
    ), "the gateway's project rule changed: bring app/services/llm/provider_config.py into line" + read
    auth = core_source("services", "cloud_auth.rs").read_text(encoding="utf-8")
    struct = auth[auth.index("pub struct ProviderCredentials {"):]
    fields = re.findall(r"pub (\w+): Option<String>,", struct[: struct.index("\n}\n")])
    assert [f for f in fields if f.startswith("gcp_")] == list(pc.GCP_KEYS), "ProviderCredentials changed" + read
    vertex = squeeze(core_source("services", "anthropic_adapter", "backend", "vertex.rs").read_text(encoding="utf-8"))
    for piece in (
        # The credential: `provider_config`'s `gcp_credentials_json`, parsed
        # by `gcp_auth`, and nothing else.
        ".and_then(|creds| creds.gcp_credentials_json)?;",
        "let provider = gcp_auth::CustomServiceAccount::from_json(account).ok()?;",
        'if !row_has_a_service_account(model) { return Some(("provider_credential_missing", missing_credential(model))); }',
        # Location and project: the config first, then the metadata; no
        # default of either.
        'let region = pc_location .or_else(|| { metadata .and_then(|m| m.get("region")) .and_then(|v| v.as_str()) '
        ".map(String::from) }) .map(|l| l.trim().to_string()) .filter(|l| !l.is_empty());",
        'let location = location.ok_or_else(|| { "the model names no Vertex AI location',
        'let project = pc_project .or_else(|| { metadata .and_then(|m| m.get("project")) .and_then(|v| v.as_str()) '
        ".map(String::from) }) .map(|p| p.trim().to_string()) .filter(|p| !p.is_empty());",
        # A Claude row with an `api_url` builds no host.
        'let claude = model.model_name.to_ascii_lowercase().contains("claude"); let built = if claude { '
        "if model.api_url.as_deref().is_some_and(|u| !u.is_empty()) { return None; }",
    ):
        assert piece in vertex, f"the gateway's Vertex AI row check changed ({piece!r})" + read
    assert "api_key" not in vertex.split("fn row_account")[1].split("fn parse_account")[0].replace(
        "A row's `api_key` is not read", ""
    ), read
    # The locations that are not a region have their own host, and pass the
    # same name rule.
    assert '"global" => "aiplatform.googleapis.com".to_string(),' in hosts, read
    assert 'multi_region @ ("us" | "eu") => format!("aiplatform.{multi_region}.rep.googleapis.com"),' in hosts, read
    assert "Ok(match gcp_location(location)? {" in hosts, read
    # A Bedrock row: its credential, then its endpoint.
    for path, call in (
        (("services", "anthropic_adapter", "backend", "bedrock.rs"), "pub(crate) fn row_problem"),
        (("services", "bedrock_runtime", "mod.rs"), "pub fn row_problem"),
    ):
        source = core_source(*path).read_text(encoding="utf-8")
        body = squeeze(source[source.index(call):][:600])
        assert (
            'if AwsCredentials::from_model(model).is_none() { return Some(("provider_credential_missing", '
            "missing_credential(model))); }"
        ) in body, f"{path[-1]}: row_problem changed" + read
    # A Bedrock row's region: the config, the metadata, the host of a
    # Bedrock Runtime `api_url`, and no default.
    bedrock = squeeze(core_source("services", "anthropic_adapter", "backend", "bedrock.rs").read_text(encoding="utf-8"))
    region = bedrock[bedrock.index("pub(crate) fn region(model: &LlmModel) -> Option<String> {"):][:1300]
    for piece in (
        "if let Some(region) = creds.aws_region { return Some(region); }",
        '.and_then(|m| m.get("aws_region")) .and_then(|v| v.as_str()) { return Some(region.to_string()); }',
        'if let Some(start) = url.find("bedrock-runtime.") { let after = &url[start + "bedrock-runtime.".len()..]; '
        "if let Some(end) = after.find('.') { return Some(after[..end].to_string()); } }",
    ):
        assert piece in region, f"BedrockBackend::region changed in the gateway ({piece!r})" + read
    assert re.search(r"return Some\(after\[\.\.end\]\.to_string\(\)\); \} \} \} None \}", region), (
        "a Bedrock row has a default region again" + read
    )
    assert "let Some(region) = Self::region(model) else { return Err(format!( \"provider_endpoint_invalid: " in bedrock, read
