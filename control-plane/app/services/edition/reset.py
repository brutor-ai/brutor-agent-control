# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The edition reset: delete the Enterprise Edition's data from a database, so
that the Community Edition starts on it.

An operator command and nothing else. It is reached from the command line
(`python main.py edition-reset`, `app/services/edition/cli.py`); no route, no
start-up step and no seed step calls it, it reads no environment variable,
and it never asks a question. Without `confirm=True` it reads and changes
nothing.

With it, in ONE transaction (all of it or none of it):

1. every row of the Enterprise-only tables (`ENTERPRISE_ONLY_TABLES`);
2. every tenant but the one kept, with everything it owns. Their rows of the
   audit hash chain are declared first (`audit_chain_prunes`, reason
   `manual`, actor `edition-reset`): a writer chain interleaves the tenants,
   and an undeclared hole reads as tampering in the kept tenant's evidence.
   Declarations made earlier are kept for the same reason;
3. the Enterprise feature flags: switched off on the kept tenant;
4. the edition marker;
5. a record of all this in the kept tenant's admin audit log.

After it, `guard.enforce_edition` finds nothing, and the Community Edition
starts. Running it again finds nothing to do and writes nothing.

What it does not touch: the kept tenant's Community data, its audit log, its
rows of the audit hash chain and its evidence log; and anything outside the
database (vector collections, deployed containers and models, stored files).

What the edition rules do not decide, it refuses instead of guessing:

* which tenant survives when there are several: `keep_tenant` must be given;
* a tenant other than the default one as the survivor: the Community Edition
  runs on the default tenant, and turning another tenant into it is a
  migration nobody has specified.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.composition.edition import ENTERPRISE_TENANT_FEATURE_FLAGS, enterprise_only_tables
from app.services.edition import guard

COMMAND = guard.RESET_COMMAND
CONFIRM_FLAG = guard.RESET_CONFIRM_FLAG
KEEP_TENANT_OPTION = "--keep-tenant"
AUDIT_ACTION = "edition_reset"
PRUNE_ACTOR = "edition-reset"

# Tables of a removed tenant that are kept: declarations about the audit hash
# chain, which is shared between tenants.
KEPT_TABLES_OF_A_REMOVED_TENANT = frozenset({"audit_chain_prunes"})

# The lock `init_database` holds while an instance starts.
INIT_ADVISORY_LOCK = 8742


@dataclass
class ResetPlan:
    """What a reset would delete: kinds and counts, read from the database."""

    marker: Optional[dict] = None
    tables: Dict[str, int] = field(default_factory=dict)
    tenants: List[dict] = field(default_factory=list)
    keep_tenant: Optional[str] = None
    remove_tenants: List[str] = field(default_factory=list)
    removed_tenant_rows: Dict[str, Dict[str, int]] = field(default_factory=dict)
    feature_flags: Dict[str, int] = field(default_factory=dict)
    blockers: List[str] = field(default_factory=list)

    @property
    def nothing_to_do(self) -> bool:
        return not (
            self.marker is not None
            or any(self.tables.values())
            or len(self.tenants) > 1
            or any(self.feature_flags.values())
        )


@dataclass
class ResetOutcome:
    plan: ResetPlan
    executed: bool = False
    audit_id: Optional[str] = None


def _tables():
    import app.database.models  # noqa: F401  (registers every model)
    from app.database import Base

    return Base.metadata.tables


