# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community Edition does not start on a database the Enterprise Edition has
used.

One schema serves both editions. A Community Edition that started on
Enterprise data would ignore it: policy-as-code bundles, a second tenant, knowledge-base bindings, cache configuration.
Governance controls would be switched off without a sign. So, at start, inside
the lock `init_database` holds, after the schema is current and before
anything else is written:

* An **Enterprise** composition writes the edition marker (the single row of
  `edition_marker`): the "first" columns once, the "last" columns each time.
  It checks nothing and never refuses.
* A **Community** composition writes nothing. It reads the marker, and it
  looks for Enterprise data directly, so that a database without the marker
  is still caught: a row in any Enterprise-only table, more than one tenant,
  an Enterprise feature flag switched on. If anything is found it
  raises `EditionRefusal`, and the process does not serve.

**It fails closed.** A marker that cannot be read, a table that is not there,
a check that raises or answers something that is no count: each is a refusal.
The Community Edition starts only when every check was made and found
nothing.

The refusal lists kinds and counts, never row contents, and never the text of
a database error (a driver error can quote a statement and its parameters).
It names the two ways forward: start the Enterprise Edition, or run the reset
command (`app/services/edition/reset.py`), which this module never calls.

Which edition a process is comes from its composition
(`app/composition/edition.py`), and so does the list of what is Enterprise
data. The core gateway makes the same check: the SQL below and the wording
of the message are shared with it word for word, and `tests/test_edition_guard.py` holds the
two together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.composition.edition import (
    COMMUNITY,
    ENTERPRISE,
    ENTERPRISE_TENANT_FEATURE_FLAGS,
    composed_enterprise_features,
    edition_of,
    enterprise_only_tables,
)
from app.core.logging_system import BrutorLogger
from app.env_vars import BRUTOR_VERSION

logger = BrutorLogger("edition")

# An Enterprise-only table is counted up to this many rows: the start-up
# check has to stay cheap on a table of millions (`kb_chunks`,
# `discovery_events`). The reset command counts exactly.
ROW_CAP = 1000

RESET_COMMAND = "python main.py edition-reset"
RESET_CONFIRM_FLAG = "--confirm-delete-enterprise-data"

KIND_MARKER = "marker"
KIND_TABLE = "table"
KIND_TENANTS = "tenants"
KIND_FLAG = "feature_flag"

MARKER_TABLE = "edition_marker"

MARKER_READ_SQL = (
    "SELECT enterprise_first_started_at, enterprise_last_started_at, "
    "enterprise_first_version, enterprise_last_version FROM edition_marker WHERE id = 1"
)

MARKER_WRITE_SQL = (
    "INSERT INTO edition_marker (id, enterprise_first_started_at, enterprise_last_started_at, "
    "enterprise_first_version, enterprise_last_version) "
    "VALUES (1, :now, :now, :version, :version) "
    "ON CONFLICT (id) DO UPDATE SET "
    "enterprise_last_started_at = excluded.enterprise_last_started_at, "
    "enterprise_last_version = excluded.enterprise_last_version"
)


@dataclass(frozen=True)
class Finding:
    """One kind of Enterprise data that was found: what, and how much."""

    kind: str
    subject: str
    count: int
    feature: Optional[str] = None
    capped: bool = False
    detail: Optional[str] = None


@dataclass(frozen=True)
class Undecided:
    """A check that could not be made. `error` is the exception's class name."""

    kind: str
    subject: str
    error: str


@dataclass(frozen=True)
class Check:
    """One direct check: a statement that answers a count, and how many is
    too many (`more_than`)."""

    kind: str
    subject: str
    feature: Optional[str]
    sql: str
    more_than: int = 0


