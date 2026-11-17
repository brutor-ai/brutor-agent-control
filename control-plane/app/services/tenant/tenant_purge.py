# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Deleting everything a tenant owns.

Two callers: the Tenant Console's "delete tenant"
(`enterprise/brutor_enterprise/multi_tenant/endpoints/tenant_management.py`) and the edition reset
command (`app/services/edition/reset.py`), which removes every
tenant but one. The tenant's own row is left to the caller.
"""

from __future__ import annotations

from typing import Collection

from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session


def purge_tenant_data(db: Session, tenant_id: str, keep_tables: Collection[str] = ()) -> None:
    """Delete every row carrying `tenant_id`, in the caller's transaction.

    `keep_tables` are left alone (the reset keeps the declarations about the
    shared audit chain). Raises `RuntimeError` when the purge cannot finish.

    GENERIC DELETION IN REVERSE DEPENDENCY ORDER

    This used to be a hand-maintained list (admin sessions, audit logs,
    admins) followed by "let CASCADE handle the rest". That rotted: tenant
    creation seeds a whole default estate — system MCP servers, skills with
    scripts and assets, guardrails, OAuth clients, A2A keys — and not every
    path down that graph cascades (ten tenant-scoped tables have no CASCADE
    on tenant_id at all, and grandchild FKs vary). The result was a 500 on
    deleting any tenant created through the API.

    Instead of growing the list again, purge generically: every table
    carrying a tenant_id column, children before parents (reverse
    topological order), each table in its own SAVEPOINT. Tables that still
    fail — the model/provider/routing-group FK cycle makes a perfect order
    impossible — are retried on the next pass, which converges once their
    dependants are gone.
    """
    from app.database import Base
    from app.database.models.llm_model import LLModel

    # Break the one known FK cycle inside a tenant's data before ordering
    # matters: models point at routing groups and routing groups are
    # themselves models' parents.
    db.query(LLModel).filter(LLModel.tenant_id == tenant_id).update(
        {"routing_group_id": None}, synchronize_session=False
    )
    db.flush()

    keep = set(keep_tables)
    remaining = [
        t
        for t in reversed(Base.metadata.sorted_tables)
        if "tenant_id" in t.columns and t.name != "tenants" and t.name not in keep
    ]
    for _pass in range(5):
        blocked = []
        for tbl in remaining:
            try:
                with db.begin_nested():
                    db.execute(tbl.delete().where(tbl.c.tenant_id == tenant_id))
            except sa_exc.IntegrityError:
                blocked.append(tbl)
        if not blocked:
            break
        remaining = blocked
    else:
        raise RuntimeError(
            "tenant purge did not converge; still blocked: " + ", ".join(t.name for t in remaining)
        )
