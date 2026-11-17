# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for get_or_create_default_tenant (seed_initial_data).

The seeding code used to key on total tenant count and then grab
`db.query(Tenant).first()` — an unordered, random row. On a multi-tenant
install that could select an operator's *customer* tenant and re-provision it
on every boot. The selection must always be keyed on `is_default`, and a
missing default must be CREATED rather than substituted by a customer tenant.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_or_create_default_tenant
from app.database.models.tenant import Tenant


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[Tenant.__table__])
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_customer_tenant(db: Session, tid: str = "acme") -> Tenant:
    t = Tenant(
        id=tid,
        name=tid,
        display_name="Acme Corp",
        primary_contact_email=f"admin@{tid}.example",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
    )
    db.add(t)
    db.commit()
    return t


def test_creates_default_when_db_empty(db: Session):
    result = get_or_create_default_tenant(db)
    assert result.is_default is True
    assert result.id == "default"
    assert db.query(Tenant).filter(Tenant.is_default == True).count() == 1  # noqa: E712


def test_does_not_hijack_customer_tenant_when_no_default(db: Session):
    # Only a customer (non-default) tenant exists — the bug would have
    # returned and re-provisioned THIS tenant.
    customer = _add_customer_tenant(db, "acme")

    result = get_or_create_default_tenant(db)

    # A fresh default tenant is created; the customer tenant is NOT selected.
    assert result.id != customer.id
    assert result.is_default is True
    assert result.id == "default"

    # The customer tenant is untouched and still non-default.
    refreshed = db.query(Tenant).filter(Tenant.id == "acme").one()
    assert refreshed.is_default is False
    assert db.query(Tenant).count() == 2


def test_returns_existing_default_alongside_customers(db: Session):
    _add_customer_tenant(db, "acme")
    existing_default = get_or_create_default_tenant(db)  # creates 'default'

    # A second call must return the SAME default, not create another and not
    # pick the customer tenant.
    again = get_or_create_default_tenant(db)
    assert again.id == existing_default.id == "default"
    assert again.is_default is True
    assert db.query(Tenant).filter(Tenant.is_default == True).count() == 1  # noqa: E712
    assert db.query(Tenant).count() == 2
