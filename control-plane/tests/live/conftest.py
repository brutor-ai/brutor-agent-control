# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Shared fixtures for the live end-to-end suite.

Design notes
============
- Every test in this directory carries the ``live_e2e`` marker (applied
  in ``pytest_collection_modifyitems`` below) so the default
  ``pytest`` invocation excludes it.
- Module-level skip if the stack isn't reachable or no provider keys
  are present — the suite is a smoke check, not an integration harness
  that brings up its own services.
- Fixtures are session-scoped where it's safe (login tokens, default
  group lookup) so the suite can run in seconds, not minutes.
- Configuration is env-var driven so the same suite can target a
  staging deployment without code changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

CONTROL_URL = os.environ.get("BRUTOR_CONTROL_URL", "http://localhost:5050").rstrip("/")
PROXY_URL = os.environ.get("BRUTOR_PROXY_URL", "http://localhost:8100").rstrip("/")
ADMIN_USERNAME = os.environ.get("BRUTOR_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("BRUTOR_ADMIN_PASSWORD")  # no default: the installation's own
TRIAL_USERNAME = os.environ.get("BRUTOR_TRIAL_USERNAME", "trial")
TRIAL_PASSWORD = os.environ.get("BRUTOR_TRIAL_PASSWORD", "Trial123!")
# Self-hosted seeds the default tenant with id="default". For customised
# installs, point the suite at a different tenant via env.
DEFAULT_TENANT_ID = os.environ.get("BRUTOR_TENANT_ID", "default")

DEFAULT_GROUP_NAME = "Default"
HELLO_WORLD_SKILL_NAME = "hello-world"

# Everything the suite provisions (models, MCP servers / server configs,
# VMCPs, skill bindings, API keys, the trial user's membership) is bound
# to this ONE resource group instead of Default, so a run never widens
# what Default's members can reach and the suite's footprint is visible
# in one place in the Admin UI. Created idempotently by `e2e_group_id`.
E2E_GROUP_NAME = os.environ.get("BRUTOR_E2E_GROUP", "E2ETests")


# ─────────────────────────────────────────────────────────────────────
# E2E_MODELS — the ONE list of models the live suite exercises
# ─────────────────────────────────────────────────────────────────────
#
# Keyed by control-plane provider slug (``LLModel.provider`` /
# catalog ``provider_key``). Every name MUST exist verbatim as a
# ``model_name`` in that provider's seed catalog file under
# ``app/database/provider_data/`` (the source of truth that the
# ``/v1/admin/llm-catalog`` browse API serves):
#
#   openai    → openai_models_init.py, openai_multimodal_models_init.py,
#               openai_embedding_models_init.py
#   anthropic → anthropic_models_init.py      gemini  → google_models_init.py
#   xai       → xai_models_init.py            deepseek → deepseek_models_init.py
#   moonshot  → moonshotai_models_init.py     groq    → groq_models_init.py
#   minimax   → minimax_models_init.py        fal_ai  → fal_ai_models_init.py
#
# The control plane seeds only catalog entries marked ``enabled``, so the
# suite imports anything missing from the catalog (``POST
# /v1/admin/llm-catalog/{id}/import`` — the Admin UI's "Browse → Add"
# flow), keys it, enables it and binds it to E2ETests. See
# ``e2e_models`` below.
#
# To update: when a provider retires one of these, pick the cheapest
# current replacement from the same catalog file (chat entries: 1–2 per
# provider, cheapest first) and change it HERE — no test hard-codes a
# chat model name. `test_e2e_models_exist_in_catalog` fails loudly if a
# name has left the catalog.
E2E_MODELS: Dict[str, List[str]] = {
    "openai": [
        "gpt-6-luna",              # cheapest current GPT chat (reasoning, prompt cache)
        "gpt-image-2.6-flare",     # image_generation
        "tts-1",                   # audio_speech
        "gpt-transcribe",          # audio_transcription
        "text-embedding-3-small",  # embedding
    ],
    "anthropic": ["claude-haiku-5-5"],
    # flash first: it supports cachedContents (prompt-cache tier); lite is cheaper.
    "gemini": ["gemini-flash-latest", "gemini-flash-lite-latest"],
    "xai": ["grok-4.3"],
    "deepseek": ["deepseek-flash"],
    "moonshot": ["kimi-k3"],
    "groq": ["openai/gpt-oss-20b"],
    "minimax": ["MiniMax-M3"],
    "fal_ai": ["luma/agent/ray/v3.2/text-to-video"],
}

# Multimodal tier models (each also listed in E2E_MODELS above).
E2E_IMAGE_MODEL = "gpt-image-2.6-flare"
E2E_TTS_MODEL = "tts-1"
E2E_STT_MODEL = "gpt-transcribe"
E2E_EMBEDDING_MODEL = "text-embedding-3-small"
E2E_VIDEO_MODEL = "luma/agent/ray/v3.2/text-to-video"
_E2E_NON_CHAT = {
    E2E_IMAGE_MODEL, E2E_TTS_MODEL, E2E_STT_MODEL, E2E_EMBEDDING_MODEL,
    E2E_VIDEO_MODEL,
}


def e2e_chat_model_names(provider: Optional[str] = None) -> List[str]:
    """Chat-capable E2E model names, optionally for one provider."""
    out: List[str] = []
    for slug, names in E2E_MODELS.items():
        if provider and slug != provider:
            continue
        out.extend(n for n in names if n not in _E2E_NON_CHAT)
    return out

REQUEST_TIMEOUT = float(os.environ.get("BRUTOR_LIVE_TIMEOUT", "30"))


# ─────────────────────────────────────────────────────────────────────
# Provider keys — same loader convention as test_seed_models_health_check
# ─────────────────────────────────────────────────────────────────────

_KEYS_FILE = Path(__file__).parent.parent / ".secrets" / "provider_keys.env"

# Map of env-var name → control-plane provider slug. The control plane
# stores ``LLModel.provider`` as the lowercase slug. Keep this list in
# sync with the providers we have probes for in
# ``test_seed_models_health_check.py``.
_KEY_TO_PROVIDER: Dict[str, str] = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    # Google AI Studio uses the provider slug `gemini` in the catalog
    # (see seed in `init_llm_providers.PROVIDER_SEED_CATALOG`). The
    # `GOOGLE_API_KEY` env var is the conventional name AI Studio docs
    # use, hence the legacy-shaped mapping. Vertex is a separate slug
    # (`vertex_ai`) and uses a different auth flow (service account /
    # ADC), not a plain API key — we don't claim to provision it here.
    "GOOGLE_API_KEY": "gemini",
    "XAI_API_KEY": "xai",
    "DEEPSEEK_API_KEY": "deepseek",
    "MOONSHOT_API_KEY": "moonshot",
    "GROQ_API_KEY": "groq",
    "MINIMAX_API_KEY": "minimax",
    # Fal.ai — async queue API for video generation (luma-dream-machine,
    # kling-video, runway-gen3) and a bunch of image models. Listed
    # here so a tenant with `FAL_KEY` set in `provider_keys.env` runs
    # the multimodal video tests against `fal_ai/fal-ai/luma-dream-machine`
    # (the default-enabled video model in the seed catalogue).
    "FAL_KEY": "fal_ai",
}


