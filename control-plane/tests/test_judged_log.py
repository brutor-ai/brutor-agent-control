# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`judged_log` decides which proxy-log rows count towards a rate. It had no direct
test. A correlated form of it (0.11.5) raised the fleet list's plan cost past
Postgres's JIT thresholds: about 1.3 s of compilation per call on a large tenant,
with an empty incident-window table. These pin what it means, and that it stays
uncorrelated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import String, case, create_engine, literal, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models.ai_system_drift_resolution import AISystemIncidentWindow
from app.database.models.ai_system_run import AISystemRun
from app.database.models.proxy_log import ProxyLog
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_log

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
T = "t-a"
OTHER = "t-b"


def _window(tenant, system):
    return AISystemIncidentWindow(
        id=f"w-{tenant}-{system}",
        tenant_id=tenant,
        ai_system_id=system,
        starts_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1),
        cause_category="provider_outage",
        reason="test",
        declared_by="test",
    )


def _run(tenant, root, system, at=NOW):
    return AISystemRun(
        id=f"r-{tenant}-{root}",
        tenant_id=tenant,
        root_task_id=root,
        ai_system_id=system,
        started_at=at,
        terminal_state="completed",
        finalized_by="explicit",
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (AISystemRun, AISystemIncidentWindow):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _judged(db, root):
    """The predicate's value for a log row of tenant T with this root task."""
    log = select(literal(root, String).label("root_task_id")).subquery("l")
    return db.execute(select(case((judged_log(log.c, T, purpose=PURPOSE_RATE), 1), else_=0))).scalar() == 1


def test_a_row_of_a_run_outside_every_window_counts(db):
    db.add(_run(T, "root", "sys"))
    db.commit()
    assert _judged(db, "root")


def test_a_row_of_a_run_inside_its_systems_window_does_not_count(db):
    db.add_all([_run(T, "root", "sys"), _window(T, "sys")])
    db.commit()
    assert not _judged(db, "root")


def test_a_tenant_wide_window_covers_every_system(db):
    db.add_all([_run(T, "root", "sys"), _window(T, None)])
    db.commit()
    assert not _judged(db, "root")


def test_another_systems_window_does_not_excuse_this_run(db):
    db.add_all([_run(T, "root", "sys"), _window(T, "other-sys")])
    db.commit()
    assert _judged(db, "root")


def test_a_run_that_started_outside_the_window_counts(db):
    db.add_all([_run(T, "root", "sys", at=NOW - timedelta(hours=2)), _window(T, "sys")])
    db.commit()
    assert _judged(db, "root")


def test_another_tenants_window_and_run_do_not_excuse_this_tenants_row(db):
    db.add_all([_run(OTHER, "root", "sys"), _window(OTHER, "sys"), _run(T, "root", "sys")])
    db.commit()
    assert _judged(db, "root")


def test_a_row_of_no_run_counts_even_beside_an_excluded_run(db):
    db.add_all([_run(T, "excluded", "sys"), _window(T, "sys")])
    db.commit()
    assert _judged(db, "no-such-run")
    assert _judged(db, None)  # NOT IN alone would make this NULL, so not judged


def test_the_predicate_stays_uncorrelated():
    """A subquery that references the log row is what tripped JIT, per row."""
    sql = str(judged_log(ProxyLog, T, purpose=PURPOSE_RATE).compile(compile_kwargs={"literal_binds": True}))
    subquery = sql[sql.index("SELECT"):]
    assert "proxy_logs" not in subquery, sql


def test_a_fact_may_not_use_the_exclusion():
    with pytest.raises(ValueError):
        judged_log(ProxyLog, T, purpose="fact")
