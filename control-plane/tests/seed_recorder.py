# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Recorder: what the install seed and tenant provisioning write.

`record()` runs, on an empty in-memory SQLite database holding every model's
table, what a starting control plane and a tenant creation run:

1. `install`: `app.database.seed_initial_data()` on the empty database (the
   first start: the `SEEDING` registry, which provisions the default tenant
   through the `PROVISIONING` registry);
2. `second_start`: `seed_initial_data()` again (a restart);
3. `second_tenant`: `TenantProvisioner.provision_tenant()` for a new tenant.

and returns, for each phase, two forms of what was written:

* **order-sensitive**: the write statements in the order they were issued,
  as `[verb, table, statements]`, consecutive equal `[verb, table]` merged
  (a statement may carry many rows; the rows are in the other form). This
  is the form that shows a step moved.
* **order-insensitive**: the rows the phase added to each table and the rows
  it removed (a changed row is one removed and one added), sorted, with what
  differs from run to run masked (see `_mask`): generated identifiers,
  timestamps, password hashes, ciphertext, key material. A long text (a
  skill's script, a catalog description) is recorded as its length and
  digest.

The composition recorded is the one the process is in when `record()` is
called: the caller asks for the Community composition around it.

The database is private to the call (`app.database.SessionLocal` is pointed
at it for the duration and put back).
"""

from __future__ import annotations

import collections
import datetime as _dt
import decimal
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List
from unittest import mock

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401  (registers the SQLite JSONB shims)

SECOND_TENANT = "seed-recorder-tenant"

# Environment the seed reads. Pinned, so that a developer's shell does not
# change the recording.
_SEED_ENV = {
    "BRUTOR_SYSTEM_ADMIN_USERNAME": "sysadmin",
    "BRUTOR_SYSTEM_ADMIN_PASSWORD": "suite-system-admin-password",
    "BRUTOR_SYSTEM_ADMIN_EMAIL": "sysadmin@system.local",
    "BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME": "admin",
    "BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD": "suite-tenant-admin-password",
    "BRUTOR_DEFAULT_TENANT_ADMIN_EMAIL": "admin@example.com",
    # The trial portal user is made only when its password is configured.
    "BRUTOR_TRIAL_USER_USERNAME": "trial",
    "BRUTOR_TRIAL_USER_PASSWORD": "Trial123!",
}

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_BCRYPT = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}")
_FERNET = re.compile(r"gAAAAA[A-Za-z0-9_\-]{20,}={0,2}")
_HEX = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{10,}(?![0-9a-zA-Z])")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:?\d\d)?")
_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)
_ULID = re.compile(r"(?<![0-9A-Za-z])[0-9A-HJKMNP-TV-Z]{26}(?![0-9A-Za-z])")

# A text longer than this is recorded as its length and digest.
LONG_TEXT = 200


def _mask_text(value: str) -> str:
    value = _PEM.sub("<pem>", value)
    value = _BCRYPT.sub("<bcrypt>", value)
    value = _FERNET.sub("<fernet>", value)
    value = _UUID.sub("<uuid>", value)
    value = _ULID.sub("<ulid>", value)
    value = _TIMESTAMP.sub("<timestamp>", value)
    value = _HEX.sub("<hex>", value)
    if len(value) > LONG_TEXT:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<text length={len(value)} sha256={digest}>"
    return value


def _mask(value: Any) -> Any:
    """`value` with everything that differs between two runs replaced."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{" and stripped[-1:] in "]}":
            try:
                return _mask(json.loads(value))
            except ValueError:
                pass
        return _mask_text(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return "<timestamp>"
    if isinstance(value, dict):
        return {str(k): _mask(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_mask(v) for v in value]
    if isinstance(value, (bytes, memoryview)):
        return "<bytes>"
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return "<uuid>"
    return value


# Columns whose value is random or a secret and has no shape `_mask` knows:
# the value is replaced whole (a NULL stays NULL, so "was it set" is kept).
_OPAQUE_COLUMNS = {
    "tenant_a2a_keys": {"private_key_encrypted", "public_key"},
    "api_keys": {"key_prefix"},
}


def _rows(conn, table) -> List[Dict[str, Any]]:
    opaque = _OPAQUE_COLUMNS.get(table.name, set())
    rows = []
    for row in conn.execute(table.select()).mappings():
        rows.append(
            {
                column: ("<opaque>" if column in opaque and value is not None else _mask(value))
                for column, value in sorted(row.items())
            }
        )
    return rows


def _tables(engine) -> Dict[str, List[str]]:
    """Every table that holds a row: its rows, masked, each as one line of
    JSON, sorted."""
    from app.database import Base

    found: Dict[str, List[str]] = {}
    with engine.connect() as conn:
        for name in sorted(Base.metadata.tables):
            rows = [json.dumps(row, sort_keys=True, default=str) for row in _rows(conn, Base.metadata.tables[name])]
            if rows:
                found[name] = sorted(rows)
    return found


def _difference(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> Dict[str, Dict[str, List[str]]]:
    """Per table, the rows `after` has and `before` has not (`added`), and the
    other way round (`removed`), as multisets. Tables with neither are left
    out."""
    changed: Dict[str, Dict[str, List[str]]] = {}
    for name in sorted(set(before) | set(after)):
        was, now = collections.Counter(before.get(name, [])), collections.Counter(after.get(name, []))
        added, removed = sorted((now - was).elements()), sorted((was - now).elements())
        if added or removed:
            changed[name] = {"added": added, "removed": removed}
    return changed


def _statement(statement: str):
    words = statement.split(None, 3)
    verb = words[0].upper() if words else ""
    if verb == "INSERT" and len(words) > 2:
        return verb, words[2].strip('"').split("(")[0]
    if verb == "DELETE" and len(words) > 2:
        return verb, words[2].strip('"')
    if verb == "UPDATE" and len(words) > 1:
        return verb, words[1].strip('"')
    return None


class _Writes:
    """The write statements an engine was sent, merged while consecutive."""

    def __init__(self, engine) -> None:
        self.log: List[List[Any]] = []
        event.listen(engine, "after_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany) -> None:
        found = _statement(statement)
        if found is None:
            return
        if self.log and self.log[-1][:2] == list(found):
            self.log[-1][2] += 1
        else:
            self.log.append([found[0], found[1], 1])

    def take(self) -> List[List[Any]]:
        taken, self.log = self.log, []
        return taken


@contextmanager
def _private_database() -> Iterator[Any]:
    import app.database as database
    import app.database.models  # noqa: F401  (registers every model)
    from app.database import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    env = {**_SEED_ENV}
    with mock.patch.object(database, "SessionLocal", factory), mock.patch.dict(os.environ, env):
        try:
            yield engine, factory
        finally:
            engine.dispose()


def _provision_second_tenant(factory) -> None:
    from app.core.logging_system import BrutorLogger
    from app.database.models.tenant import Tenant
    from app.services.tenant.tenant_provisioning import TenantProvisioner

    db = factory()
    try:
        tenant = Tenant(
            id=SECOND_TENANT,
            name=SECOND_TENANT,
            display_name="Seed Recorder",
            organization_name="Seed Recorder",
            primary_contact_email="admin@seed-recorder.example",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
            features={},
        )
        db.add(tenant)
        db.flush()
        TenantProvisioner(logger=BrutorLogger("seed-recorder")).provision_tenant(
            db=db,
            tenant=tenant,
            admin_username="recorder-admin",
            admin_password="Recorder123!",
            admin_email="admin@seed-recorder.example",
            admin_full_name="Seed Recorder Administrator",
            seed_llms=True,
        )
        db.commit()
    finally:
        db.close()


def record() -> Dict[str, Any]:
    """Run the three phases in the composition the process is in."""
    import app.database as database

    recording: Dict[str, Any] = {}
    with _private_database() as (engine, factory):
        writes = _Writes(engine)

        state: Dict[str, List[str]] = {}
        for phase, run in (
            ("install", database.seed_initial_data),
            ("second_start", database.seed_initial_data),
            ("second_tenant", lambda: _provision_second_tenant(factory)),
        ):
            run()
            after = _tables(engine)
            recording[phase] = {"writes": writes.take(), "rows": _difference(state, after)}
            state = after
    return recording


def canonical(recording: Dict[str, Any]) -> str:
    """The recording as text: one write statement and one row per line, so
    that a difference reads as the rows and statements that differ."""
    lines = ["{"]
    phases = list(recording)
    for p, phase in enumerate(phases):
        body = recording[phase]
        lines.append(f" {json.dumps(phase)}: {{")
        lines.append('  "writes": [')
        lines += [
            f"   {json.dumps(w)}{',' if i < len(body['writes']) - 1 else ''}" for i, w in enumerate(body["writes"])
        ]
        lines.append("  ],")
        lines.append('  "rows": {')
        tables = list(body["rows"])
        for t, table in enumerate(tables):
            lines.append(f"   {json.dumps(table)}: {{")
            for k, kind in enumerate(("added", "removed")):
                rows = body["rows"][table][kind]
                lines.append(f'    "{kind}": [')
                # A row is already one line of JSON.
                lines += [f"     {r}{',' if i < len(rows) - 1 else ''}" for i, r in enumerate(rows)]
                lines.append(f"    ]{',' if k == 0 else ''}")
            lines.append(f"   }}{',' if t < len(tables) - 1 else ''}")
        lines.append("  }")
        lines.append(f" }}{',' if p < len(phases) - 1 else ''}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def summary(recording: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Per phase: rows added per table (for a readable assertion message)."""
    return {
        phase: {table: len(change["added"]) - len(change["removed"]) for table, change in body["rows"].items()}
        for phase, body in recording.items()
    }


if __name__ == "__main__":
    # python tests/seed_recorder.py enterprise|community <output file>
    # A process of its own: the registries are process-wide, and a recording
    # must not depend on what else ran in the process.
    import sys

    composition, output = sys.argv[1], sys.argv[2]
    if composition == "community":
        from tests.community_process import become_community_process

        become_community_process()
    elif composition == "enterprise":
        from tests.app_factory import become_enterprise_process

        become_enterprise_process()
    else:
        raise SystemExit(f"unknown composition: {composition}")
    from app.composition.edition import composed_edition

    assert composed_edition() == composition, composed_edition()
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(canonical(record()))
    print("RECORDED", composition)
