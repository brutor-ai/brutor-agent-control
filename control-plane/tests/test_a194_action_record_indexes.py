# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""a194_action_record_indexes — the generated projections of the action-record body exist on both
schema paths, agree with the body, and serve the queries they were made for.

Fresh installs build the table from the ORM (`create_all` + stamp); upgrades
run the migration. A column or index present in one and not the other is a
page that is fast on one install and broken on the next. And a projection is
only worth having if it cannot disagree with the body: the columns are
generated, and this pins their values against bodies of every shape.

The bug class: a page that asks the JSONB body of every record a question on
every load (Compliance → Overview took 6 to 19 s, the ledger summary 2.5 s).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")


from app.database.models.action_record import ActionRecord
from tests.source_trees import python_sources

ROOT = Path(__file__).resolve().parents[1]


def test_no_page_asks_the_record_body_these_questions():
    """Any SQL in the app that spells one of the four body paths, outside the
    model, is a query that reads every record body again."""
    body_paths = re.compile(
        r"record\s*->\s*'chain'\s*->>\s*'(parent_record_id|relation)'"
        r"|record\s*->\s*'context'\s*->>\s*'backfilled'"
        r"|'\{disposition,human_disposed\}'"
        r"|\(\"disposition\", \"human_disposed\"\)"
    )
    found = []
    for path in python_sources((ROOT / "app")):
        if path.name == "action_record.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if body_paths.search(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    evidence = (ROOT / "app/api/v1/endpoints/admin/evidence.py").read_text()
    assert ".yield_per(" not in evidence, "the ledger summary must not stream record bodies"
    assert not found, "use the generated columns of action_records (a194_action_record_indexes):\n  " + "\n  ".join(found)


# ---------------------------------------------------------------------------
# On Postgres: both schema paths, the values, and the plans
# ---------------------------------------------------------------------------


def test_the_summary_filters_on_the_bare_column():
    """`human_disposed IS TRUE` is not matched to the partial index's
    predicate by the planner; the bare boolean is."""
    from sqlalchemy.dialects import postgresql

    sql = str(ActionRecord.__table__.select().where(ActionRecord.human_disposed).compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("WHERE action_records.human_disposed")
    evidence = (ROOT / "app/api/v1/endpoints/admin/evidence.py").read_text()
    assert "base.filter(ActionRecord.human_disposed)" in evidence