def _load_provider_keys() -> Dict[str, str]:
    """Return ``{env_var_name: api_key}``. File wins over os.environ so
    a stale shell export can't override the file you actually edited."""
    keys: Dict[str, str] = {}
    if _KEYS_FILE.exists():
        for raw in _KEYS_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip().strip("'").strip('"')
    for env_name in _KEY_TO_PROVIDER:
        if env_name not in keys and env_name in os.environ:
            keys[env_name] = os.environ[env_name]
    # Drop empty values — easier than asking every consumer to filter.
    return {k: v for k, v in keys.items() if v}


# ─────────────────────────────────────────────────────────────────────
# Out-of-credit provider keys — detected explicitly, reported loudly
# ─────────────────────────────────────────────────────────────────────
#
# A provider refusing because the ACCOUNT is out of money is not "a model is
# unavailable": every model of that provider is dark until someone tops up.
# Tests call `check_out_of_credit(provider, status, body)` on any provider
# error; a hit is recorded here, the test FAILS with an "OUT OF CREDIT"
# reason, the terminal summary lists the provider, and the session exits
# non-zero even if nothing else failed.

OUT_OF_CREDIT: Dict[str, str] = {}

# Each provider's real billing-exhaustion error code / text (lower-cased).
_BILLING_MARKERS: Dict[str, Tuple[str, ...]] = {
    # 429 {"error":{"code":"insufficient_quota","message":"You exceeded your
    # current quota, please check your plan and billing details…"}}
    "openai": ("insufficient_quota", "exceeded your current quota",
               "billing_hard_limit_reached", "credit_balance"),
    # 400 invalid_request_error "Your credit balance is too low to access the
    # Anthropic API. Please go to Plans & Billing…"
    "anthropic": ("credit balance is too low",),
    # AI Studio prepaid / billing: "Your prepayment credits are depleted",
    # 403 PERMISSION_DENIED "…billing to be enabled", spend cap reached.
    "gemini": ("prepayment credits are depleted", "credits are depleted",
               "billing to be enabled", "billing account", "spending cap"),
    # 403 "Your team … has either used all available credits or reached its
    # monthly spending limit" / "does not have any credits or licenses".
    "xai": ("used all available credits", "monthly spending limit",
            "does not have any credits"),
    # 402 {"error":{"message":"Insufficient Balance","type":"unknown_error"}}
    "deepseek": ("insufficient balance",),
    # 429 exceeded_current_quota_error "Your account … is suspended / has
    # insufficient balance, please check your plan and billing details".
    "moonshot": ("exceeded_current_quota_error", "insufficient balance",
                 "account is suspended"),
    # Groq: organisation restricted for billing / spend limit reached.
    "groq": ("organization_restricted", "spend limit", "billing"),
    # MiniMax base_resp {"status_code":1008,"status_msg":"insufficient balance"}
    "minimax": ("insufficient balance", '"status_code":1008', "status_code: 1008"),
    # fal 403 "User is locked. Reason: Exhausted balance. Top up your balance
    # at fal.ai/dashboard/billing."
    "fal_ai": ("exhausted balance", "top up your balance", "user is locked"),
}
_BILLING_GENERIC = ("payment required", "out of credit", "insufficient credit")


