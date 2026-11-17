# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The edition guard, the start-up half: the Community Edition does not start
on a database the Enterprise Edition has used.

* The edition is read from the composition (`app/composition/edition.py`).
  This build composes both editions, so it is the Enterprise Edition; the
  Community composition is asked for with `community_composition()`.
* An Enterprise start writes the edition marker and never refuses.
* A Community start reads the marker and looks for Enterprise data directly,
  and refuses for each kind: the marker, a row in any Enterprise-only table,
  a second tenant, an
  Enterprise feature flag switched on. It writes nothing.
* It fails closed: a marker it cannot read, or a check that fails, is a
  refusal.
* What is Enterprise data is the same list as the boundary file
  (`editions.yaml`), and the SQL of every check is the SQL in the
  edition-install contract the core implements from. Both are read from the
  directory `BRUTOR_EDITION_RECORDS_DIR` names; without it those tests skip.

Every test runs on SQLite; with `BRUTOR_PG_TESTS=1` also on a throwaway
Postgres database this module creates and drops.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
from unittest import mock

import pytest
from sqlalchemy import text

from app.composition import edition as edition_map
from app.composition.edition import (
    COMMUNITY,
    ENTERPRISE,
    ENTERPRISE_ONLY_TABLES,
    ENTERPRISE_TENANT_FEATURE_FLAGS,
    composed_edition,
    composed_enterprise_features,
    default_tenant_feature_flags,
    enterprise_only_tables,
)
from app.composition.features import ENTERPRISE_FEATURES, MULTI_TENANT
from app.services.edition import guard
from app.services.edition.guard import EditionRefusal, enforce_edition
from tests.edition_boundary import edition_boundary_file, edition_record
from tests.edition_compositions import (
    community_composition,
    count,
    empty_every_table,
    insert_row,
    insert_tenant,
    pg_enabled,
    scratch_database,
)

# The edition-install contract, in the edition records directory beside the
# boundary file (`BRUTOR_EDITION_RECORDS_DIR`).
CONTRACT_NAME = "d15-contract.md"

NO_FEATURES = frozenset()
ALL_TABLES = [table for table, _ in enterprise_only_tables()]
ALL_FLAGS = list(ENTERPRISE_TENANT_FEATURE_FLAGS)

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


@pytest.fixture
def db(engine):
    """An empty schema with the Community Edition's single tenant."""
    empty_every_table(engine)
    with engine.begin() as conn:
        insert_tenant(conn)
    yield engine
    empty_every_table(engine)


def _start(engine, features=NO_FEATURES, version="1.0.0"):
    with engine.begin() as conn:
        return enforce_edition(conn, features, version)


def _refusal(engine) -> EditionRefusal:
    with pytest.raises(EditionRefusal) as caught:
        _start(engine)
    return caught.value


def _marker_rows(engine):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, enterprise_first_started_at, enterprise_last_started_at, "
                "enterprise_first_version, enterprise_last_version FROM edition_marker"
            )
        ).all()


def _assert_names_both_ways_forward(message: str) -> None:
    assert "has been used with the Enterprise Edition" in message
    assert "Start the Enterprise Edition on this database" in message
    assert "python main.py edition-reset" in message
    assert "--confirm-delete-enterprise-data" in message
    assert "pg_dump" in message


# ---------------------------------------------------------------------------
# The map: what is Enterprise data
# ---------------------------------------------------------------------------


def _editions_yaml():
    return edition_boundary_file()


class TestTheMapIsTheBoundaryFile:
    def test_the_enterprise_only_tables_are_the_boundary_files(self):
        listed = _editions_yaml()["control_plane"]["enterprise_only_tables"]
        assert {feature: tuple(tables) for feature, tables in listed.items()} == ENTERPRISE_ONLY_TABLES

    def test_the_enterprise_feature_flags_are_the_boundary_files(self):
        flags = _editions_yaml()["control_plane"]["tenant_feature_flags"]
        assert sorted(flags["enterprise"]) == sorted(ENTERPRISE_TENANT_FEATURE_FLAGS)
        assert not set(flags["community"]) & set(ENTERPRISE_TENANT_FEATURE_FLAGS)
        assert not set(flags["split"]) & set(ENTERPRISE_TENANT_FEATURE_FLAGS)

    def test_every_flag_belongs_to_a_feature_the_boundary_file_names(self):
        features = set(_editions_yaml()["enterprise_features"])
        assert set(ENTERPRISE_TENANT_FEATURE_FLAGS.values()) <= features
        assert set(ENTERPRISE_ONLY_TABLES) <= features

    def test_every_table_is_a_table_of_the_schema_and_is_listed_once(self):
        import app.database.models  # noqa: F401
        from app.database import Base

        assert len(ALL_TABLES) == len(set(ALL_TABLES)) == 37
        assert set(ALL_TABLES) <= set(Base.metadata.tables)
        assert set(ENTERPRISE_ONLY_TABLES) <= ENTERPRISE_FEATURES

    def test_every_flag_is_a_tenant_feature_key(self):
        from app.constants.tenant import TENANT_FEATURE_KEYS

        assert set(ENTERPRISE_TENANT_FEATURE_FLAGS) <= set(TENANT_FEATURE_KEYS)
        assert set(ENTERPRISE_TENANT_FEATURE_FLAGS.values()) <= ENTERPRISE_FEATURES

    def test_the_marker_table_is_not_enterprise_data(self):
        assert "edition_marker" not in ALL_TABLES


