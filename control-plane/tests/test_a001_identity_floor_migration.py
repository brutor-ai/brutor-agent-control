# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The identity floor's schema: the revision after the baseline and the models.

The policy columns (a group's explicit identity policy, the tenant's
identity default), the credential facts the gateway records on every call
and every run, the CHECKs that keep their vocabularies closed, the index the
lifecycle gate and the identity summary read through, and the one OIDC IdP
per issuer and tenant rule.

`tests/test_alembic_head_is_the_models_schema.py` proves on Postgres that the
revisions and the models build the same schema; this file pins what that
schema must hold, so a change to both sides at once is still caught.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from sqlalchemy import Boolean, CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database.models.ai_system_run import AISystemRun
from app.database.models.idp_server import IdPServer
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

LEVELS = ("key", "bound_key", "workload", "workload_pop")
KINDS = ("api_key", "workload_token", "x509_svid", "brutor_token", "human_session", "platform_service", "anonymous")
OUTCOMES = ("met", "below_policy", "refused", "exempt")

# table -> column -> (type, length or None)
NEW_COLUMNS = {
    ResourceGroup: {"identity_policy": (JSONB, None)},
    Tenant: {"identity_policy_default": (JSONB, None)},
    ProxyLog: {
        "credential_kind": (String, 16),
        "credential_assurance": (String, 16),
        "credential_ref": (String, 512),
        "credential_policy_outcome": (String, 16),
    },
    AISystemRun: {
        "credential_assurance_min": (String, 16),
        "credential_mixed": (Boolean, None),
        "credential_refs": (JSONB, None),
    },
}


def _revision_path() -> Path:
    found = sorted(VERSIONS.glob("*_0000_a001_identity_floor.py"))
    assert len(found) == 1, f"expected one a001_identity_floor revision, found {found}"
    return found[0]


def _revision():
    path = _revision_path()
    spec = importlib.util.spec_from_file_location("a001_identity_floor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sections() -> tuple[str, str]:
    text = _revision_path().read_text()
    up, down = text.split("def downgrade", 1)
    return up, down


def _check(model, name: str) -> str:
    found = [c for c in model.__table__.constraints if isinstance(c, CheckConstraint) and c.name == name]
    assert len(found) == 1, f"{model.__tablename__} has no CHECK {name}"
    return str(found[0].sqltext)


def _index(model, name: str) -> Index:
    found = [i for i in model.__table__.indexes if i.name == name]
    assert len(found) == 1, f"{model.__tablename__} has no index {name}"
    return found[0]


def _quoted(sql: str) -> set[str]:
    return set(re.findall(r"'([a-z0-9_]+)'", sql))


class TestRevision:
    def test_it_follows_the_baseline(self):
        rev = _revision()
        assert rev.revision == "a001_identity_floor"
        assert rev.down_revision == "a000_baseline"
        assert len(rev.revision) <= 32

    def test_it_migrates_no_data(self):
        up, _ = _sections()
        for verb in ("UPDATE ", "INSERT ", "DELETE "):
            assert verb not in up.upper().replace("ON DELETE", ""), verb

    def test_every_new_column_is_added_and_dropped(self):
        up, down = _sections()
        for model, columns in NEW_COLUMNS.items():
            table = model.__tablename__
            for name in columns:
                assert re.search(rf"add_column\(\s*['\"]{table}['\"].*?['\"]{name}['\"]", up, re.S), (table, name)
                assert re.search(rf"drop_column\(\s*['\"]{table}['\"],\s*['\"]{name}['\"]", down), (table, name)

    def test_every_new_constraint_and_index_is_created_and_dropped(self):
        up, down = _sections()
        for name in (
            "ix_proxy_logs_credential",
            "ck_proxy_logs_credential_kind",
            "ck_proxy_logs_credential_assurance",
            "ck_proxy_logs_credential_outcome",
            "ck_ai_system_runs_credential_assurance_min",
            "uq_idp_servers_tenant_oidc_issuer",
        ):
            assert name in up, name
            assert name in down, name


class TestModels:
    def test_the_new_columns_are_nullable_without_a_default_and_commented(self):
        for model, columns in NEW_COLUMNS.items():
            for name, (kind, length) in columns.items():
                column = model.__table__.c[name]
                assert isinstance(column.type, kind), (model.__tablename__, name, column.type)
                if length is not None:
                    assert column.type.length == length, (model.__tablename__, name)
                assert column.nullable, (model.__tablename__, name)
                assert column.server_default is None and column.default is None, (model.__tablename__, name)
                assert column.comment, (model.__tablename__, name)

    def test_the_kind_check_admits_exactly_the_kinds(self):
        sql = _check(ProxyLog, "ck_proxy_logs_credential_kind")
        assert "credential_kind IS NULL" in sql
        assert _quoted(sql) == set(KINDS)

    def test_the_level_checks_admit_exactly_the_ladder(self):
        for model, name, column in (
            (ProxyLog, "ck_proxy_logs_credential_assurance", "credential_assurance"),
            (AISystemRun, "ck_ai_system_runs_credential_assurance_min", "credential_assurance_min"),
        ):
            sql = _check(model, name)
            assert f"{column} IS NULL" in sql
            assert _quoted(sql) == set(LEVELS), name

    def test_the_outcome_check_admits_exactly_the_outcomes(self):
        sql = _check(ProxyLog, "ck_proxy_logs_credential_outcome")
        assert "credential_policy_outcome IS NULL" in sql
        assert _quoted(sql) == set(OUTCOMES)

    def test_the_credential_index_serves_the_gate_and_the_summary(self):
        index = _index(ProxyLog, "ix_proxy_logs_credential")
        assert [c.name for c in index.columns] == [
            "tenant_id",
            "resource_group_id",
            "credential_assurance",
            "timestamp",
        ]

    def test_one_oidc_idp_per_issuer_and_tenant(self):
        index = _index(IdPServer, "uq_idp_servers_tenant_oidc_issuer")
        assert index.unique
        assert [c.name for c in index.columns] == ["tenant_id", "issuer"]
        where = str(index.dialect_options["postgresql"]["where"])
        assert where.replace(" ", "") == "protocol='oidc'"
