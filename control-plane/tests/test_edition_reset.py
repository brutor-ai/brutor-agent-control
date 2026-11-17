# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The edition guard, the reset half: the operator command that deletes the
Enterprise data, so that the Community Edition starts.

* It is a dry run until it is given the confirmation flag: it prints what it
  would delete (kinds and counts) and changes nothing.
* With the flag, in one transaction: every row of the Enterprise-only tables,
  every tenant but the one kept (with everything it owns), the Enterprise
  feature flags (switched off) and the edition marker. It records
  the reset in the kept tenant's admin audit log, and declares the removed
  tenants' rows of the audit hash chain, so the kept tenant's evidence still
  verifies. The Community data of the kept tenant is not touched.
* What the edition rule leaves open is refused, not guessed: which tenant survives when
  there are several.
* Nothing but the command line reaches it: no route, no start-up step, no
  environment variable.

Every database here is created by the test run (SQLite file, or a throwaway
Postgres database with `BRUTOR_PG_TESTS=1`).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from app.composition.edition import ENTERPRISE_TENANT_FEATURE_FLAGS, enterprise_only_tables
from app.composition.features import MULTI_TENANT
from app.constants.tenant import TENANT_FEATURE_KEYS
from app.services.edition import cli as reset_cli
from app.services.edition import guard, reset
from app.services.edition.guard import enforce_edition
from app.services.edition.reset import reset_enterprise_data
from tests.edition_compositions import (
    community_composition,
    count,
    empty_every_table,
    insert_row,
    insert_tenant,
    pg_enabled,
    scratch_database,
)
from tests.source_trees import python_sources

REPO = Path(__file__).resolve().parents[1]
ALL_TABLES = [table for table, _ in enterprise_only_tables()]
ALL_ON = {key: True for key in TENANT_FEATURE_KEYS}

# Tables of the Community Edition the kept tenant's rows must survive in.
COMMUNITY_TABLES = (
    "tenant_roles",
    "llm_model",
    "mcp_servers",
    "resource_groups",
    "api_keys",
    "end_users",
    "proxy_logs",
    "evidence_logs",
)

DIALECTS = [
    "sqlite",
    pytest.param(
        "postgresql",
        marks=pytest.mark.skipif(not pg_enabled(), reason="needs Postgres; set BRUTOR_PG_TESTS=1"),
    ),
]


@pytest.fixture(scope="module", params=DIALECTS)
def engine(request, tmp_path_factory):
    with scratch_database(request.param, tmp_path_factory) as scratch:
        yield scratch


def _community_rows(conn, tenant_id: str) -> None:
    insert_row(conn, "tenant_roles", id=f"role-{tenant_id}", tenant_id=tenant_id)
    insert_row(
        conn, "tenant_admins", id=f"adm-password-{tenant_id}", tenant_id=tenant_id, role_id=f"role-{tenant_id}"
    )
    insert_row(
        conn,
        "tenant_admin_audit_logs",
        tenant_id=tenant_id,
        admin_id=f"adm-password-{tenant_id}",
        action="login",
        success=True,
    )
    for table in ("llm_model", "mcp_servers", "resource_groups", "api_keys", "end_users", "evidence_logs"):
        insert_row(conn, table, tenant_id=tenant_id)
    insert_row(conn, "idp_servers", id=f"idp-oidc-{tenant_id}", tenant_id=tenant_id, protocol="oidc")


# One writer chain, the two tenants' rows interleaved as a busy gateway
# writes them: default at 1, 4, 6; acme at 2, 3, 5.
CHAIN = [(1, "default"), (2, "acme"), (3, "acme"), (4, "default"), (5, "acme"), (6, "default")]