class TestTheContractTheCoreImplementsFrom:
    """The edition-install contract carries the SQL the core runs. It is held to the SQL
    this plane runs, so the two planes cannot come to disagree."""

    def _contract(self) -> str:
        return edition_record(CONTRACT_NAME).read_text(encoding="utf-8")

    def test_it_has_the_sql_of_every_check(self):
        contract = self._contract()
        missing = [check.sql for check in guard.direct_checks("postgresql") if check.sql not in contract]
        assert missing == []
        assert guard.MARKER_READ_SQL in contract

    def test_it_has_one_check_for_every_kind(self):
        checks = guard.direct_checks("postgresql")
        assert [c.subject for c in checks if c.kind == "table"] == ALL_TABLES
        assert [c.subject for c in checks if c.kind == "feature_flag"] == ALL_FLAGS
        assert sorted({c.kind for c in checks}) == [
            "feature_flag",
            "table",
            "tenants",
        ]

    def test_it_has_the_wording_of_the_refusal(self):
        contract = self._contract()
        message = guard.refusal_message(
            [guard.Finding(kind="tenants", subject="tenants", count=2)], []
        )
        for line in message.splitlines():
            if line.strip() and not line.startswith("  - "):
                assert line.strip() in contract, line

    def test_its_check_table_is_this_planes_checks_in_order(self):
        """Section 4.2 lists exactly the checks this plane runs: no row more,
        none less, in order, with kind, subject, feature, threshold and SQL."""
        contract = self._contract()
        section = contract[contract.index("### 4.2 The direct checks") :]
        section = section[: section.index("\n## 5.")]
        rows = []
        for line in section.splitlines():
            cells = line.split("|")
            if len(cells) != 8 or not cells[1].strip().isdigit():
                continue
            rows.append(
                (
                    int(cells[1]),
                    *(cell.strip().strip("`") for cell in cells[2:5]),
                    int(cells[5]),
                    cells[6].strip().strip("`"),
                )
            )
        ours = [
            (n, c.kind, c.subject, c.feature or "none", c.more_than, c.sql)
            for n, c in enumerate(guard.direct_checks("postgresql"), start=1)
        ]
        assert rows == ours
        assert f"{len(ours)} checks, run and reported in this order" in section
        assert f"in that file's order ({len(ALL_TABLES)} tables)" in section
        assert f"flag list is `control_plane.tenant_feature_flags.enterprise` ({len(ALL_FLAGS)} flags)" in section
        assert f"`LIMIT {guard.ROW_CAP}` inside" in section

    def test_its_line_formats_are_this_planes_kinds(self):
        contract = self._contract()
        section = contract[contract.index("Line formats:") : contract.index("## 7.")]
        kinds = re.findall(r"^\| `(\w+)` \|", section, re.M)
        assert kinds == [guard.KIND_MARKER, guard.KIND_TABLE, guard.KIND_TENANTS, guard.KIND_FLAG]

    def test_its_refusals_are_what_this_plane_prints(self):
        """The two example refusals of section 6, the marker line aside
        (its times come from the database), are this plane's text for the
        same findings."""
        contract = self._contract()
        section = contract[contract.index("## 6. The wording of the refusal") :]
        blocks = re.findall(r"```text\n(.*?)\n```", section, re.S)
        checks = {(c.kind, c.subject): c for c in guard.direct_checks("postgresql")}

        def found(kind, subject, count):
            check = checks[(kind, subject)]
            return guard.Finding(
                kind=kind, subject=subject, count=count, feature=check.feature, capped=count >= guard.ROW_CAP
            )

        with_findings = guard.refusal_message(
            [
                found(guard.KIND_TABLE, "policy_bundles", 3),
                found(guard.KIND_TABLE, "system_admins", 1),
                found(guard.KIND_TABLE, "kb_chunks", 1000),
                found(guard.KIND_TENANTS, "tenants", 2),
                found(guard.KIND_FLAG, "knowledge_base", 1),
                found(guard.KIND_FLAG, "mcp_registry", 2),
            ],
            [],
        )
        documented = [line for line in blocks[0].splitlines() if not line.startswith("  - edition marker: ")]
        assert documented == with_findings.splitlines()

        undecided = guard.refusal_message(
            [found(guard.KIND_TENANTS, "tenants", 2)],
            [
                guard.Undecided(kind=guard.KIND_MARKER, subject=guard.MARKER_TABLE, error="UndefinedTable"),
                guard.Undecided(kind=guard.KIND_TABLE, subject="batch_jobs", error="ProgrammingError"),
            ],
        )
        assert blocks[1] == undecided

    def test_its_reset_is_this_planes(self):
        contract = self._contract()
        section = contract[contract.index("## 7. The reset command") :]
        assert f"deletes every row of the {len(ALL_TABLES)} Enterprise-only tables" in section
        assert "SAML" not in section and "idp_subject" not in section

    def test_it_has_the_marker_table(self):
        contract = self._contract()
        from app.database.models.edition_marker import EditionMarker

        assert "CREATE TABLE edition_marker" in contract
        for column in EditionMarker.__table__.columns:
            assert column.name in contract