def _count(db: Session, table, *where) -> int:
    return int(db.execute(select(func.count()).select_from(table).where(*where)).scalar() or 0)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_reset(db: Session, keep_tenant: Optional[str] = None) -> ResetPlan:
    """Read what a reset would delete. Changes nothing."""
    tables = _tables()
    tenants = tables["tenants"]
    enterprise = [name for name, _ in enterprise_only_tables()]

    plan = ResetPlan()
    plan.marker = guard.read_marker(db.connection())
    plan.tables = {name: _count(db, tables[name]) for name in enterprise}
    rows = db.execute(select(tenants.c.id, tenants.c.is_default, tenants.c.features).order_by(tenants.c.id)).all()
    plan.tenants = [{"id": row.id, "is_default": bool(row.is_default)} for row in rows]
    ids = [row.id for row in rows]

    # Which tenant is kept.
    blockers: List[str] = []
    kept = None
    listing = ", ".join(f"{t['id']}{' (the default tenant)' if t['is_default'] else ''}" for t in plan.tenants)
    if keep_tenant is not None:
        if keep_tenant not in ids:
            blockers.append(f"{KEEP_TENANT_OPTION} {keep_tenant}: there is no such tenant. Tenants: {listing}.")
        else:
            kept = keep_tenant
    elif len(ids) == 1:
        kept = ids[0]
    elif len(ids) > 1:
        blockers.append(
            f"There are {len(ids)} tenants and the Community Edition runs one. Every tenant but one is "
            f"deleted with everything it owns, and the command does not choose: name the tenant to keep "
            f"with {KEEP_TENANT_OPTION} <id>. Tenants: {listing}."
        )
    else:
        blockers.append(
            "There is no tenant, so there is no admin audit log to record the reset in. Start the "
            "Enterprise Edition once (it creates the default tenant), then run the command again."
        )
    if kept is not None and not next(t for t in plan.tenants if t["id"] == kept)["is_default"]:
        blockers.append(
            f"Tenant {kept} is not the default tenant. The Community Edition runs on the default tenant, "
            f"and the command does not turn another tenant into it. Keep the default tenant, or move the "
            f"data in the Enterprise Edition first. Tenants: {listing}."
        )
        kept = None
    plan.keep_tenant = kept
    plan.remove_tenants = [tenant_id for tenant_id in ids if tenant_id != kept] if kept is not None else []

    # What goes with the tenants that are removed (the Enterprise-only tables
    # are counted once, above).
    for tenant_id in plan.remove_tenants:
        counts = {}
        for name, table in tables.items():
            if "tenant_id" not in table.columns or name in enterprise or name == "tenants":
                continue
            if name in KEPT_TABLES_OF_A_REMOVED_TENANT:
                continue
            n = _count(db, table, table.c.tenant_id == tenant_id)
            if n:
                counts[name] = n
        plan.removed_tenant_rows[tenant_id] = counts

    # What is left to clear in the tenants that stay.
    staying = [tenant_id for tenant_id in ids if tenant_id not in plan.remove_tenants]
    plan.feature_flags = {
        flag: sum(1 for row in rows if row.id in staying and _is_on((row.features or {}).get(flag)))
        for flag in ENTERPRISE_TENANT_FEATURE_FLAGS
    }

    if not plan.nothing_to_do:
        plan.blockers = blockers
    return plan


def _is_on(value) -> bool:
    return value is True or value == "true"


# ---------------------------------------------------------------------------
# The plan, printed
# ---------------------------------------------------------------------------


def _n_rows(n: int) -> str:
    return "1 row" if n == 1 else f"{n} rows"


