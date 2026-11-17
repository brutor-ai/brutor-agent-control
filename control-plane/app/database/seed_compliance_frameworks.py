# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Seed: the compliance framework registry

Reference data owned by code, upserted on every boot — same discipline as
the ISO 42001 control catalog. Migration `a177_evidence_records` inserts the identical rows
for upgraded installs; a fresh install is `create_all` + stamp and never
runs that migration, so this is the only path that populates it there.

Idempotent: missing rows are inserted, existing rows have their
code-owned fields brought current, and nothing operator-facing is touched
(tenant enablement lives on the tenant, not here).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.database.models.compliance_framework import (
    CATALOG_VERSION,
    FRAMEWORK_SEED,
    SEED_FIELDS,
    ComplianceFramework,
)

logger = logging.getLogger(__name__)


def _from_catalog(spec: dict) -> dict:
    """The seed row with the fields the obligation catalog owns taken from it.

    Title, reference and verification are the catalog's: a second copy in
    the seed would drift from it. A framework counts as verified here only
    when the catalog is verified against a FINAL instrument, the same rule as `compliance.offering`.
    """
    from app.services.compliance import offering

    catalog = offering.any_catalog(spec["id"])
    if catalog is None:
        return spec
    return {
        **spec,
        "title": catalog.title,
        "reference": catalog.reference,
        "verified_against_instrument": offering.is_offered(catalog),
    }


def seed_compliance_frameworks(db: Session) -> int:
    """Upsert the registry rows. Returns the number created or updated."""
    changed = 0
    for spec in (_from_catalog(s) for s in FRAMEWORK_SEED):
        row = db.get(ComplianceFramework, spec["id"])
        if row is None:
            db.add(
                ComplianceFramework(
                    id=spec["id"],
                    catalog_version=CATALOG_VERSION,
                    **{k: spec[k] for k in SEED_FIELDS},
                )
            )
            changed += 1
            continue
        dirty = False
        for k in SEED_FIELDS:
            if getattr(row, k) != spec[k]:
                setattr(row, k, spec[k])
                dirty = True
        if row.catalog_version != CATALOG_VERSION:
            row.catalog_version = CATALOG_VERSION
            dirty = True
        if dirty:
            changed += 1
    if changed:
        # Flush here rather than rely on the session's autoflush: a second
        # call in the same transaction must find the rows it just added,
        # or it adds them again.
        db.flush()
        logger.info("Seeded/updated %d compliance framework registry rows", changed)
    return changed


__all__ = ["seed_compliance_frameworks"]
