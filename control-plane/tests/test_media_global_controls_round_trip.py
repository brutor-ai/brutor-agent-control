# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
RG-wide MEDIA (multimodal) limits + governance round-trip.

``ResourceGroup.media_global_limits`` / ``media_global_governance`` are single
JSON columns keyed by LLModelMode value (audio_speech, audio_transcription,
image_generation, video_generation, embedding). One column covers every media
mode instead of a column-per-mode. The admin UI drives them through:

  * PATCH/GET ``/admin/resource-groups/{gid}/media-global-limits``
  * PATCH/GET ``/admin/resource-groups/{gid}/media-global-governance``

This exercises the real FastAPI routes: for EACH of the 5 modes we PATCH the
full key-set that mode supports and GET-assert every key survives — the
recurring "Pydantic response model strips undeclared fields" gotcha would drop
a key here if the response model ever stopped carrying the blob. We also pin
that ``ResourceGroupResponse`` (the list/detail response) declares both media
columns, since that is the other surface the values flow back through.

Run: pytest tests/test_media_global_controls_round_trip.py -q
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import MagicMock

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.models.admin.resource_group import ResourceGroupResponse
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import TenantRole, TenantRolePermission
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant

JWT_SECRET = os.environ["JWT_SECRET"]


# =============================================================================
# Media key sets — the full per-mode surface the admin form exposes.
# =============================================================================

# The keys a group may set in this release: the monthly keys, the warning
# percentages, `hard_stop: false` and the embedding mode are refused
# (`tests/test_unenforced_controls_refused.py`).
_BASE_LIMITS = {
    "daily_budget_usd": 5.0,
    "daily_requests": 500,
    "hard_stop": True,
}

MEDIA_LIMITS = {
    "audio_speech": {**_BASE_LIMITS, "daily_characters": 100000},
    "audio_transcription": {**_BASE_LIMITS, "daily_audio_seconds": 3600},
    "image_generation": {**_BASE_LIMITS, "daily_images": 200},
    "video_generation": {**_BASE_LIMITS, "daily_video_seconds": 300},
}

MEDIA_GOVERNANCE = {
    "audio_speech": {
        "voice_whitelist": ["alloy", "echo", "fable"],
        "response_format_whitelist": ["mp3", "opus"],
        "max_input_characters": 4000,
        "speed_min": 0.5,
        "speed_max": 1.5,
    },
    "audio_transcription": {
        "language_allowlist": ["en", "sv", "de"],
        "response_format_whitelist": ["json", "text"],
        "max_audio_size_mb": 25,
        "require_prompt_guardrail": True,
    },
    "image_generation": {
        "size_whitelist": ["1024x1024", "512x512"],
        "quality_whitelist": ["standard", "hd"],
        "style_whitelist": ["vivid", "natural"],
        "max_n": 4,
        "max_prompt_characters": 4000,
    },
    "video_generation": {
        "max_duration_seconds": 60,
        "resolution_whitelist": ["720p", "1080p"],
        "require_image_input": True,
        "max_prompt_characters": 2000,
    },
    "embedding": {
        "dim_whitelist": [256, 512, 1536],
        "max_batch_size": 96,
    },
}

MEDIA_MODES = ["audio_speech", "audio_transcription", "image_generation",
               "video_generation", "embedding"]
LIMIT_MODES = [mode for mode in MEDIA_MODES if mode in MEDIA_LIMITS]


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Full create_all — the router's dependencies (auth, audit) + to_dict()
    # relationships need a broad table set; the conftest JSONB->TEXT +
    # pg_notify shims keep this SQLite-safe.
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(
        create_resource_groups_router(usage_limit_enforcer=MagicMock(), logger=logger),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        setup.add(Tenant(
            id="t1", name="t1", display_name="T1",
            primary_contact_email="a@t1.invalid",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted",
        ))
        setup.flush()
        role = TenantRole(
            id="role-t1-admin", tenant_id="t1", name="Tenant Admin",
            description="full", is_system=True,
        )
        setup.add(role)
        setup.flush()
        for p in Permission:
            if p == Permission.SYSTEM_ADMIN:
                continue
            setup.add(TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=role.id, permission=p.value,
            ))
        auth.create_tenant_admin(
            username="adm1", email="adm1@t1.example", password="Secret123!",
            tenant_id="t1", db=setup, role_id="role-t1-admin",
        )
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, SessionFactory=SessionFactory)
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, SessionFactory):
        self.client = client
        self.SessionFactory = SessionFactory

    def login(self):
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "adm1", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def seed_rg(self, rg_id="rg-media"):
        s = self.SessionFactory()
        try:
            s.add(ResourceGroup(
                id=rg_id, tenant_id="t1", name="Engineering",
                display_name="Engineering", hierarchy_level=0, is_active=True,
            ))
            s.commit()
            return rg_id
        finally:
            s.close()


