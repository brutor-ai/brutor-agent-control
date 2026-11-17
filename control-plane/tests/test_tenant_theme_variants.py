# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression — 4-variant tenant theme (admin_light / admin_dark /
portal_light / portal_dark + shared company_name).

The bug pinned here: before migration a062_tenant_theme_variants, a tenant could only
brand the Admin Console in light mode. There was no way to customise
the Admin Console's dark mode and no per-tenant branding for the
User Portal at all. The fix:

  * `tenants.theme_variants` JSON column with all four variants.
  * Read helpers (`get_theme_variant`, `get_theme_variants`)
    layered: per-variant override → admin_light legacy flat-column
    fallback → light/dark default palette.
  * Public unauthenticated `/v1/public/tenants/{id}/theme` so the
    portal can theme its login screen before the user signs in.

The tests focus on the contract — what each layer returns, that the
flat-column fallback still works for admin_light, and that the
public endpoint refuses unknown / suspended tenants without leaking
existence info.
"""

from __future__ import annotations

import os

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

from app.constants.tenant import (
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
    THEME_VARIANTS,
    default_theme_for_variant,
)
from app.database import Base, get_db
from app.database.models.tenant import Tenant


REQUIRED_TABLES = [Tenant.__table__]


def _new_tenant(**kwargs) -> Tenant:
    """Minimal tenant row covering NOT-NULL + UNIQUE fields. `name`
    has a UNIQUE constraint, so derive it from `id` unless caller
    overrides — otherwise two test rows in the same fixture collide.
    SQLite enforces NOT NULL even without FK enforcement, so this
    helper saves four arg copies per test."""
    tenant_id = kwargs.pop("id", "t1")
    defaults = dict(
        id=tenant_id,
        name=kwargs.pop("name", tenant_id),
        display_name="Acme",
        organization_name="Acme",
        primary_contact_email="a@acme.invalid",
        primary_contact_name="admin",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
    )
    defaults.update(kwargs)
    return Tenant(**defaults)


# =============================================================================
# Pure-model behaviour (no HTTP). Pin the read-layer contract.
# =============================================================================


def test_theme_variants_constant_lists_all_four():
    """If a future change adds a 5th surface (e.g. mobile portal),
    the editor + this list need to stay in lockstep."""
    assert set(THEME_VARIANTS) == {
        "admin_light", "admin_dark", "portal_light", "portal_dark",
    }


def test_default_theme_for_variant_picks_dark_for_dark_keys():
    assert default_theme_for_variant("admin_light")["background_color"] == DEFAULT_THEME_LIGHT["background_color"]
    assert default_theme_for_variant("admin_dark")["background_color"] == DEFAULT_THEME_DARK["background_color"]
    assert default_theme_for_variant("portal_dark")["text_primary"] == DEFAULT_THEME_DARK["text_primary"]


def test_default_theme_for_variant_rejects_unknown():
    with pytest.raises(ValueError):
        default_theme_for_variant("admin_purple")


def test_unbranded_tenant_returns_default_palettes_per_variant():
    """A fresh tenant with no theme overrides reads the canonical
    defaults — light palette for light variants, dark for dark."""
    t = _new_tenant(theme_variants=None)
    al = t.get_theme_variant("admin_light")
    ad = t.get_theme_variant("admin_dark")
    assert al["background_color"] == DEFAULT_THEME_LIGHT["background_color"]
    assert ad["background_color"] == DEFAULT_THEME_DARK["background_color"]
    # company_name fallback chain ends at display_name.
    assert al["company_name"] == "Acme"


def test_admin_light_falls_back_to_legacy_flat_columns():
    """The migration backfills `admin_light` from the flat columns,
    but the read path must ALSO honour the flat columns at runtime —
    a tenant who customised the legacy single-theme schema and then
    upgraded should keep their look without operator action."""
    t = _new_tenant(
        theme_variants=None,
        theme_primary_color="#FF0000",
        theme_logo_url="https://example.com/legacy-logo.png",
    )
    al = t.get_theme_variant("admin_light")
    assert al["primary_color"] == "#FF0000"
    assert al["logo_url"] == "https://example.com/legacy-logo.png"
    # admin_dark stays default — legacy columns only seed admin_light.
    ad = t.get_theme_variant("admin_dark")
    assert ad["primary_color"] == DEFAULT_THEME_DARK["primary_color"]


def test_variant_overrides_win_over_legacy_and_defaults():
    t = _new_tenant(
        theme_primary_color="#FF0000",  # legacy
        theme_variants={
            "admin_light": {"primary_color": "#00FF00"},  # override
            "admin_dark": {"background_color": "#111111"},
        },
    )
    al = t.get_theme_variant("admin_light")
    assert al["primary_color"] == "#00FF00"
    # Other admin_light fields not overridden → legacy still wins.
    # logo_url isn't in either override → defaults to None.
    assert al["logo_url"] is None
    ad = t.get_theme_variant("admin_dark")
    assert ad["background_color"] == "#111111"
    # admin_dark.primary_color not overridden → dark default.
    assert ad["primary_color"] == DEFAULT_THEME_DARK["primary_color"]


def test_set_theme_variant_round_trip():
    t = _new_tenant()
    t.set_theme_variant("portal_dark", {"primary_color": "#ABCDEF"})
    assert t.theme_variants == {"portal_dark": {"primary_color": "#ABCDEF"}}
    # Empty payload clears the variant.
    t.set_theme_variant("portal_dark", {})
    assert t.theme_variants in (None, {})


def test_set_theme_variant_rejects_unknown_key():
    t = _new_tenant()
    with pytest.raises(ValueError):
        t.set_theme_variant("admin_purple", {"primary_color": "#000000"})


def test_get_theme_variants_returns_all_four_plus_company_name():
    t = _new_tenant(theme_company_name="Custom Co")
    bundle = t.get_theme_variants()
    assert bundle["company_name"] == "Custom Co"
    assert set(bundle["variants"].keys()) == set(THEME_VARIANTS)


# =============================================================================
# Public endpoint — what the user portal calls pre-login.
# =============================================================================