def direct_checks(dialect: str = "postgresql") -> Tuple[Check, ...]:
    """Every direct check, in the order they run and are reported.

    The Postgres statements are the ones in the contract. SQLite (the test
    suite's default database) differs only in how it reads a JSON key.
    """
    checks: List[Check] = [
        Check(
            kind=KIND_TABLE,
            subject=table,
            feature=feature,
            sql=f"SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT {ROW_CAP}) AS capped",
        )
        for table, feature in enterprise_only_tables()
    ]
    checks.append(
        Check(kind=KIND_TENANTS, subject="tenants", feature=None, sql="SELECT count(*) FROM tenants", more_than=1)
    )
    for flag, feature in ENTERPRISE_TENANT_FEATURE_FLAGS.items():
        if dialect == "sqlite":
            sql = f"SELECT count(*) FROM tenants WHERE json_extract(features, '$.{flag}') IN (1, 'true')"
        else:
            sql = f"SELECT count(*) FROM tenants WHERE (features ->> '{flag}') = 'true'"
        checks.append(Check(kind=KIND_FLAG, subject=flag, feature=feature, sql=sql))
    return tuple(checks)


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def read_marker(conn: Connection) -> Optional[dict]:
    """The marker row, or None when the Enterprise Edition has not started
    here. Raises when the table cannot be read."""
    row = conn.execute(text(MARKER_READ_SQL)).first()
    if row is None:
        return None
    return {
        "enterprise_first_started_at": _iso(row[0]),
        "enterprise_last_started_at": _iso(row[1]),
        "enterprise_first_version": row[2],
        "enterprise_last_version": row[3],
    }


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def record_enterprise_start(conn: Connection, version: str, now: Optional[datetime] = None) -> None:
    """Write the marker: insert the row, or move its "last" columns on.

    One statement, so two instances starting together leave one row whatever
    the order (they are also serialized by `init_database`'s advisory lock
    when they share a database).
    """
    now = now or datetime.now(timezone.utc)
    conn.execute(text(MARKER_WRITE_SQL), {"now": now, "version": (version or "dev")[:50]})


# ---------------------------------------------------------------------------
# Looking for Enterprise data
# ---------------------------------------------------------------------------


def _run_check(conn: Connection, check: Check) -> Optional[Finding]:
    # In a savepoint: on Postgres a statement that fails aborts the
    # transaction, and the checks after it have to run all the same.
    with conn.begin_nested():
        value = conn.execute(text(check.sql)).scalar()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("the check did not answer a count")
    if value <= check.more_than:
        return None
    capped = check.kind == KIND_TABLE and value >= ROW_CAP
    return Finding(kind=check.kind, subject=check.subject, count=value, feature=check.feature, capped=capped)


def look_for_enterprise_data(conn: Connection) -> Tuple[List[Finding], List[Undecided]]:
    """Run every direct check. Returns what was found, and what could not be
    checked. Never raises for a failed check: a failed check is reported."""
    findings: List[Finding] = []
    undecided: List[Undecided] = []
    for check in direct_checks(conn.dialect.name):
        try:
            finding = _run_check(conn, check)
        except Exception as error:  # noqa: BLE001 - fail closed, whatever went wrong
            undecided.append(Undecided(kind=check.kind, subject=check.subject, error=type(error).__name__))
            continue
        if finding is not None:
            findings.append(finding)
    return findings, undecided


def _marker_finding(conn: Connection) -> Tuple[Optional[Finding], Optional[Undecided]]:
    try:
        with conn.begin_nested():
            marker = read_marker(conn)
    except Exception as error:  # noqa: BLE001 - fail closed, whatever went wrong
        return None, Undecided(kind=KIND_MARKER, subject=MARKER_TABLE, error=type(error).__name__)
    if marker is None:
        return None, None
    detail = (
        f"the Enterprise Edition first started on this database at "
        f"{marker['enterprise_first_started_at']} (version {marker['enterprise_first_version']}) "
        f"and last started at {marker['enterprise_last_started_at']} "
        f"(version {marker['enterprise_last_version']})"
    )
    return Finding(kind=KIND_MARKER, subject=MARKER_TABLE, count=1, detail=detail), None


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def _rows(finding: Finding) -> str:
    if finding.capped:
        return f"{finding.count} or more rows"
    return f"{finding.count} row" if finding.count == 1 else f"{finding.count} rows"