def check_out_of_credit(provider: str, status: int, body: str) -> Optional[str]:
    """Record + return a reason when ``body`` is ``provider`` refusing for
    billing / credit exhaustion. HTTP 402 is always billing."""
    text = (body or "").lower()
    hit = status == 402 or any(
        m in text for m in _BILLING_MARKERS.get(provider, ()) + _BILLING_GENERIC
    )
    if not hit:
        return None
    reason = f"OUT OF CREDIT: {provider} key refused for billing ({status}): {short(body, 240)}"
    OUT_OF_CREDIT.setdefault(provider, reason)
    return reason


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    if not OUT_OF_CREDIT:
        return
    terminalreporter.section("PROVIDER KEYS OUT OF CREDIT", sep="!", red=True, bold=True)
    for provider, reason in sorted(OUT_OF_CREDIT.items()):
        terminalreporter.write_line(f"  {provider}: {reason}", red=True, bold=True)
    terminalreporter.write_line(
        "  Top up these accounts (or replace the keys in tests/.secrets/"
        "provider_keys.env). The session exits non-zero while any keyed "
        "provider is out of credit.", red=True,
    )


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if OUT_OF_CREDIT and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ─────────────────────────────────────────────────────────────────────
# Pytest hooks: auto-mark + module-level skip
# ─────────────────────────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Tag every test in this directory with ``live_e2e``.

    Saves us from sprinkling the marker on every function and means
    a contributor can't forget it.
    """
    here = Path(__file__).parent.resolve()
    marker = pytest.mark.live_e2e
    for item in items:
        try:
            test_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if here in test_path.parents or test_path.parent == here:
            item.add_marker(marker)


def _services_reachable() -> Tuple[bool, str]:
    """Probe both control and proxy. Return ``(ok, reason)``."""
    try:
        with httpx.Client(timeout=2.0) as c:
            r1 = c.get(f"{CONTROL_URL}/health")
            if r1.status_code >= 500:
                return False, f"control /health → {r1.status_code}"
            r2 = c.get(f"{PROXY_URL}/health")
            if r2.status_code >= 500:
                return False, f"proxy /health → {r2.status_code}"
    except httpx.HTTPError as e:
        return False, f"connection failure: {type(e).__name__}: {e}"
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def _enforce_live_prereqs() -> None:
    """Session guard. Skips every test in this directory with a clear
    reason when the running stack isn't reachable or no provider keys
    are configured. Same convention as the ``live_api`` suite — fixtures
    skip themselves rather than exiting pytest, so a default
    ``pytest`` run that happens to collect this directory still
    produces a clean skip line per test instead of an error."""
    ok, reason = _services_reachable()
    if not ok:
        pytest.skip(
            f"live stack not reachable — {reason}. "
            f"Bring up Postgres + control ({CONTROL_URL}) + proxy "
            f"({PROXY_URL}) before running this suite.",
            allow_module_level=True,
        )
    if not _load_provider_keys():
        pytest.skip(
            "no provider keys found — drop at least one of "
            f"{sorted(_KEY_TO_PROVIDER)} into "
            f"tests/.secrets/provider_keys.env (or export it).",
            allow_module_level=True,
        )


# ─────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def control_client() -> httpx.Client:
    with httpx.Client(base_url=CONTROL_URL, timeout=REQUEST_TIMEOUT) as c:
        yield c


@pytest.fixture(scope="session")
def proxy_client() -> httpx.Client:
    with httpx.Client(base_url=PROXY_URL, timeout=REQUEST_TIMEOUT) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────
# Provider keys + provider→models grouping
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def provider_keys() -> Dict[str, str]:
    """``{env_var_name: api_key}`` for whichever providers are keyed."""
    return _load_provider_keys()


@pytest.fixture(scope="session")
def keyed_providers(provider_keys: Dict[str, str]) -> Dict[str, str]:
    """``{provider_slug: api_key}`` — the slug as stored in
    ``LLModel.provider`` (e.g. ``"openai"``)."""
    out: Dict[str, str] = {}
    for env_name, key in provider_keys.items():
        slug = _KEY_TO_PROVIDER.get(env_name)
        if slug:
            out[slug] = key
    return out


# ─────────────────────────────────────────────────────────────────────
# Auth — admin + trial tokens
# ─────────────────────────────────────────────────────────────────────


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def default_tenant_id() -> str:
    """The default tenant's id.

    Resolved from ``BRUTOR_TENANT_ID`` (self-hosted seed default
    ``"default"``). We don't list tenants here because that endpoint is
    system-admin-only — and the live suite logs in as the tenant admin
    of the default tenant, not a system admin."""
    return DEFAULT_TENANT_ID


@pytest.fixture(scope="session")
def admin_token(control_client: httpx.Client, default_tenant_id: str) -> str:
    """Tenant admin JWT for the default tenant.

    The suite drives the platform as the **tenant admin** of the
    default tenant (seeded as ``admin`` with the installation's own password), not as a system
    admin. That avoids the system-admin scope blindness on
    tenant-scoped routes (no `tenant_id` query param required) and
    matches how a real operator running this stack would interact
    with the admin API."""
    r = control_client.post(
        "/v1/admin/users/tenant/login",
        json={
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "tenant_id": default_tenant_id,
        },
    )
    if r.status_code != 200:
        pytest.fail(
            f"Tenant admin login failed ({r.status_code}): {r.text[:200]}. "
            f"Set BRUTOR_ADMIN_USERNAME / BRUTOR_ADMIN_PASSWORD / "
            f"BRUTOR_TENANT_ID if your seeded tenant admin uses "
            f"non-default credentials or lives in a non-'default' tenant."
        )
    body = r.json()
    token = body.get("access_token")
    assert token, f"tenant login response missing access_token: {body}"
    return token


@pytest.fixture(scope="session")
def admin_headers(admin_token: str) -> Dict[str, str]:
    return _bearer(admin_token)


@pytest.fixture(scope="session")
def admin_tenant_id(default_tenant_id: str) -> str:
    """Alias — same value, kept for tests that read 'the admin's tenant'."""
    return default_tenant_id


@pytest.fixture(scope="session")
def trial_token(
    proxy_client: httpx.Client,
    default_tenant_id: str,
    e2e_environment: Dict[str, Any],
) -> str:
    """End-user JWT for the seeded ``trial`` portal user.

    Depends on ``e2e_environment`` so the trial user is already a member
    of E2ETests (and the E2E models / skill / MCP bindings exist) before
    the token is minted — the trial user has no other group on a fresh
    stack, so without it every portal call would see nothing.

    Portal login requires ``X-Tenant-ID`` — it tells the proxy which
    tenant's user table to authenticate against. There is no admin API
    to reset an end-user's password; if this fails, recreate the trial
    user in Admin UI → Users with ``BRUTOR_TRIAL_PASSWORD``."""
    r = proxy_client.post(
        "/v1/portal/auth/login",
        json={"username": TRIAL_USERNAME, "password": TRIAL_PASSWORD},
        headers={"X-Tenant-ID": default_tenant_id},
    )
    if r.status_code != 200:
        pytest.fail(
            f"Trial login failed ({r.status_code}): {r.text[:200]}. "
            f"Set BRUTOR_TRIAL_USERNAME / BRUTOR_TRIAL_PASSWORD if your "
            f"trial bundle uses non-default credentials."
        )
    body = r.json()
    token = body.get("access_token")
    assert token, f"trial login missing access_token: {body}"
    return token


@pytest.fixture(scope="session")
def trial_headers(
    trial_token: str, default_tenant_id: str, e2e_group_id: str
) -> Dict[str, str]:
    """Headers a portal user sends on every proxy call.

    - ``Authorization: Bearer`` is the standard scheme — accepted on
      every portal route (auth, models, etc.).
    - ``X-Gateway-Authorization: Bearer`` is what the **chat preflight**
      reads to populate ``auth_ctx.gateway_user_id``. Without it, the
      preflight treats the request as anonymous and rejects model
      access with 403, even when the user is in a group that owns the
      model. We send both so a single token works on every endpoint.
    - ``X-Tenant-ID`` lets middleware route the tenant context.
    - ``X-Resource-Group`` pins every runtime call to E2ETests, the one
      group the suite provisions into (see README "One group").
    """
    return {
        **_bearer(trial_token),
        "X-Gateway-Authorization": f"Bearer {trial_token}",
        "X-Tenant-ID": default_tenant_id,
        "X-Resource-Group": e2e_group_id,
    }


# ─────────────────────────────────────────────────────────────────────
# Tenant resources — Default group, Hello World skill
# ─────────────────────────────────────────────────────────────────────


# The control plane's group-assign endpoint accepts the literal
# string ``"default"`` as a sentinel that resolves to the tenant's
# Default resource group. Using the sentinel avoids needing a
# tenant-scoped resource-group listing — the system admin's
# ``/v1/admin/resource-groups`` returns an empty list because the
# endpoint scopes by ``admin.tenant_id`` (which is null for system
# admins). The sentinel is the supported, documented escape hatch.
DEFAULT_GROUP_SENTINEL = "default"


@pytest.fixture(scope="session")
def default_group_id(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
) -> str:
    """Resolve the **Default** resource group id.

    Tenant admins can list groups directly — the endpoint scopes
    automatically by ``admin.tenant_id``. We still fall back to the
    ``"default"`` sentinel if the listing comes back empty for any
    reason; the control plane resolves it server-side."""
    r = control_client.get("/v1/admin/resource-groups", headers=admin_headers)
    if r.status_code == 200:
        body = r.json()
        raw = body.get("groups") if isinstance(body, dict) else body
        if isinstance(raw, dict):
            raw = list(raw.values())
        if isinstance(raw, list):
            for g in raw:
                if isinstance(g, dict) and g.get("name") == DEFAULT_GROUP_NAME and g.get("id"):
                    return g["id"]
    return DEFAULT_GROUP_SENTINEL


@pytest.fixture(scope="session")
def hello_world_tool_def(
    proxy_client: httpx.Client, trial_headers: Dict[str, str]
) -> Optional[Dict]:
    """Build an OpenAI-style tool definition for the trial user's
    hello-world skill, by reading the proxy's skill catalog. Returns
    None if the skill isn't visible to this user — the chat assertion
    handles that as a hard failure with a useful message."""
    r = proxy_client.get(
        "/v1/proxy/skills/catalog",
        headers=trial_headers,
    )
    if r.status_code != 200:
        return None
    body = r.json()
    catalog: List[Dict] = (
        body.get("skills")
        if isinstance(body, dict) and "skills" in body
        else (body if isinstance(body, list) else [])
    )
    skill = next(
        (s for s in catalog if s.get("name") == HELLO_WORLD_SKILL_NAME),
        None,
    )
    if not skill:
        return None
    return {
        "type": "function",
        "function": {
            "name": skill["name"],
            "description": skill.get("description")
            or "Demo skill that responds to greetings.",
            "parameters": skill.get("parameters")
            or {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


# ─────────────────────────────────────────────────────────────────────
# E2ETests group + idempotent provisioning (models, skill, MCP, trial)
# ─────────────────────────────────────────────────────────────────────


def _items(body: Any, *keys: str) -> List[Dict[str, Any]]:
    """Unwrap the list out of an admin list envelope (``{"groups": [...]}``,
    ``{"models": {id: {...}}}``, a bare list, ...)."""
    if isinstance(body, dict):
        for k in keys:
            if k in body:
                body = body[k]
                break
    if isinstance(body, dict):
        body = list(body.values())
    return [x for x in body if isinstance(x, dict)] if isinstance(body, list) else []


def _expect_ok(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code not in (200, 201, 204):
        pytest.fail(f"{what} failed ({r.status_code}): {short(r.text)}")
    return r


def current_group_ids(row: Dict[str, Any]) -> List[str]:
    """A model / config row's group memberships across the response shapes
    (``group_ids`` vs ``groups[].id`` / ``resource_groups[].id``)."""
    if isinstance(row.get("group_ids"), list):
        return [g for g in row["group_ids"] if g]
    for key in ("resource_groups", "groups"):
        rg = row.get(key)
        if isinstance(rg, list):
            return [g.get("id") for g in rg if isinstance(g, dict) and g.get("id")]
    return []


def list_configured_models(
    client: httpx.Client, headers: Dict[str, str], provider: str
) -> List[Dict[str, Any]]:
    """The tenant's CONFIGURED models (``llm_model`` rows — Admin UI → AI
    Models → Configuration) for one provider. NOT the catalog: catalog
    entries only become configured models when imported."""
    r = _expect_ok(
        client.get(
            f"/v1/admin/llms?provider={provider}&include_groups=true",
            headers=headers,
        ),
        f"GET /v1/admin/llms?provider={provider}",
    )
    return _items(r.json(), "models")


def list_catalog_entries(
    client: httpx.Client, headers: Dict[str, str], provider: str
) -> List[Dict[str, Any]]:
    """The global LLM catalog for one provider (Admin UI → Browse)."""
    r = _expect_ok(
        client.get(
            f"/v1/admin/llm-catalog?provider_key={provider}&limit=500",
            headers=headers,
        ),
        f"GET /v1/admin/llm-catalog?provider_key={provider}",
    )
    return _items(r.json(), "catalog", "entries", "items")


@pytest.fixture(scope="session")
def e2e_group_id(control_client: httpx.Client, admin_headers: Dict[str, str]) -> str:
    """Id of the E2ETests resource group — created if missing, re-activated
    if an operator disabled it (keys bound to an inactive group are refused
    by the proxy)."""
    r = _expect_ok(
        control_client.get("/v1/admin/resource-groups", headers=admin_headers),
        "list resource groups",
    )
    group = next(
        (g for g in _items(r.json(), "groups") if g.get("name") == E2E_GROUP_NAME),
        None,
    )
    if group is None:
        r = _expect_ok(
            control_client.post(
                "/v1/admin/resource-groups",
                headers=admin_headers,
                json={
                    "name": E2E_GROUP_NAME,
                    "display_name": E2E_GROUP_NAME,
                    "description": (
                        "Everything the live end-to-end suite provisions "
                        "(tests/live). Safe to delete between runs."
                    ),
                },
            ),
            f"create resource group {E2E_GROUP_NAME}",
        )
        group = r.json().get("group") or r.json()
    elif group.get("is_active") is False:
        _expect_ok(
            control_client.patch(
                f"/v1/admin/resource-groups/{group['id']}",
                headers=admin_headers,
                json={"is_active": True},
            ),
            f"re-activate {E2E_GROUP_NAME}",
        )
    return group["id"]


# Modes the gateway sweep (test_e2e_gateway_sweep.py) exercises with one
# minimal call each. Video is limited to E2E_VIDEO_MODEL: every other video
# entry renders seconds of footage per call.
SWEEP_MODES = {
    "chat", "responses", "embedding",
    "image_generation", "audio_speech", "audio_transcription",
    "video_generation",
}


def sweep_entry_excluded(entry: Dict[str, Any]) -> Optional[str]:
    """Why a catalog entry is NOT swept (None = swept).

    Only the catalog's own unusable markers exclude an entry: ``deprecated``
    true, or a ``retire_date`` on/before today. ``recommended`` (the seed's
    default-enabled flag) is deliberately NOT used — the sweep covers the
    whole catalog, not just what the seed imports."""
    mode = (entry.get("mode") or "chat").lower()
    if mode not in SWEEP_MODES:
        return f"mode {mode} not swept"
    if mode == "video_generation" and entry.get("model_name") != E2E_VIDEO_MODEL:
        return "video: only the representative model is swept (spend)"
    if entry.get("deprecated") is True:
        return "catalog marks it deprecated"
    retire = entry.get("retire_date")
    if retire:
        import datetime as _dt
        try:
            if _dt.date.fromisoformat(str(retire)[:10]) <= _dt.date.today():
                return f"catalog retire_date {retire} has passed"
        except ValueError:
            pass
    return None


def sweep_catalog(
    client: httpx.Client, headers: Dict[str, str], providers: List[str]
) -> List[Dict[str, Any]]:
    """Every catalog entry of ``providers`` the gateway sweep covers."""
    out: List[Dict[str, Any]] = []
    for slug in providers:
        for e in list_catalog_entries(client, headers, slug):
            if sweep_entry_excluded(e) is None:
                out.append(e)
    return out


@pytest.fixture(scope="session")
def provisioned_models(
    keyed_providers: Dict[str, str],
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    e2e_group_id: str,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{provider: {model_name: configured row}}`` for E2E_MODELS ∪ the
    catalog-derived sweep set of every keyed provider — each configured,
    keyed, enabled and bound to E2ETests.

    Configured models and the catalog are separate: a model that is not
    configured yet is brought in exactly the way the Admin UI's Browse → Add
    flow does it, ``POST /v1/admin/llm-catalog/{catalog_id}/import`` (with
    api_key, enabled and group_ids=[E2ETests]). Idempotent: an existing
    configured row is reused — its key is (re)set, it is enabled, and
    E2ETests is ADDED to its groups (other memberships, e.g. a seeded Default
    binding, are left alone). The Default group is never added.
    """
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for slug, api_key in keyed_providers.items():
        catalog = {
            e.get("model_name"): e
            for e in list_catalog_entries(control_client, admin_headers, slug)
        }
        wanted = list(E2E_MODELS.get(slug, []))
        wanted += [
            n for n, e in catalog.items()
            if n and n not in wanted and sweep_entry_excluded(e) is None
        ]
        if not wanted:
            continue
        configured: Dict[str, Dict[str, Any]] = {}
        for m in list_configured_models(control_client, admin_headers, slug):
            name = m.get("model_name")
            # Prefer the row already in E2ETests, then an enabled one, when
            # the tenant has several rows for one model_name.
            prev = configured.get(name)
            if name and (
                prev is None
                or (e2e_group_id in current_group_ids(m) and e2e_group_id not in current_group_ids(prev))
                or (m.get("enabled") and not prev.get("enabled")
                    and e2e_group_id not in current_group_ids(prev))
            ):
                configured[name] = m
        ids: List[str] = []
        for name in wanted:
            row = configured.get(name)
            if row is None:
                entry = catalog.get(name)
                if not entry:
                    pytest.fail(
                        f"E2E model {slug}/{name} is not in the LLM catalog — "
                        f"update E2E_MODELS in tests/live/conftest.py to a "
                        f"current entry of that provider's catalog file."
                    )
                r = _expect_ok(
                    control_client.post(
                        f"/v1/admin/llm-catalog/{entry['id']}/import",
                        headers=admin_headers,
                        json={
                            "api_key": api_key,
                            "enabled": True,
                            "group_ids": [e2e_group_id],
                        },
                    ),
                    f"import catalog entry {entry['id']}",
                )
                ids.append((r.json().get("model") or r.json())["id"])
                continue
            patch: Dict[str, Any] = {"api_key": api_key}
            if not row.get("enabled"):
                patch["enabled"] = True
            _expect_ok(
                control_client.patch(
                    f"/v1/admin/llms/{row['id']}", headers=admin_headers, json=patch
                ),
                f"PATCH {slug}/{name}",
            )
            groups = current_group_ids(row)
            if e2e_group_id not in groups:
                _expect_ok(
                    control_client.post(
                        f"/v1/admin/llms/{row['id']}/groups/assign",
                        headers=admin_headers,
                        json={"group_ids": sorted(set(groups) | {e2e_group_id})},
                    ),
                    f"bind {slug}/{name} to {E2E_GROUP_NAME}",
                )
            ids.append(row["id"])
        fresh = {
            m["id"]: m for m in list_configured_models(control_client, admin_headers, slug)
        }
        out[slug] = {fresh[i]["model_name"]: fresh[i] for i in ids if i in fresh}
    return out


@pytest.fixture(scope="session")
def e2e_models(
    provisioned_models: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """``{provider: [configured row, ...]}`` for the E2E_MODELS subset only
    (the focused tests' representative set), in E2E_MODELS order."""
    return {
        slug: [rows[n] for n in E2E_MODELS.get(slug, []) if n in rows]
        for slug, rows in provisioned_models.items()
        if slug in E2E_MODELS
    }


def _find_end_user_id(
    client: httpx.Client, headers: Dict[str, str], username: str
) -> Optional[Any]:
    r = client.get(f"/v1/admin/end-users?search={username}", headers=headers)
    if r.status_code != 200:
        return None
    return next(
        (u.get("id") for u in _items(r.json(), "users") if u.get("username") == username),
        None,
    )


@pytest.fixture(scope="session")
def e2e_environment(
    control_client: httpx.Client,
    admin_headers: Dict[str, str],
    e2e_group_id: str,
    e2e_models: Dict[str, List[Dict[str, Any]]],
    provisioned_models: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Provision every model (E2E_MODELS ∪ catalog sweep set, see
    ``provisioned_models``), then bind the suite's generic dependencies to E2ETests (all additive and
    idempotent; nothing is removed from Default):

    * the seeded ``hello-world`` skill (skill → group binding),
    * the Agent Skills system MCP server config,
    * the tenant's default guardrail policy (so the seeded policy governs
      E2E traffic — `test_e2e_guardrail_enforcement`),
    * the Default prompt-cache policy (`test_e2e_prompt_caching`),
    * the trial user's membership (first group joined → primary).
    """
    g = e2e_group_id

    # Skill.
    r = _expect_ok(control_client.get("/v1/admin/agent-skills", headers=admin_headers), "list skills")
    for sk in _items(r.json(), "skills"):
        if sk.get("name") == HELLO_WORLD_SKILL_NAME and g not in current_group_ids(sk):
            _expect_ok(
                control_client.post(
                    f"/v1/admin/agent-skills/{sk['id']}/groups",
                    headers=admin_headers, json={"group_ids": [g]},
                ),
                "bind hello-world skill",
            )

    # Agent Skills system MCP server config.
    r = _expect_ok(control_client.get("/v1/admin/server-configs", headers=admin_headers), "list server configs")
    for cfg in _items(r.json(), "configs", "server_configs"):
        if str(cfg.get("mcp_server_id") or "").startswith("system-agent-skill-server") \
                and g not in current_group_ids(cfg):
            _expect_ok(
                control_client.patch(
                    f"/v1/admin/server-configs/{cfg['id']}", headers=admin_headers,
                    json={"group_ids": sorted(set(current_group_ids(cfg)) | {g})},
                ),
                "bind Agent Skills server config",
            )

    # Default guardrail policy (PATCH replaces the set → merge).
    r = _expect_ok(control_client.get("/v1/admin/guardrails/configs", headers=admin_headers), "list guardrails")
    for cfg in _items(r.json(), "configs"):
        if cfg.get("is_default") and g not in current_group_ids(cfg):
            _expect_ok(
                control_client.patch(
                    f"/v1/admin/guardrails/configs/{cfg['id']}/groups", headers=admin_headers,
                    json={"group_ids": sorted(set(current_group_ids(cfg)) | {g})},
                ),
                "bind default guardrail policy",
            )

    # Default prompt-cache policy (POST adds).
    r = _expect_ok(control_client.get("/v1/admin/prompt-cache/policies", headers=admin_headers), "list prompt-cache policies")
    for pol in _items(r.json(), "policies"):
        if pol.get("name") == "Default" and g not in (pol.get("assigned_group_ids") or []):
            _expect_ok(
                control_client.post(
                    f"/v1/admin/prompt-cache/policies/{pol['id']}/groups",
                    headers=admin_headers, json={"group_ids": [g]},
                ),
                "bind Default prompt-cache policy",
            )

    # Trial user: exists, logs in with TRIAL_PASSWORD, is in E2ETests.
    trial_id = _ensure_trial_user(control_client, admin_headers, g)
    # Membership (already-a-member is reported as skipped, not an error).
    _expect_ok(
        control_client.post(
            f"/v1/admin/resource-groups/{g}/members",
            headers=admin_headers, json={"user_ids": [str(trial_id)]},
        ),
        f"add {TRIAL_USERNAME} to {E2E_GROUP_NAME}",
    )
    policy_changes = enforce_policy_scoping(control_client, admin_headers)
    return {
        "group_id": g,
        "policy_changes": policy_changes,
        "models": e2e_models,
        "trial_user_id": trial_id,
        "api_key": _ensure_e2e_api_key(control_client, admin_headers, g),
    }


@pytest.fixture(scope="session", autouse=True)
def _e2e_setup(_enforce_live_prereqs: None, e2e_environment: Dict[str, Any]) -> None:
    """Runs the E2E provisioning once per session for EVERY live test
    (including governance/), after the reachability / key guard. A fresh
    stack with only the seed needs nothing else."""


@pytest.fixture(scope="session")
def e2e_api_key(e2e_environment: Dict[str, Any]) -> str:
    """A shared API key bound to E2ETests (created once, then reused)."""
    return e2e_environment["api_key"]


def _portal_login_status(username: str, password: str) -> int:
    with httpx.Client(base_url=PROXY_URL, timeout=REQUEST_TIMEOUT) as c:
        r = c.post(
            "/v1/portal/auth/login",
            json={"username": username, "password": password},
            headers={"X-Tenant-ID": DEFAULT_TENANT_ID},
        )
    return r.status_code


def _ensure_trial_user(
    client: httpx.Client, headers: Dict[str, str], group_id: str
) -> Any:
    """Make sure the ``trial`` end user exists and accepts TRIAL_PASSWORD.

    There is no admin endpoint to reset an end user's password, so a trial
    user whose password drifted is deleted and recreated through the admin
    API (Admin UI → Users → Delete / Add) — never by editing the DB."""
    uid = _find_end_user_id(client, headers, TRIAL_USERNAME)
    if uid is not None and _portal_login_status(TRIAL_USERNAME, TRIAL_PASSWORD) == 200:
        return uid
    if uid is not None:
        _expect_ok(
            client.delete(f"/v1/admin/end-users/{uid}", headers=headers),
            f"delete {TRIAL_USERNAME} (password drifted)",
        )
    _expect_ok(
        client.post(
            "/v1/admin/end-users",
            headers=headers,
            json={
                "username": TRIAL_USERNAME,
                "email": os.environ.get("BRUTOR_TRIAL_EMAIL", "trial@e2e.brutor.invalid"),
                "password": TRIAL_PASSWORD,
                "display_name": "Trial (E2E)",
                "group_ids": [group_id],
                "send_welcome_email": False,
            },
        ),
        f"create end user {TRIAL_USERNAME}",
    )
    uid = _find_end_user_id(client, headers, TRIAL_USERNAME)
    status = _portal_login_status(TRIAL_USERNAME, TRIAL_PASSWORD)
    if uid is None or status != 200:
        pytest.fail(f"{TRIAL_USERNAME} still cannot log in after recreate ({status})")
    return uid


# The cleartext of an API key is only returned at creation, so the suite
# keeps the one it minted in the (gitignored) secrets dir and reuses it
# while the proxy still accepts it.
_E2E_KEY_FILE = _KEYS_FILE.parent / "e2e_api_key.env"
E2E_API_KEY_NAME = "e2e-suite-shared"


def _ensure_e2e_api_key(
    client: httpx.Client, headers: Dict[str, str], group_id: str
) -> str:
    try:
        cached = _E2E_KEY_FILE.read_text().strip().split("=", 1)[-1] if _E2E_KEY_FILE.exists() else ""
    except OSError:
        cached = ""
    if cached:
        with httpx.Client(base_url=PROXY_URL, timeout=REQUEST_TIMEOUT) as c:
            r = c.get("/v1/proxy/llm/models", headers={"Authorization": f"Bearer {cached}"})
        if r.status_code == 200:
            return cached
    r = _expect_ok(
        client.post(
            f"/v1/admin/resource-groups/{group_id}/api-keys",
            headers=headers,
            json={
                "name": f"{E2E_API_KEY_NAME}-{os.getpid()}",
                "description": "Live e2e suite shared key (tests/live) — safe to revoke",
                "access_mode": "shared",
            },
        ),
        f"create {E2E_GROUP_NAME} API key",
    )
    body = r.json()
    key = body.get("api_key") or body.get("key") or body.get("plain_key")
    assert key, "API-key create response carried no cleartext key"
    try:
        _E2E_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _E2E_KEY_FILE.write_text(f"E2E_API_KEY={key}\n")
        _E2E_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


# ─────────────────────────────────────────────────────────────────────
# Policy scoping — nothing the suite provisions may be tenant-wide
# ─────────────────────────────────────────────────────────────────────
#
# A policy with NO group binding applies to every group's traffic, and one
# bound to Default reaches every Default member. The suite (and the demo
# provisioning scripts governance/ runs) must leave neither behind: each
# policy is either on E2ETests or on the demo group whose scenario tests it.
# The tenant's own seeded defaults (ids ``<kind>-default-<tenant>``, bound to
# Default by tenant provisioning) are not suite-created and are left alone.


def _is_seeded_default(row: Dict[str, Any]) -> bool:
    rid = str(row.get("id") or "")
    return rid.endswith(f"-default-{DEFAULT_TENANT_ID}")


def _policy_inventory(
    client: httpx.Client, headers: Dict[str, str]
) -> List[Tuple[str, Dict[str, Any], List[str]]]:
    """``(kind, row, group_ids)`` for every enabled, non-seeded policy row."""
    out: List[Tuple[str, Dict[str, Any], List[str]]] = []
    sources = [
        ("semantic_policy", "/v1/admin/semantic-policies", ("policies",)),
        ("guardrail", "/v1/admin/guardrails/configs", ("configs",)),
        ("argument_policy", "/v1/admin/argument-policies", ("policies",)),
        ("semantic_cache", "/v1/admin/semantic-cache/configs", ("configs",)),
        ("prompt_cache", "/v1/admin/prompt-cache/policies", ("policies",)),
    ]
    for kind, path, keys in sources:
        r = client.get(path, headers=headers)
        if r.status_code != 200:
            continue
        for row in _items(r.json(), *keys):
            if row.get("enabled") is False or _is_seeded_default(row):
                continue
            groups = (
                row.get("resource_group_ids")
                or row.get("assigned_group_ids")
                or current_group_ids(row)
                or []
            )
            out.append((kind, row, list(groups)))
    return out


def _rebind_policy(
    client: httpx.Client, headers: Dict[str, str], kind: str,
    row: Dict[str, Any], group_ids: List[str],
) -> httpx.Response:
    pid = row["id"]
    if kind == "semantic_policy":
        return client.patch(f"/v1/admin/semantic-policies/{pid}", headers=headers,
                            json={"resource_group_ids": group_ids})
    if kind == "guardrail":
        return client.patch(f"/v1/admin/guardrails/configs/{pid}/groups", headers=headers,
                            json={"group_ids": group_ids})
    if kind == "argument_policy":
        return client.patch(f"/v1/admin/argument-policies/{pid}/groups", headers=headers,
                            json={"group_ids": group_ids})
    if kind == "semantic_cache":
        return client.post(f"/v1/admin/semantic-cache/configs/{pid}/groups", headers=headers,
                           json={"group_ids": group_ids})
    # prompt_cache: POST adds; Default (if any) is removed separately.
    r = client.post(f"/v1/admin/prompt-cache/policies/{pid}/groups", headers=headers,
                    json={"group_ids": group_ids})
    return r


def scope_suite_policies(
    client: httpx.Client, headers: Dict[str, str], e2e_group: str, default_group: Optional[str],
) -> List[str]:
    """Rebind every enabled, non-seeded policy that is tenant-wide (no group)
    or bound to Default onto E2ETests (Default removed, other groups kept).
    Returns a human-readable list of what was changed."""
    changed: List[str] = []
    for kind, row, groups in _policy_inventory(client, headers):
        if groups and (not default_group or default_group not in groups):
            continue
        target = sorted({g for g in groups if g != default_group} | {e2e_group})
        r = _rebind_policy(client, headers, kind, row, target)
        if kind == "prompt_cache" and default_group in groups and r.status_code < 300:
            r = client.delete(
                f"/v1/admin/prompt-cache/policies/{row['id']}/groups/{default_group}",
                headers=headers,
            )
        _expect_ok(r, f"scope {kind} {row.get('name')!r} to {E2E_GROUP_NAME}")
        changed.append(
            f"{kind} {row.get('name')!r}: {'tenant-wide' if not groups else 'Default'} → {E2E_GROUP_NAME}"
        )
    return changed


def tenant_wide_policy_violations(
    client: httpx.Client, headers: Dict[str, str], default_group: Optional[str],
) -> List[str]:
    """Enabled, non-seeded policies that are tenant-wide or on Default."""
    bad = []
    for kind, row, groups in _policy_inventory(client, headers):
        if not groups:
            bad.append(f"{kind} {row.get('name')!r} ({row['id']}) has no group binding")
        elif default_group and default_group in groups:
            bad.append(f"{kind} {row.get('name')!r} ({row['id']}) is bound to Default")
    return bad


def enforce_policy_scoping(client: httpx.Client, headers: Dict[str, str]) -> List[str]:
    """Scope, then verify. Raises AssertionError listing any survivor."""
    r = _expect_ok(client.get("/v1/admin/resource-groups", headers=headers), "list groups")
    by_name = {g.get("name"): g.get("id") for g in _items(r.json(), "groups")}
    e2e, default = by_name.get(E2E_GROUP_NAME), by_name.get(DEFAULT_GROUP_NAME)
    assert e2e, f"{E2E_GROUP_NAME} group missing"
    changed = scope_suite_policies(client, headers, e2e, default)
    bad = tenant_wide_policy_violations(client, headers, default)
    assert not bad, (
        "enabled suite policies left tenant-wide or on Default after setup:\n  "
        + "\n  ".join(bad)
    )
    return changed


def ensure_catalog_mcp_server(
    client: httpx.Client,
    headers: Dict[str, str],
    *,
    catalog_entry_id: str,
    display_name: str,
    group_id: str,
) -> str:
    """Install an MCP catalog entry (Admin UI → MCP → Catalog → Import) if no
    server named ``display_name`` exists, bind its server config to
    ``group_id`` and refresh its capabilities. Returns the mcp_server id.

    A server the suite imports itself is bound to E2ETests ONLY (import binds
    it to the tenant's system group by default — we replace that). One an
    operator already installed keeps its groups and gains E2ETests."""
    r = _expect_ok(client.get("/v1/admin/mcp-servers", headers=headers), "list MCP servers")
    server = next(
        (s for s in _items(r.json(), "servers", "mcp_servers") if s.get("name") == display_name),
        None,
    )
    imported = False
    if server is None:
        r = _expect_ok(
            client.post(
                f"/v1/admin/mcp-catalog/{catalog_entry_id}/import",
                headers=headers, json={"enabled": True},
            ),
            f"import MCP catalog entry {catalog_entry_id}",
        )
        body = r.json()
        sid = body.get("mcp_server_id") or (body.get("server") or {}).get("id")
        imported = True
    else:
        sid = server["id"]
        if not server.get("enabled"):
            _expect_ok(
                client.patch(f"/v1/admin/mcp-servers/{sid}", headers=headers, json={"enabled": True}),
                f"enable MCP server {display_name}",
            )
    assert sid, f"could not resolve the MCP server id for {display_name}"
    r = _expect_ok(client.get(f"/v1/admin/server-configs/by-server/{sid}", headers=headers), "configs for server")
    for cfg in _items(r.json(), "configs", "server_configs"):
        target = [group_id] if imported else sorted(set(current_group_ids(cfg)) | {group_id})
        patch: Dict[str, Any] = {"group_ids": target}
        if not cfg.get("enabled"):
            patch["enabled"] = True
        if set(target) != set(current_group_ids(cfg)) or "enabled" in patch:
            _expect_ok(
                client.patch(f"/v1/admin/server-configs/{cfg['id']}", headers=headers, json=patch),
                f"bind {display_name} server config",
            )
    # Best effort — the proxy also discovers lazily on first tools/list.
    client.post(f"/v1/admin/mcp-servers/{sid}/discover-capabilities", headers=headers, timeout=60.0)
    return sid


@pytest.fixture(scope="session")
def e2e_chat_model(proxy_client: httpx.Client, trial_headers: Dict[str, str]) -> str:
    """The first E2E_MODELS chat model (E2E_MODELS order) the trial user can
    reach — for tests that need *a* working chat model, not a sweep."""
    r = proxy_client.get("/v1/portal/models", headers=trial_headers)
    if r.status_code != 200:
        pytest.fail(f"cannot list portal models ({r.status_code}): {short(r.text)}")
    visible = {
        m.get("model_name") or m.get("name")
        for m in _items(r.json(), "models")
    }
    for name in e2e_chat_model_names():
        if name in visible:
            return name
    pytest.fail(
        f"none of the E2E chat models is visible to {TRIAL_USERNAME}: "
        f"visible={sorted(v for v in visible if v)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Diagnostic helper — surface in test failures
# ─────────────────────────────────────────────────────────────────────


def short(text: str, n: int = 240) -> str:
    """Trim long bodies for readable assertion messages."""
    text = text or ""
    return text if len(text) <= n else f"{text[:n]}…"