# =============================================================================
# Tests
# =============================================================================

def test_response_model_declares_media_columns():
    """Regression guard: ResourceGroupResponse must declare both media blobs
    or GET/list silently strips them (the recurring field-drop class)."""
    fields = set(ResourceGroupResponse.model_fields.keys())
    assert "media_global_limits" in fields
    assert "media_global_governance" in fields


def test_media_global_limits_round_trip(harness):
    headers = harness.login()
    rg = harness.seed_rg()

    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/media-global-limits",
        headers=headers,
        json={"media_global_limits": MEDIA_LIMITS},
    )
    assert r.status_code == 200, r.text

    r = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/media-global-limits",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    stored = r.json()["media_global_limits"]

    # Every mode + every key must survive.
    assert set(stored.keys()) == set(LIMIT_MODES)
    for mode in LIMIT_MODES:
        for key, val in MEDIA_LIMITS[mode].items():
            assert stored[mode][key] == val, f"limits {mode}.{key} did not round-trip"


def test_media_global_governance_round_trip(harness):
    headers = harness.login()
    rg = harness.seed_rg()

    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/media-global-governance",
        headers=headers,
        json={"media_global_governance": MEDIA_GOVERNANCE},
    )
    assert r.status_code == 200, r.text

    r = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/media-global-governance",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    stored = r.json()["media_global_governance"]

    assert set(stored.keys()) == set(MEDIA_MODES)
    for mode in MEDIA_MODES:
        for key, val in MEDIA_GOVERNANCE[mode].items():
            assert stored[mode][key] == val, f"governance {mode}.{key} did not round-trip"


@pytest.mark.parametrize("mode", LIMIT_MODES)
def test_each_mode_limits_isolated_round_trip(harness, mode):
    """PATCH a single mode's full key-set and confirm it round-trips on GET —
    proves every mode's own keys are carried, not just the union blob."""
    headers = harness.login()
    rg = harness.seed_rg(rg_id=f"rg-lim-{mode}")

    body = {"media_global_limits": {mode: MEDIA_LIMITS[mode]}}
    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/media-global-limits",
        headers=headers, json=body,
    )
    assert r.status_code == 200, r.text

    stored = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/media-global-limits", headers=headers,
    ).json()["media_global_limits"]
    assert set(stored.keys()) == {mode}
    for key, val in MEDIA_LIMITS[mode].items():
        assert stored[mode][key] == val, f"limits {mode}.{key} lost"


@pytest.mark.parametrize("mode", MEDIA_MODES)
def test_each_mode_governance_isolated_round_trip(harness, mode):
    headers = harness.login()
    rg = harness.seed_rg(rg_id=f"rg-gov-{mode}")

    body = {"media_global_governance": {mode: MEDIA_GOVERNANCE[mode]}}
    r = harness.client.patch(
        f"/v1/admin/resource-groups/{rg}/media-global-governance",
        headers=headers, json=body,
    )
    assert r.status_code == 200, r.text

    stored = harness.client.get(
        f"/v1/admin/resource-groups/{rg}/media-global-governance", headers=headers,
    ).json()["media_global_governance"]
    assert set(stored.keys()) == {mode}
    for key, val in MEDIA_GOVERNANCE[mode].items():
        assert stored[mode][key] == val, f"governance {mode}.{key} lost"