def describe(finding: Finding) -> str:
    """One line for one finding: the kind and the count, nothing of the rows."""
    if finding.kind == KIND_MARKER:
        return f"edition marker: {finding.detail}"
    if finding.kind == KIND_TABLE:
        return f"Enterprise table {finding.subject} ({finding.feature}): {_rows(finding)}"
    if finding.kind == KIND_TENANTS:
        return f"tenants: {finding.count} (the Community Edition runs a single tenant)"
    if finding.kind == KIND_FLAG:
        tenants = "1 tenant" if finding.count == 1 else f"{finding.count} tenants"
        return f"Enterprise feature flag {finding.subject} ({finding.feature}): switched on for {tenants}"
    return f"{finding.kind} {finding.subject}: {finding.count}"


def describe_undecided(item: Undecided) -> str:
    what = {
        KIND_MARKER: "edition marker",
        KIND_TABLE: f"Enterprise table {item.subject}",
        KIND_TENANTS: "tenants",
        KIND_FLAG: f"Enterprise feature flag {item.subject}",
    }.get(item.kind, f"{item.kind} {item.subject}")
    return f"{what}: the check failed ({item.error})"


def refusal_message(findings: Sequence[Finding], undecided: Sequence[Undecided]) -> str:
    lines = [
        "The Community Edition does not start on this database.",
    ]
    if findings:
        lines += [
            "This database has been used with the Enterprise Edition. Found:",
            *[f"  - {describe(finding)}" for finding in findings],
        ]
    if undecided:
        lines += [
            "Whether this database has been used with the Enterprise Edition could not be decided, "
            "and the Community Edition does not start when it cannot decide. Not checked:",
            *[f"  - {describe_undecided(item)}" for item in undecided],
        ]
    lines += [
        "Two ways forward:",
        "  1. Start the Enterprise Edition on this database.",
        "  2. Run the reset command of the control plane, which deletes the Enterprise data "
        "so that the Community Edition can start:",
        f"       {RESET_COMMAND}",
        f"     It prints what it would delete and changes nothing until it is given "
        f"{RESET_CONFIRM_FLAG}. Take a pg_dump first.",
    ]
    return "\n".join(lines)


class EditionRefusal(RuntimeError):
    """The Community Edition will not start on this database."""

    def __init__(self, findings: Iterable[Finding] = (), undecided: Iterable[Undecided] = ()):
        self.findings = tuple(findings)
        self.undecided = tuple(undecided)
        super().__init__(refusal_message(self.findings, self.undecided))


# ---------------------------------------------------------------------------
# The start
# ---------------------------------------------------------------------------


def enforce_edition(conn: Connection, enterprise_features: Iterable[str], version: str = "dev") -> str:
    """Do what this edition does at start, on `conn`'s transaction.

    Enterprise (`enterprise_features` is not empty): write the marker.
    Community: read the marker and look for Enterprise data; raise
    `EditionRefusal` unless every check was made and found nothing.
    Returns the edition.
    """
    edition = edition_of(enterprise_features)
    if edition == ENTERPRISE:
        record_enterprise_start(conn, version)
        return ENTERPRISE

    findings: List[Finding] = []
    undecided: List[Undecided] = []
    marker, marker_undecided = _marker_finding(conn)
    if marker is not None:
        findings.append(marker)
    if marker_undecided is not None:
        undecided.append(marker_undecided)
    found, not_checked = look_for_enterprise_data(conn)
    findings += found
    undecided += not_checked
    if findings or undecided:
        raise EditionRefusal(findings, undecided)
    return COMMUNITY


def enforce_edition_at_start(engine: Engine, enterprise_features: Optional[Iterable[str]] = None) -> str:
    """`enforce_edition` in a transaction of its own, for `init_database`.

    `enterprise_features` is what the application was composed of; when the
    caller has no application (`python main.py init`), the process-wide
    composition is read.
    """
    if enterprise_features is None:
        enterprise_features = composed_enterprise_features()
    version = BRUTOR_VERSION.require()
    try:
        with engine.begin() as conn:
            edition = enforce_edition(conn, enterprise_features, version)
    except EditionRefusal as refusal:
        logger.error(
            "Edition check refused the start",
            context={
                "found": [describe(f) for f in refusal.findings],
                "not_checked": [describe_undecided(u) for u in refusal.undecided],
            },
            exc_info=False,
        )
        raise
    logger.info(f"Edition check passed: {edition} edition")
    return edition