# ---------------------------------------------------------------------------
# The edition is read from the composition
# ---------------------------------------------------------------------------


class TestTheEditionIsReadFromTheComposition:


    def test_one_enterprise_entry_makes_it_enterprise(self):
        from app.composition.startup import LifecycleStep

        step = LifecycleStep("x", lambda context: None, feature=MULTI_TENANT)
        with community_composition():
            assert composed_enterprise_features(extra=[step]) == {MULTI_TENANT}
            assert composed_edition(extra=[step]) == ENTERPRISE

    def test_a_tag_that_is_no_enterprise_feature_is_refused(self):
        from app.composition.startup import LifecycleStep

        with pytest.raises(ValueError):
            composed_enterprise_features(extra=[LifecycleStep("x", lambda c: None, feature="nonsense")])

    def test_the_application_hands_its_composition_to_the_database_step(self):
        """`create_app` composes; the `database` step passes on what was
        composed, extensions included."""
        from app.composition.startup import STARTUP, StartupContext

        step = next(s for s in STARTUP if s.name == "database")
        context = StartupContext(
            app=None, services=None, logger=mock.MagicMock(), enterprise_features=frozenset({MULTI_TENANT})
        )
        with mock.patch("app.composition.startup.init_database") as init:
            step.run(context)
        init.assert_called_once_with(enterprise_features=frozenset({MULTI_TENANT}))

    def test_create_app_computes_the_features_of_what_it_composed(self):
        import app.app as app_module

        source = inspect.getsource(app_module.create_app)
        assert "composed_enterprise_features(" in source
        assert "enterprise_features=" in source


class TestTheDefaultTenantsFeatureFlags:

    def test_the_community_composition_leaves_the_enterprise_flags_off(self):
        """Otherwise a Community Edition would switch on what it cannot serve,
        and refuse its own database at the next start."""
        from app.constants.tenant import TENANT_FEATURE_KEYS
        from app.database.models.tenant import Tenant

        with community_composition():
            features = Tenant.create_default_tenant().features
        assert set(features) == set(TENANT_FEATURE_KEYS)
        assert {k for k, on in features.items() if not on} == set(ENTERPRISE_TENANT_FEATURE_FLAGS)


# ---------------------------------------------------------------------------
# Enterprise: writes the marker, never refuses
# ---------------------------------------------------------------------------