def _used_by_enterprise(engine, second_tenant: bool = True) -> None:
    """A database as the Enterprise Edition leaves it."""
    empty_every_table(engine)
    with engine.begin() as conn:
        insert_tenant(conn, "default", features=dict(ALL_ON))
        _community_rows(conn, "default")
        for table in ALL_TABLES:
            insert_row(conn, table)
        insert_row(conn, "policy_bundles")
        if second_tenant:
            insert_tenant(conn, "acme", features=dict(ALL_ON))
            _community_rows(conn, "acme")
            insert_row(conn, "idp_servers", id="idp-saml-acme", tenant_id="acme", protocol="saml")
            insert_row(conn, "policy_bundles", tenant_id="acme")
            insert_row(
                conn,
                "audit_chain_prunes",
                id="prune-earlier",
                tenant_id="acme",
                chain_id="chain-0",
                seq_min=1,
                seq_max=2,
                row_count=2,
                runs=[{"from": 1, "to": 2, "tail_hash": "aa"}],
                reason="retention",
            )
        for seq, tenant_id in CHAIN:
            if tenant_id == "acme" and not second_tenant:
                continue
            insert_row(
                conn,
                "proxy_logs",
                tenant_id=tenant_id,
                chain_id="chain-1",
                chain_seq=seq,
                row_hash=f"hash-{seq}",
            )
        enforce_edition(conn, frozenset({MULTI_TENANT}), "1.0.0")


@pytest.fixture
def used(engine):
    _used_by_enterprise(engine)
    yield engine
    empty_every_table(engine)


@pytest.fixture
def used_single_tenant(engine):
    _used_by_enterprise(engine, second_tenant=False)
    yield engine
    empty_every_table(engine)


def _snapshot(engine) -> dict:
    """Every table's row count, and per tenant where the table has one."""
    from app.database import Base

    out = {}
    with engine.connect() as conn:
        for table in Base.metadata.tables.values():
            out[table.name] = count(conn, table.name)
            if "tenant_id" in table.columns:
                for tenant_id in ("default", "acme"):
                    out[f"{table.name}/{tenant_id}"] = count(conn, table.name, f"tenant_id = '{tenant_id}'")
        out["features"] = sorted(
            (row.id, str(sorted((row.features or {}).items())))
            for row in conn.execute(Base.metadata.tables["tenants"].select())
        )
    return out


def _community_start(engine) -> str:
    with community_composition(), engine.begin() as conn:
        return enforce_edition(conn, frozenset(), "1.0.0")


