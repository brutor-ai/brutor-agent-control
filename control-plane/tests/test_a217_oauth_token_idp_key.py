# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a217_oauth_token_idp_key — a stored OAuth credential is keyed with its identity provider
(the control plane's half; the gateway's upsert names the same columns).

`oauth_tokens` was unique on `(tenant_id, idp_user_id, mcp_server_id)`. A
subject is unique within its identity provider only: two providers of one
tenant that both name a subject `42` met on one row. The gateway refuses the
second provider's exchange with 409 until the key includes the provider;
this is the key.

Runs on the suite's SQLite; with BRUTOR_PG_TESTS=1 on Postgres, where the
revision itself is run both ways.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.database.models.oauth_token import OAuthToken
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

NEW = "uq_oauth_tokens_tenant_idp_user_server"
NEW_COLUMNS = ["tenant_id", "idp_id", "idp_user_id", "mcp_server_id"]
OLD_INDEX = "ix_oauth_tokens_tenant_user_server"


def test_the_model_is_keyed_with_the_identity_provider():
    table = OAuthToken.__table__
    uniques = {c.name: [col.name for col in c.columns] for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {NEW: NEW_COLUMNS}
    # The plain index on the old key went with it: the unique key's own index
    # serves the keyed lookup, `ix_oauth_tokens_tenant_idp_user` a subject's
    # credentials and `ix_oauth_tokens_tenant_server` a server's.
    names = {index.name for index in table.indexes}
    assert OLD_INDEX not in names
    assert {"ix_oauth_tokens_tenant_idp_user", "ix_oauth_tokens_tenant_server", "ix_oauth_tokens_tenant_idp"} <= names


@pytest.fixture()
def h():
    for url in throwaway_pg_database("oauth_token_idp_key"):
        harness = build_admin_harness([], logger_name="oauth-token-idp-key", database_url=url)
        yield harness
        harness.close()


def _token(idp: str, subject: str = "42", server: str = "srv-1", tenant: str = "t1") -> OAuthToken:
    return OAuthToken(
        tenant_id=tenant, idp_id=idp, idp_user_id=subject, mcp_server_id=server,
        encrypted_access_token=f"ciphertext-{idp}", token_type="Bearer",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


def _held(h) -> list:
    with h.Factory() as s:
        return sorted((t.tenant_id, t.idp_id, t.idp_user_id, t.mcp_server_id) for t in s.query(OAuthToken))


def test_two_identity_providers_naming_the_same_subject_each_hold_a_credential(h):
    h.seed(lambda s: s.add(_token("idp-a")))
    # Was: IntegrityError on uq_oauth_tokens_tenant_user_server.
    h.seed(lambda s: s.add(_token("idp-b")))
    h.seed(lambda s: s.add(_token("idp-a", tenant="t2")))
    assert _held(h) == [
        ("t1", "idp-a", "42", "srv-1"), ("t1", "idp-b", "42", "srv-1"), ("t2", "idp-a", "42", "srv-1"),
    ]


def test_one_identity_providers_subject_holds_one_credential_per_server(h):
    h.seed(lambda s: s.add(_token("idp-a")))
    with pytest.raises(IntegrityError):
        h.seed(lambda s: s.add(_token("idp-a")))
    h.seed(lambda s: s.add(_token("idp-a", server="srv-2")))
    assert len(_held(h)) == 2


# --- the revision, on Postgres ------------------------------------------------
