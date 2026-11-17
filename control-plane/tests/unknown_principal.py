# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A third principal, for asking what a route does with a caller that is neither
a tenant administrator nor the system administrator.

`robot` is registered, signs in with a token of its own, holds every
permission and belongs to no tenant; it is not platform-wide and its
`resolve_tenant` refuses every request (403). A route that decides its tenant
by asking the principal therefore refuses it. A route that lets it reach a
tenant's data compared `admin_type` with "tenant" and took everything else
for the system administrator: the fall-through the registry exists to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
from fastapi import HTTPException

from app.composition.principals import HUMAN_SESSION, PRINCIPALS, PrincipalSpec

ROBOT = "robot"


def robot_admin():
    return SimpleNamespace(
        id="robot-1", username="robot", email="robot@example.com", full_name="Robot",
        tenant_id=None, role_id=None, role=None, permissions=[], is_active=True, is_locked=False,
        has_permission=lambda _p: True,
        to_dict=lambda: {"id": "robot-1", "username": "robot", "admin_type": ROBOT},
    )


def _verify(service, payload, db):
    return robot_admin(), None


def _resolve(admin, tenant_id_query):
    raise HTTPException(status_code=403, detail="robot acts on no tenant")


def robot_spec() -> PrincipalSpec:
    return PrincipalSpec(
        name=ROBOT, credential_class=HUMAN_SESSION, verify=_verify,
        resolve_tenant=_resolve, find_session=lambda db, session_id: None,
    )


def robot_headers(secret: str):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"typ": "admin", "admin_type": ROBOT, "user_id": "robot-1", "username": "robot",
         "jti": "robot-session", "iat": now, "exp": now + timedelta(hours=1)},
        secret, algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


class robot_registered:
    """`robot` in the process-wide registry for a `with` block."""

    def __enter__(self):
        self.saved = dict(PRINCIPALS._specs)
        PRINCIPALS.register([robot_spec()])
        return self

    def __exit__(self, *exc):
        PRINCIPALS._specs = self.saved
        return False
