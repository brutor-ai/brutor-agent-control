# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the profile-photo capability token (app/core/photo_token.py) and
its enforcement on the public photo route. The token (`?h=` = a prefix of the
photo's SHA-256 hash) stops unauthenticated enumeration of photos by integer
id and cross-tenant scraping.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.public_assets import create_public_assets_router
from app.core.photo_token import MIN_PHOTO_TOKEN_LEN, photo_token_ok
from app.database import Base, get_db
from app.database.models.end_user import EndUser
from app.database.models.tenant import Tenant

HASH = "abcdef0123456789" * 4  # 64 hex chars


# ---------------------------------------------------------------------------
# Unit: photo_token_ok
# ---------------------------------------------------------------------------


def test_token_none_or_empty_rejected():
    assert not photo_token_ok(HASH, None)
    assert not photo_token_ok(HASH, "")
    assert not photo_token_ok(None, HASH)
    assert not photo_token_ok("", HASH)


def test_token_shorter_than_minimum_rejected():
    assert MIN_PHOTO_TOKEN_LEN == 8
    assert not photo_token_ok(HASH, HASH[:7])


def test_legacy_8char_prefix_accepted():
    assert photo_token_ok(HASH, HASH[:8])


def test_full_hash_accepted():
    assert photo_token_ok(HASH, HASH)


def test_wrong_prefix_rejected():
    assert not photo_token_ok(HASH, "f" * 8)


def test_token_longer_than_hash_rejected():
    assert not photo_token_ok(HASH[:8], HASH)


# ---------------------------------------------------------------------------
# Endpoint: /v1/assets/users/end-user/{id}/photo enforces the token
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_and_hash():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # Create all tables — EndUser lazy-loads relationships (end_user_groups)
    # during attribute access; the conftest JSONB->TEXT shim makes a full
    # create_all SQLite-safe.
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    s = SessionFactory()
    s.add(Tenant(id="t1", name="t1", display_name="T1", primary_contact_email="a@t1.x"))
    user = EndUser(
        tenant_id="t1",
        username="alice",
        email="alice@t1.x",
        password_hash="x" * 60,
        photo_data=b"\xff\xd8\xff\xe0jpegbytes",
        photo_mime_type="image/jpeg",
        photo_hash=HASH,
    )
    s.add(user)
    s.commit()
    user_id = user.id
    s.close()

    def override_get_db():
        db = SessionFactory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(create_public_assets_router(), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), user_id
    app.dependency_overrides.clear()
    engine.dispose()


def test_photo_404_without_token(client_and_hash):
    client, user_id = client_and_hash
    resp = client.get(f"/v1/assets/users/end-user/{user_id}/photo")
    assert resp.status_code == 404


def test_photo_404_with_wrong_token(client_and_hash):
    client, user_id = client_and_hash
    resp = client.get(f"/v1/assets/users/end-user/{user_id}/photo?h={'f' * 8}")
    assert resp.status_code == 404


def test_photo_200_with_full_hash(client_and_hash):
    client, user_id = client_and_hash
    resp = client.get(f"/v1/assets/users/end-user/{user_id}/photo?h={HASH}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


def test_photo_200_with_legacy_8char_prefix(client_and_hash):
    client, user_id = client_and_hash
    resp = client.get(f"/v1/assets/users/end-user/{user_id}/photo?h={HASH[:8]}")
    assert resp.status_code == 200


def test_photo_404_for_nonexistent_id_even_with_token(client_and_hash):
    client, _ = client_and_hash
    resp = client.get(f"/v1/assets/users/end-user/999999/photo?h={HASH}")
    assert resp.status_code == 404