class TestAnEnterpriseStart:
    def test_it_writes_the_marker_on_an_empty_database(self, db):
        assert _marker_rows(db) == []
        assert _start(db, frozenset({MULTI_TENANT}), "1.2.3") == ENTERPRISE
        rows = _marker_rows(db)
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].enterprise_first_started_at is not None
        assert rows[0].enterprise_first_version == rows[0].enterprise_last_version == "1.2.3"

    def test_a_second_start_keeps_one_row_and_the_first_start(self, db):
        _start(db, frozenset({MULTI_TENANT}), "1.2.3")
        first = _marker_rows(db)[0]
        _start(db, frozenset({MULTI_TENANT}), "1.3.0")
        rows = _marker_rows(db)
        assert len(rows) == 1
        assert rows[0].enterprise_first_started_at == first.enterprise_first_started_at
        assert rows[0].enterprise_first_version == "1.2.3"
        assert rows[0].enterprise_last_version == "1.3.0"
        assert rows[0].enterprise_last_started_at >= first.enterprise_last_started_at

    def test_it_never_refuses_whatever_is_in_the_database(self, db):
        with db.begin() as conn:
            insert_tenant(conn, "acme")
            insert_row(conn, "policy_bundles")
            insert_row(conn, "idp_servers", protocol="saml")
        assert _start(db, frozenset({MULTI_TENANT})) == ENTERPRISE
        assert _start(db, frozenset({MULTI_TENANT})) == ENTERPRISE

    def test_it_does_not_look_for_enterprise_data_at_all(self, db):
        with mock.patch.object(guard, "look_for_enterprise_data", side_effect=AssertionError("looked")):
            assert _start(db, frozenset({MULTI_TENANT})) == ENTERPRISE


    @pytest.mark.skipif(not pg_enabled(), reason="concurrent writers need Postgres; set BRUTOR_PG_TESTS=1")
    def test_concurrent_starts_leave_one_consistent_row(self, db):
        if db.dialect.name != "postgresql":
            pytest.skip("concurrent writers need Postgres")
        errors = []
        barrier = threading.Barrier(12)

        def start(n):
            try:
                barrier.wait(timeout=30)
                _start(db, frozenset({MULTI_TENANT}), f"1.0.{n}")
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=start, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        rows = _marker_rows(db)
        assert len(rows) == 1
        assert rows[0].enterprise_first_started_at <= rows[0].enterprise_last_started_at
        assert re.fullmatch(r"1\.0\.\d+", rows[0].enterprise_first_version)


# ---------------------------------------------------------------------------
# Community: starts on its own database, refuses on Enterprise's
# ---------------------------------------------------------------------------