def _run_cli(engine, *args, env=None):
    with mock.patch.object(reset_cli, "_engine", lambda: engine):
        return CliRunner().invoke(reset_cli.edition_reset, list(args), env=env or {})


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestTheDryRun:
    def test_the_database_is_refused_by_community_to_begin_with(self, used):
        with pytest.raises(guard.EditionRefusal):
            _community_start(used)

    def test_without_confirmation_nothing_changes(self, used):
        before = _snapshot(used)
        outcome = reset_enterprise_data(used, keep_tenant="default")
        assert outcome.executed is False
        assert outcome.audit_id is None
        assert _snapshot(used) == before

    def test_confirm_has_to_be_given_by_name_and_is_off_by_default(self):
        parameters = inspect.signature(reset_enterprise_data).parameters
        assert parameters["confirm"].default is False
        assert parameters["confirm"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_plan_counts_exactly_what_would_be_deleted(self, used):
        plan = reset_enterprise_data(used, keep_tenant="default").plan
        assert plan.blockers == []
        assert plan.nothing_to_do is False
        assert plan.marker is not None
        expected = {table: 1 for table in ALL_TABLES}
        expected["policy_bundles"] = 3
        assert plan.tables == expected
        assert plan.keep_tenant == "default"
        assert plan.remove_tenants == ["acme"]
        acme = plan.removed_tenant_rows["acme"]
        assert acme["proxy_logs"] == 3
        assert acme["tenant_admins"] == 1
        assert acme["llm_model"] == 1
        assert "policy_bundles" not in acme  # counted once, with the Enterprise tables
        assert plan.feature_flags == {flag: 1 for flag in ENTERPRISE_TENANT_FEATURE_FLAGS}

    def test_the_command_without_the_flag_prints_the_plan_and_changes_nothing(self, used):
        before = _snapshot(used)
        result = _run_cli(used, "--keep-tenant", "default")
        assert result.exit_code == 0, result.output
        assert _snapshot(used) == before
        out = result.output
        assert "DRY RUN" in out
        assert "Nothing was changed" in out
        assert "pg_dump" in out
        assert "--confirm-delete-enterprise-data" in out
        assert re.search(r"policy_bundles\s+\(policy_as_code\):\s+3 rows", out)
        assert re.search(r"system_admins\s+\(multi_tenant\):\s+1 row", out)
        assert "tenant acme" in out
        assert "SAML" not in out
        assert "knowledge_base" in out
        assert "edition marker" in out
        assert used.url.database.split("/")[-1] in out
        if used.url.password:
            assert used.url.password not in out

    def test_the_dry_run_prints_no_row_contents(self, engine):
        _used_by_enterprise(engine)
        secret = "s3cr3t-value-that-must-not-be-printed"
        with engine.begin() as conn:
            insert_row(conn, "idp_servers", protocol="oidc", name=secret)
        assert secret not in _run_cli(engine, "--keep-tenant", "default").output
        empty_every_table(engine)

    @pytest.mark.parametrize(
        "env",
        [
            {"CONFIRM_DELETE_ENTERPRISE_DATA": "1"},
            {"EDITION_RESET_CONFIRM_DELETE_ENTERPRISE_DATA": "true"},
            {"BRUTOR_EDITION_RESET_CONFIRM": "1"},
            {"BRUTOR_CONFIRM_DELETE_ENTERPRISE_DATA": "yes"},
        ],
    )
    def test_an_environment_variable_alone_never_acts(self, used, env):
        before = _snapshot(used)
        result = _run_cli(used, "--keep-tenant", "default", env=env)
        assert "DRY RUN" in result.output
        assert _snapshot(used) == before

    def test_the_command_never_asks(self, used):
        """Never a prompt: with no flag and input waiting, it is a dry run."""
        before = _snapshot(used)
        with mock.patch.object(reset_cli, "_engine", lambda: used):
            result = CliRunner().invoke(reset_cli.edition_reset, ["--keep-tenant", "default"], input="y\nyes\n")
        assert "DRY RUN" in result.output
        assert _snapshot(used) == before
        source = inspect.getsource(reset_cli)
        assert "prompt" not in source and "confirm(" not in source and "input(" not in source


# ---------------------------------------------------------------------------
# What the edition rule leaves open is refused
# ---------------------------------------------------------------------------


class TestWhatIsNotDecidedIsRefused:
    def test_several_tenants_and_none_chosen(self, used):
        before = _snapshot(used)
        outcome = reset_enterprise_data(used, confirm=True)
        assert outcome.executed is False
        assert len(outcome.plan.blockers) == 1
        assert "--keep-tenant" in outcome.plan.blockers[0]
        assert _snapshot(used) == before

    def test_the_command_says_so_and_exits_with_an_error(self, used):
        before = _snapshot(used)
        result = _run_cli(used, "--confirm-delete-enterprise-data")
        assert result.exit_code == 2
        assert "REFUSED" in result.output
        assert "--keep-tenant" in result.output
        assert "default" in result.output and "acme" in result.output
        assert _snapshot(used) == before

    def test_a_tenant_that_does_not_exist(self, used):
        before = _snapshot(used)
        outcome = reset_enterprise_data(used, confirm=True, keep_tenant="nobody")
        assert outcome.executed is False
        assert "nobody" in outcome.plan.blockers[0]
        assert _snapshot(used) == before

    def test_a_tenant_that_is_not_the_default_tenant(self, used):
        """The Community Edition runs on the default tenant. Turning another
        tenant into it is not something the reset decides."""
        before = _snapshot(used)
        outcome = reset_enterprise_data(used, confirm=True, keep_tenant="acme")
        assert outcome.executed is False
        assert "default tenant" in outcome.plan.blockers[0]
        assert _snapshot(used) == before

    def test_one_tenant_needs_no_choice(self, used_single_tenant):
        outcome = reset_enterprise_data(used_single_tenant, confirm=True)
        assert outcome.plan.blockers == []
        assert outcome.plan.keep_tenant == "default"
        assert outcome.executed is True


# ---------------------------------------------------------------------------
# The reset
# ---------------------------------------------------------------------------


class TestTheReset:
    def test_every_kind_is_gone_and_community_starts(self, used):
        outcome = reset_enterprise_data(used, confirm=True, keep_tenant="default")
        assert outcome.executed is True
        with used.connect() as conn:
            assert guard.read_marker(conn) is None
            findings, undecided = guard.look_for_enterprise_data(conn)
            assert findings == [] and undecided == []
            for table in ALL_TABLES:
                assert count(conn, table) == 0, table
            assert count(conn, "tenants") == 1
            assert count(conn, "edition_marker") == 0
        assert _community_start(used) == "community"

    def test_the_kept_tenants_community_data_is_intact(self, used):
        before = _snapshot(used)
        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        after = _snapshot(used)
        for table in COMMUNITY_TABLES:
            assert after[f"{table}/default"] == before[f"{table}/default"] > 0, table
        with used.connect() as conn:
            assert [r[0] for r in conn.execute(text("SELECT id FROM tenant_admins ORDER BY id"))] == [
                "adm-password-default"
            ]
            assert [r[0] for r in conn.execute(text("SELECT id FROM idp_servers ORDER BY id"))] == [
                "idp-oidc-default"
            ]
            assert [r[0] for r in conn.execute(text("SELECT id FROM tenants"))] == ["default"]

    def test_nothing_outside_the_plan_is_deleted(self, used):
        """Table by table: a table loses rows only if the plan names it."""
        from app.database import Base

        before = _snapshot(used)
        plan = reset_enterprise_data(used, confirm=True, keep_tenant="default").plan
        after = _snapshot(used)
        for table in Base.metadata.tables.values():
            name = table.name
            if name in ALL_TABLES or name in ("edition_marker", "tenants"):
                continue
            lost = before[name] - after[name]
            if name == "tenant_admin_audit_logs":
                lost += 1  # the record of the reset
            if name == "audit_chain_prunes":
                lost += 1  # the declaration
            expected = plan.removed_tenant_rows["acme"].get(name, 0)
            assert lost == expected, name

    def test_the_other_tenant_is_gone_with_everything_it_owned(self, used):
        from app.database import Base

        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        after = _snapshot(used)
        left = {
            name: after[f"{name}/acme"]
            for name, table in Base.metadata.tables.items()
            if "tenant_id" in table.columns and after[f"{name}/acme"]
        }
        # Declarations about the shared audit chain are kept: see the chain test.
        assert left == {"audit_chain_prunes": 2}

    def test_the_enterprise_feature_flags_are_switched_off_and_the_others_kept(self, used):
        from app.database import Base

        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        with used.connect() as conn:
            features = conn.execute(Base.metadata.tables["tenants"].select()).one().features
        assert {k for k, on in features.items() if not on} == set(ENTERPRISE_TENANT_FEATURE_FLAGS)
        assert set(features) == set(TENANT_FEATURE_KEYS)

    def test_it_is_recorded_in_the_admin_audit_log(self, used):
        from app.database import Base

        outcome = reset_enterprise_data(used, confirm=True, keep_tenant="default", operator="ops@host")
        logs = Base.metadata.tables["tenant_admin_audit_logs"]
        with used.connect() as conn:
            rows = conn.execute(logs.select().where(logs.c.action == "edition_reset")).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == outcome.audit_id
        assert row.tenant_id == "default"
        assert row.admin_id is None
        assert row.success is True or row.success == 1
        assert row.resource_type == "edition"
        details = row.details
        assert details["reset"] == (
            "the Enterprise Edition's data was deleted so that the Community Edition can start"
        )
        assert details["operator"] == "ops@host"
        assert details["removed_tenants"] == ["acme"]
        assert details["enterprise_tables"]["policy_bundles"] == 3
        assert details["enterprise_tables"]["system_admin_audit_logs"] == 1
        assert "saml_identity_providers" not in details and "saml_administrators" not in details
        assert details["feature_flags_switched_off"] == sorted(ENTERPRISE_TENANT_FEATURE_FLAGS)
        assert details["marker"]["enterprise_first_version"] == "1.0.0"
        assert details["removed_tenant_rows"]["acme"]["proxy_logs"] == 3

    def test_the_earlier_audit_log_of_the_kept_tenant_is_kept(self, used):
        before = _snapshot(used)["tenant_admin_audit_logs/default"]
        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        assert _snapshot(used)["tenant_admin_audit_logs/default"] == before + 1

    def test_the_audit_hash_chain_still_accounts_for_every_row(self, used):
        """The removed tenant's rows sat between the kept tenant's in one
        writer chain. They are declared (which positions, and the hash each
        survivor links to), so the verifier answers `pruned`, not `gap`."""
        from app.database import Base

        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        prunes = Base.metadata.tables["audit_chain_prunes"]
        with used.connect() as conn:
            kept = conn.execute(
                text("SELECT chain_seq, row_hash FROM proxy_logs WHERE chain_id = 'chain-1' ORDER BY chain_seq")
            ).all()
            declared = conn.execute(prunes.select().where(prunes.c.chain_id == "chain-1")).all()
            earlier = conn.execute(prunes.select().where(prunes.c.id == "prune-earlier")).all()
        assert [(r.chain_seq, r.row_hash) for r in kept] == [(1, "hash-1"), (4, "hash-4"), (6, "hash-6")]
        assert len(declared) == 1
        record = declared[0]
        assert record.tenant_id == "acme"
        assert record.reason == "manual"
        assert record.actor == "edition-reset"
        assert (record.seq_min, record.seq_max, record.row_count) == (2, 5, 3)
        assert record.runs == [
            {"from": 2, "to": 3, "tail_hash": "hash-3"},
            {"from": 5, "to": 5, "tail_hash": "hash-5"},
        ]
        # Every missing position of the chain is a declared one.
        present = {r.chain_seq for r in kept}
        covered = {seq for run in record.runs for seq in range(run["from"], run["to"] + 1)}
        assert present | covered == set(range(1, 7)) and not present & covered
        # A declaration made before the reset is evidence too.
        assert len(earlier) == 1

    def test_the_command_with_the_flag_resets(self, used):
        result = _run_cli(used, "--keep-tenant", "default", "--confirm-delete-enterprise-data")
        assert result.exit_code == 0, result.output
        assert "DRY RUN" not in result.output
        assert "RESET DONE" in result.output
        assert "pg_dump" in result.output
        assert _community_start(used) == "community"

    def test_twice_is_harmless(self, used):
        reset_enterprise_data(used, confirm=True, keep_tenant="default")
        after_first = _snapshot(used)
        second = reset_enterprise_data(used, confirm=True, keep_tenant="default")
        assert second.plan.nothing_to_do is True
        assert second.executed is False
        assert second.audit_id is None
        assert _snapshot(used) == after_first
        result = _run_cli(used, "--confirm-delete-enterprise-data")
        assert result.exit_code == 0
        assert "Nothing to reset" in result.output
        assert _snapshot(used) == after_first

    def test_a_community_database_has_nothing_to_reset(self, engine):
        empty_every_table(engine)
        with engine.begin() as conn:
            insert_tenant(conn)
            _community_rows(conn, "default")
        before = _snapshot(engine)
        outcome = reset_enterprise_data(engine, confirm=True)
        assert outcome.plan.nothing_to_do is True and outcome.executed is False
        assert _snapshot(engine) == before
        empty_every_table(engine)

    @pytest.mark.parametrize(
        "step", ["_delete_marker", "_record_in_audit_log", "_switch_feature_flags_off"]
    )
    def test_a_failure_part_way_rolls_everything_back(self, used, step):
        before = _snapshot(used)
        with mock.patch.object(reset, step, side_effect=RuntimeError("part way")):
            with pytest.raises(RuntimeError, match="part way"):
                reset_enterprise_data(used, confirm=True, keep_tenant="default")
        assert _snapshot(used) == before
        with pytest.raises(guard.EditionRefusal):
            _community_start(used)

    def test_the_command_reports_a_failure_and_exits_with_an_error(self, used):
        before = _snapshot(used)
        with mock.patch.object(reset, "_delete_marker", side_effect=RuntimeError("part way")):
            result = _run_cli(used, "--keep-tenant", "default", "--confirm-delete-enterprise-data")
        assert result.exit_code == 1
        assert "Nothing was changed" in result.output
        assert _snapshot(used) == before


# ---------------------------------------------------------------------------
# Nothing but the command line reaches it
# ---------------------------------------------------------------------------


def _app_sources():
    for path in python_sources((REPO / "app")):
        yield path.relative_to(REPO).as_posix(), path.read_text()


class TestOnlyTheCommandLineReachesIt:
    def test_only_the_command_imports_the_reset(self):
        users = [
            path
            for path, source in _app_sources()
            if re.search(r"edition\.reset\b|edition import reset|reset_enterprise_data", source)
            and path != "app/services/edition/reset.py"
        ]
        assert users == ["app/services/edition/cli.py"]

    def test_only_main_imports_the_command(self):
        users = [
            path
            for path, source in _app_sources()
            if re.search(r"edition\.cli\b|edition import cli|edition_reset\b", source)
            and path not in ("app/services/edition/cli.py", "app/services/edition/reset.py")
        ]
        # The command line (`app/cli.py`, which `main.py` and the console
        # script call; the Enterprise entry points hand over to it).
        assert users == ["app/cli.py"]
        main = (REPO / "app" / "cli.py").read_text()
        assert "app.services.edition.cli" in main
        assert "dispatch" in (REPO / "main.py").read_text()
        # Reached by its name on the command line, and by nothing else.
        assert re.search(r"sys\.argv\[1\] == ['\"]edition-reset['\"]", main)

    def test_no_route_is_about_the_edition(self):
        from app.composition.routers import ROUTERS

        assert [spec.name for spec in ROUTERS if "edition" in spec.name or "reset" in spec.name] == []
        import json

        # The Enterprise document: `enterprise/tests/test_enterprise_openapi_document.py`.
        paths = json.loads((REPO / "tests" / "fixtures" / "openapi_snapshot_community.json").read_text())["paths"]
        assert [p for p in paths if "edition" in p] == []

    def test_no_start_up_step_and_no_seed_step_is_the_reset(self):
        from app.composition.extensions import PROVISIONING, SEEDING
        from app.composition.startup import SHUTDOWN, STARTUP

        names = [s.name for s in STARTUP + SHUTDOWN] + SEEDING.names() + PROVISIONING.names()
        assert [n for n in names if "reset" in n or "edition" in n] == []
        import app.composition.startup as startup
        import app.database as database

        for module in (startup, database):
            source = inspect.getsource(module)
            assert not re.search(r"edition\.reset|edition\.cli|reset_enterprise_data|edition_reset", source)

    def test_the_command_and_the_reset_read_no_environment_variable(self):
        for module in (reset_cli, reset):
            source = inspect.getsource(module)
            assert not re.search(r"^\s*(import os\b|from os\b)", source, re.M)
            assert "os.environ" not in source
            assert "getenv" not in source
            assert "envvar" not in source
            assert "auto_envvar_prefix" not in source

    def test_the_flag_is_a_flag_with_no_default(self):
        options = {param.name: param for param in reset_cli.edition_reset.params}
        confirm = options["confirm_delete_enterprise_data"]
        assert confirm.is_flag is True
        assert confirm.default is False
        assert confirm.envvar is None
        assert confirm.opts == ["--confirm-delete-enterprise-data"]
        assert options["keep_tenant"].default is None
        assert options["keep_tenant"].envvar is None
