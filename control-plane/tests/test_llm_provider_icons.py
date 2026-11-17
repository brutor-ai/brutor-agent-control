# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LlmProvider icon seeding + manager tests.

Pin the contract that:
  1. The seed pipeline stamps a bundled SVG onto every catalogue
     provider that has an icon file (or aliased file) on disk.
  2. The alias map (`bedrock` → AWS mark, `vertex_ai` → Gemini
     mark, etc.) actually resolves to the same bytes, so re-seeded
     tenants get visually consistent icons.
  3. The icon manager validates upload size + MIME and writes the
     bytes plus a SHA-256 hash for ETag/dedup.
"""
from __future__ import annotations

import hashlib
import logging
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test_brutor")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.init_llm_providers import (
    ICON_ALIAS,
    _load_seed_icon,
    _resolve_icon_path,
    create_llm_providers,
)
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_provider_catalog import LlmProviderCatalog
from app.database.models.tenant import Tenant
from app.services.llm.provider.icon_manager import LlmProviderIconManager


REQUIRED_TABLES = [
    LlmProviderCatalog.__table__,
    LlmProvider.__table__,
    Tenant.__table__,
]


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_conn, _conn_record):
        # The icon tests don't exercise FK behaviour and the
        # tenant insert would otherwise need scaffolding.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Seed coverage
# ---------------------------------------------------------------------------

def test_openai_provider_has_seeded_svg_icon(session):
    create_llm_providers(
        session,
        tenant_id="t-icons",
        logger_=logging.getLogger("test"),
    )
    row = (
        session.query(LlmProvider)
        .filter(
            LlmProvider.tenant_id == "t-icons",
            LlmProvider.key == "openai",
        )
        .first()
    )
    assert row is not None
    assert row.icon_data is not None
    assert row.icon_mime_type == "image/svg+xml"
    assert row.icon_size and row.icon_size > 0
    # Hash matches what the manager would compute on the same bytes.
    assert row.icon_hash == hashlib.sha256(row.icon_data).hexdigest()


def test_aliased_providers_share_icon_bytes(session):
    """`bedrock` is aliased to `aws` — both rows should carry the
    same icon bytes (same SHA-256). Catches regressions in the
    alias map or duplicate-icon drift."""
    create_llm_providers(
        session,
        tenant_id="t-aliases",
        logger_=logging.getLogger("test"),
    )
    bedrock = (
        session.query(LlmProvider)
        .filter(
            LlmProvider.tenant_id == "t-aliases",
            LlmProvider.key == "bedrock",
        )
        .first()
    )
    sagemaker = (
        session.query(LlmProvider)
        .filter(
            LlmProvider.tenant_id == "t-aliases",
            LlmProvider.key == "sagemaker",
        )
        .first()
    )
    assert bedrock is not None and bedrock.icon_data is not None
    assert sagemaker is not None and sagemaker.icon_data is not None
    assert bedrock.icon_hash == sagemaker.icon_hash


def test_providers_without_bundled_icon_stay_null(session):
    """Replicate + AssemblyAI ship without a bundled icon (operator
    uploads via the admin UI). They should land in the DB with
    icon_data NULL but `enabled=True`."""
    create_llm_providers(
        session,
        tenant_id="t-noicon",
        logger_=logging.getLogger("test"),
    )
    for key in ("replicate", "assemblyai"):
        row = (
            session.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == "t-noicon",
                LlmProvider.key == key,
            )
            .first()
        )
        assert row is not None, f"{key} provider seed missing"
        assert row.icon_data is None, f"{key} should not have a bundled icon"
        assert row.enabled is True


def test_alias_map_targets_exist():
    """Every alias target file must exist on disk — guards against
    a mistyped alias quietly leaving an icon missing."""
    for src, dst in ICON_ALIAS.items():
        if not dst:
            continue  # explicit "no icon"
        path = _resolve_icon_path(src)
        assert path is not None, (
            f"Icon alias {src!r} → {dst!r} resolved to no file. "
            f"Either add `app/database/provider_icons/{dst}.svg` or "
            "remove the alias entry."
        )


# ---------------------------------------------------------------------------
# Icon manager
# ---------------------------------------------------------------------------

def _make_provider_row(session, key: str = "openai") -> LlmProvider:
    row = LlmProvider(
        id=f"prov-{key}",
        tenant_id="t-mgr",
        key=key,
        display_name=key.title(),
        enabled=True,
        is_system=False,
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.asyncio
async def test_save_icon_from_bytes_writes_columns(session):
    row = _make_provider_row(session)
    mgr = LlmProviderIconManager()
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    info = await mgr.save_icon_from_bytes(row.id, svg, "image/svg+xml", session)
    assert info["mime_type"] == "image/svg+xml"
    assert info["size"] == len(svg)
    assert info["hash"] == hashlib.sha256(svg).hexdigest()

    refreshed = session.query(LlmProvider).get(row.id)
    assert refreshed.icon_data == svg
    assert refreshed.icon_mime_type == "image/svg+xml"
    assert refreshed.icon_uploaded_at is not None


@pytest.mark.asyncio
async def test_save_icon_strips_charset_from_mime(session):
    row = _make_provider_row(session)
    mgr = LlmProviderIconManager()
    svg = b"<svg></svg>"
    info = await mgr.save_icon_from_bytes(
        row.id, svg, "image/svg+xml; charset=utf-8", session
    )
    # Charset suffix stripped — DB stores the canonical mime.
    assert info["mime_type"] == "image/svg+xml"


@pytest.mark.asyncio
async def test_save_icon_rejects_unsupported_mime(session):
    row = _make_provider_row(session)
    mgr = LlmProviderIconManager()
    with pytest.raises(ValueError, match="unsupported mime"):
        await mgr.save_icon_from_bytes(
            row.id, b"<svg></svg>", "application/x-zip", session
        )


@pytest.mark.asyncio
async def test_save_icon_rejects_oversized_blob(session):
    row = _make_provider_row(session)
    mgr = LlmProviderIconManager()
    too_big = b"x" * (mgr.max_file_size + 1)
    with pytest.raises(ValueError, match="exceeds"):
        await mgr.save_icon_from_bytes(
            row.id, too_big, "image/png", session
        )


def test_delete_icon_clears_columns(session):
    row = _make_provider_row(session)
    row.icon_data = b"<svg></svg>"
    row.icon_mime_type = "image/svg+xml"
    row.icon_size = 11
    row.icon_hash = "deadbeef"
    session.flush()

    mgr = LlmProviderIconManager()
    assert mgr.delete_icon(row.id, session) is True

    refreshed = session.query(LlmProvider).get(row.id)
    assert refreshed.icon_data is None
    assert refreshed.icon_mime_type is None
    assert refreshed.icon_size is None
    assert refreshed.icon_hash is None
    assert refreshed.icon_uploaded_at is None


def test_delete_icon_is_idempotent(session):
    row = _make_provider_row(session)
    mgr = LlmProviderIconManager()
    # Already null — returns False, doesn't raise.
    assert mgr.delete_icon(row.id, session) is False