class TestACommunityStart:
    def test_it_starts_on_a_fresh_database(self, db):
        assert _start(db) == COMMUNITY

    def test_it_starts_on_a_database_with_no_tenant_yet(self, engine):
        empty_every_table(engine)
        assert _start(engine) == COMMUNITY

    def test_it_starts_on_a_database_holding_community_data(self, db):
        with db.begin() as conn:
            insert_row(conn, "tenant_roles")
            insert_row(conn, "tenant_admins")
            insert_row(conn, "idp_servers", protocol="oidc")
            insert_row(conn, "idp_servers", protocol=None)
            insert_row(conn, "llm_model")
            insert_row(conn, "proxy_logs")
        assert _start(db) == COMMUNITY

    def test_it_writes_nothing(self, db):
        _start(db)
        _start(db)
        assert _marker_rows(db) == []

    def test_the_community_composition_of_this_build_starts(self, db):
        with community_composition():
            assert guard.enforce_edition_at_start(db) == COMMUNITY
        assert _marker_rows(db) == []

    def test_it_refuses_when_the_marker_is_present(self, db):
        _start(db, frozenset({MULTI_TENANT}), "9.9.9")
        refusal = _refusal(db)
        assert [(f.kind, f.subject) for f in refusal.findings] == [("marker", "edition_marker")]
        message = str(refusal)
        assert "edition marker" in message
        assert "9.9.9" in message
        _assert_names_both_ways_forward(message)

    @pytest.mark.parametrize("table", ALL_TABLES)
    def test_it_refuses_for_a_row_in_an_enterprise_only_table(self, db, table):
        with db.begin() as conn:
            insert_row(conn, table)
        refusal = _refusal(db)
        feature = dict(enterprise_only_tables())[table]
        assert [(f.kind, f.subject, f.feature, f.count) for f in refusal.findings] == [
            ("table", table, feature, 1)
        ]
        assert refusal.undecided == ()
        message = str(refusal)
        assert f"Enterprise table {table} ({feature}): 1 row" in message
        _assert_names_both_ways_forward(message)

    def test_it_refuses_for_a_second_tenant(self, db):
        with db.begin() as conn:
            insert_tenant(conn, "acme")
        refusal = _refusal(db)
        assert [(f.kind, f.count) for f in refusal.findings] == [("tenants", 2)]
        assert "tenants: 2" in str(refusal)
        _assert_names_both_ways_forward(str(refusal))

    def test_a_stored_saml_row_is_no_finding(self, db):
        """SAML single sign-on is not in this release: nothing reads a
        stored `saml` identity provider or an administrator's external
        identity, and the guard does not look for them."""
        with db.begin() as conn:
            insert_row(conn, "idp_servers", protocol="saml")
            insert_row(conn, "tenant_admins", idp_subject="name-id-of-someone")
        assert _start(db) == COMMUNITY

    @pytest.mark.parametrize("flag", ALL_FLAGS)
    def test_it_refuses_for_an_enterprise_feature_flag_switched_on(self, engine, flag):
        from tests.edition_compositions import community_feature_flags

        empty_every_table(engine)
        with engine.begin() as conn:
            insert_tenant(conn, features={**community_feature_flags(), flag: True})
        refusal = _refusal(engine)
        feature = ENTERPRISE_TENANT_FEATURE_FLAGS[flag]
        assert [(f.kind, f.subject, f.feature, f.count) for f in refusal.findings] == [
            ("feature_flag", flag, feature, 1)
        ]
        assert f"Enterprise feature flag {flag} ({feature}): switched on for 1 tenant" in str(refusal)
        _assert_names_both_ways_forward(str(refusal))
        empty_every_table(engine)

    def test_a_flag_that_is_off_or_absent_is_not_a_finding(self, engine):
        empty_every_table(engine)
        with engine.begin() as conn:
            insert_tenant(conn, features={"knowledge_base": False, "models": True})
        assert _start(engine) == COMMUNITY
        empty_every_table(engine)

    def test_it_lists_everything_it_found_with_counts_and_no_row_contents(self, db):
        secret = "s3cr3t-value-that-must-not-be-printed"
        with db.begin() as conn:
            insert_tenant(conn, "acme", display_name=secret)
            for _ in range(3):
                insert_row(conn, "policy_bundles", tenant_id=secret)
            insert_row(conn, "batch_jobs", name=secret)
        _start(db, frozenset({MULTI_TENANT}))
        refusal = _refusal(db)
        assert [(f.kind, f.subject, f.count) for f in refusal.findings] == [
            ("marker", "edition_marker", 1),
            ("table", "policy_bundles", 3),
            ("table", "batch_jobs", 1),
            ("tenants", "tenants", 2),
        ]
        message = str(refusal)
        assert "Enterprise table policy_bundles (policy_as_code): 3 rows" in message
        assert secret not in message
        assert "acme" not in message

    def test_a_large_table_is_counted_up_to_a_cap(self, db, monkeypatch):
        monkeypatch.setattr(guard, "ROW_CAP", 2)
        with db.begin() as conn:
            for _ in range(5):
                insert_row(conn, "batch_jobs")
        refusal = _refusal(db)
        assert [(f.subject, f.count, f.capped) for f in refusal.findings] == [("batch_jobs", 2, True)]
        assert "Enterprise table batch_jobs (batch_jobs): 2 or more rows" in str(refusal)

    def test_a_refused_start_changes_nothing(self, db):
        with db.begin() as conn:
            insert_row(conn, "policy_bundles")
        _refusal(db)
        with db.connect() as conn:
            assert count(conn, "policy_bundles") == 1
            assert count(conn, "edition_marker") == 0
            assert count(conn, "tenants") == 1


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