def format_plan(plan: ResetPlan, done: bool = False) -> str:
    """The plan as text: kinds and counts, nothing of the rows."""
    lines: List[str] = []
    features = dict(enterprise_only_tables())
    lines.append("Deleted:" if done else "It would delete:")
    if plan.marker is not None:
        lines.append(
            f"  - the edition marker (the Enterprise Edition first started here at "
            f"{plan.marker['enterprise_first_started_at']}, last at {plan.marker['enterprise_last_started_at']})"
        )
    filled = {name: n for name, n in plan.tables.items() if n}
    if filled:
        lines.append(
            f"  - every row of the Enterprise tables: {_n_rows(sum(filled.values()))} in "
            f"{len(filled)} of {len(plan.tables)} tables"
        )
        width = max(len(name) for name in filled)
        for name, n in filled.items():
            lines.append(f"      {name.ljust(width)} ({features[name]}): {_n_rows(n)}")
    for tenant_id in plan.remove_tenants:
        counts = plan.removed_tenant_rows.get(tenant_id, {})
        lines.append(
            f"  - tenant {tenant_id}, with everything it owns: {_n_rows(sum(counts.values()))} in "
            f"{len(counts)} tables, besides its rows of the Enterprise tables"
        )
        width = max((len(name) for name in counts), default=0)
        for name in sorted(counts):
            lines.append(f"      {name.ljust(width)}: {_n_rows(counts[name])}")
    on = {flag: n for flag, n in plan.feature_flags.items() if n}
    if on:
        lines.append(
            "  - Enterprise feature flags, switched off (not deleted): "
            + ", ".join(f"{flag} ({ENTERPRISE_TENANT_FEATURE_FLAGS[flag]})" for flag in on)
        )
    if plan.keep_tenant is not None:
        lines.append(
            f"Kept: tenant {plan.keep_tenant} and its Community Edition data, its admin audit log, its rows "
            f"of the audit hash chain and its evidence log."
        )
        lines.append(
            f"Recorded: the reset, in the admin audit log of tenant {plan.keep_tenant}"
            + (
                "; the removed tenants' rows of the audit hash chain are declared, so the kept "
                "tenant's evidence still verifies."
                if plan.remove_tenants
                else "."
            )
        )
    lines.append(
        "Not touched: anything outside the database (vector collections, deployed containers and "
        "models, stored files)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The reset
# ---------------------------------------------------------------------------


def _delete_enterprise_tables(db: Session, plan: ResetPlan) -> None:
    from app.database import Base

    enterprise = {name for name, _ in enterprise_only_tables()}
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in enterprise:
            db.execute(table.delete())


def _remove_tenants(db: Session, plan: ResetPlan) -> None:
    from app.database.models.audit_chain_prune import REASON_MANUAL
    from app.services.data_retention.sweeper import declare_prune
    from app.services.tenant.tenant_purge import purge_tenant_data

    tables = _tables()
    logs = tables["proxy_logs"]
    for tenant_id in plan.remove_tenants:
        doomed = db.execute(
            select(logs.c.chain_id, logs.c.chain_seq, logs.c.row_hash).where(logs.c.tenant_id == tenant_id)
        ).all()
        declare_prune(
            db,
            tenant_id=tenant_id,
            doomed=[(row[0], row[1], row[2]) for row in doomed],
            reason=REASON_MANUAL,
            actor=PRUNE_ACTOR,
        )
        db.flush()
        purge_tenant_data(db, tenant_id, keep_tables=KEPT_TABLES_OF_A_REMOVED_TENANT)
        db.execute(tables["tenants"].delete().where(tables["tenants"].c.id == tenant_id))


def _switch_feature_flags_off(db: Session, plan: ResetPlan) -> None:
    tenants = _tables()["tenants"]
    for row in db.execute(select(tenants.c.id, tenants.c.features)).all():
        features = dict(row.features or {})
        on = [flag for flag in ENTERPRISE_TENANT_FEATURE_FLAGS if _is_on(features.get(flag))]
        if not on:
            continue
        for flag in on:
            features[flag] = False
        db.execute(tenants.update().where(tenants.c.id == row.id).values(features=features))


def _delete_marker(db: Session) -> None:
    db.execute(_tables()["edition_marker"].delete())


def _record_in_audit_log(db: Session, plan: ResetPlan, operator: Optional[str]) -> str:
    audit_id = str(uuid.uuid4())
    db.execute(
        _tables()["tenant_admin_audit_logs"]
        .insert()
        .values(
            id=audit_id,
            tenant_id=plan.keep_tenant,
            admin_id=None,
            action=AUDIT_ACTION,
            resource_type="edition",
            resource_id="enterprise",
            request_method="CLI",
            request_path=COMMAND,
            success=True,
            details={
                "reset": "the Enterprise Edition's data was deleted so that the Community Edition can start",
                "operator": operator,
                "marker": plan.marker,
                "enterprise_tables": {name: n for name, n in plan.tables.items() if n},
                "removed_tenants": list(plan.remove_tenants),
                "removed_tenant_rows": plan.removed_tenant_rows,
                "feature_flags_switched_off": sorted(flag for flag, n in plan.feature_flags.items() if n),
            },
            timestamp=datetime.now(timezone.utc),
        )
    )
    return audit_id


def reset_enterprise_data(
    engine: Engine,
    *,
    confirm: bool = False,
    keep_tenant: Optional[str] = None,
    operator: Optional[str] = None,
) -> ResetOutcome:
    """Plan the reset and, only with `confirm=True`, carry it out.

    Returns the plan and whether anything was done. Nothing is done without
    `confirm`, when the plan has blockers, or when there is nothing to
    reset. A failure part way rolls everything back and is raised.
    """
    with Session(bind=engine, autoflush=False, expire_on_commit=False) as db:
        try:
            if confirm is True and db.get_bind().dialect.name == "postgresql":
                # Wait for an instance that is starting on this database.
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INIT_ADVISORY_LOCK})
            plan = plan_reset(db, keep_tenant)
            if confirm is not True or plan.blockers or plan.nothing_to_do:
                db.rollback()
                return ResetOutcome(plan=plan, executed=False)
            _delete_enterprise_tables(db, plan)
            _remove_tenants(db, plan)
            _switch_feature_flags_off(db, plan)
            _delete_marker(db)
            audit_id = _record_in_audit_log(db, plan, operator)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return ResetOutcome(plan=plan, executed=True, audit_id=audit_id)