class TestItFailsClosed:
    def _without_table(self, engine, name):
        from app.database import Base

        table = Base.metadata.tables[name]

        class _Dropped:
            def __enter__(self_inner):
                with engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE "{name}"' + (" CASCADE" if engine.dialect.name == "postgresql" else "")))

            def __exit__(self_inner, *exc):
                table.create(bind=engine)
                return False

        return _Dropped()

    def test_a_missing_marker_table_is_a_refusal(self, db):
        with self._without_table(db, "edition_marker"):
            refusal = _refusal(db)
        assert refusal.findings == ()
        assert [(u.kind, u.subject) for u in refusal.undecided] == [("marker", "edition_marker")]
        message = str(refusal)
        assert "could not be decided" in message
        assert "edition marker" in message
        _assert_names_both_ways_forward(message)

    def test_a_missing_enterprise_table_is_a_refusal(self, db):
        with self._without_table(db, "batch_requests"):
            refusal = _refusal(db)
        assert [(u.kind, u.subject) for u in refusal.undecided] == [("table", "batch_requests")]
        assert "batch_requests" in str(refusal)

    def test_an_unreadable_marker_is_a_refusal(self, db):
        with mock.patch.object(guard, "read_marker", side_effect=RuntimeError("boom")):
            refusal = _refusal(db)
        assert [(u.kind, u.subject) for u in refusal.undecided] == [("marker", "edition_marker")]

    def test_a_check_that_raises_is_a_refusal_and_the_others_still_run(self, db):
        with db.begin() as conn:
            insert_row(conn, "policy_bundles")
            insert_tenant(conn, "acme")
        real = guard.direct_checks

        def with_a_broken_check(dialect="postgresql"):
            checks = list(real(dialect))
            broken = guard.Check(
                kind="table", subject="kb_chunks", feature="knowledge_base", sql="SELECT count(*) FROM no_such_table"
            )
            return tuple(broken if c.subject == "kb_chunks" else c for c in checks)

        with mock.patch.object(guard, "direct_checks", with_a_broken_check):
            refusal = _refusal(db)
        assert [(u.kind, u.subject) for u in refusal.undecided] == [("table", "kb_chunks")]
        # The failed statement did not stop the checks after it.
        assert [(f.kind, f.subject) for f in refusal.findings] == [
            ("table", "policy_bundles"),
            ("tenants", "tenants"),
        ]

    def test_a_check_that_raises_alone_is_a_refusal(self, db):
        """Nothing was found, and one check could not be made: not a start."""

        def only_a_broken_check(dialect="postgresql"):
            return (guard.Check(kind="tenants", subject="tenants", feature=None, sql="SELECT nonsense FROM"),)

        with mock.patch.object(guard, "direct_checks", only_a_broken_check):
            refusal = _refusal(db)
        assert refusal.findings == ()
        assert len(refusal.undecided) == 1
        assert "could not be decided" in str(refusal)

    def test_a_check_answering_something_that_is_no_count_is_a_refusal(self, db):
        def a_check_answering_null(dialect="postgresql"):
            return (guard.Check(kind="tenants", subject="tenants", feature=None, sql="SELECT NULL"),)

        with mock.patch.object(guard, "direct_checks", a_check_answering_null):
            refusal = _refusal(db)
        assert len(refusal.undecided) == 1

    def test_the_error_text_of_a_failed_check_is_not_printed(self, db):
        """A driver error can quote the statement and its parameters."""
        with mock.patch.object(guard, "read_marker", side_effect=RuntimeError("password=hunter2")):
            message = str(_refusal(db))
        assert "hunter2" not in message
        assert "RuntimeError" in message


# ---------------------------------------------------------------------------
# Where the start refuses
# ---------------------------------------------------------------------------


class TestWhereTheStartRefuses:
    def test_the_check_runs_after_the_schema_and_before_anything_else_writes(self):
        import app.database as database

        body = inspect.getsource(database.init_database)
        check = body.index("enforce_edition_at_start(")
        assert body.index("_alembic_upgrade_head()") < check
        assert "create_all(" not in body
        assert check < body.index("_update_schema_version()")
        assert check < body.index("seed_initial_data()")
        assert check < body.index("apply_pending_at_startup()")
        assert body.index("pg_advisory_lock(8742)") < check < body.index("pg_advisory_unlock(8742)")

    def test_a_refusal_stops_the_application_before_it_serves(self):
        """The `database` step raises; the lifespan re-raises; nothing serves."""
        from tests.app_factory import create_app

        refusal = EditionRefusal([guard.Finding(kind="tenants", subject="tenants", count=2)], [])
        served = []

        async def lifespan(app):
            async with app.router.lifespan_context(app):
                served.append(True)

        with mock.patch("app.composition.startup.init_database", side_effect=refusal):
            app = create_app()
            with pytest.raises(EditionRefusal):
                asyncio.run(lifespan(app))
        assert served == []

    def test_the_refusal_is_not_swallowed_by_init_database(self):
        import app.database as database

        body = inspect.getsource(database.init_database)
        tail = body[body.rindex("except Exception as e:") :]
        assert "raise" in tail

    def test_the_guard_never_calls_the_reset(self):
        source = inspect.getsource(guard)
        assert "import" in source
        assert "edition.reset" not in source.replace("edition-reset", "")
        assert "reset_enterprise_data" not in source


def test_the_map_module_imports_no_enterprise_module():
    source = inspect.getsource(edition_map)
    assert "app.services" not in source
    assert "app.api" not in source
