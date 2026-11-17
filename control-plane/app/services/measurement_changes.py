# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Applying platform measurement changes

Called once per control-plane start, after migrations and seeding, inside
the init advisory lock (`app/database/__init__.py::init_database`). For each
registry entry (`app/constants/measurement_changes.py`) not yet recorded in
`platform_measurement_changes`:

* every (tenant, AI System, metric, basis) that has a baseline for one of the
  entry's metrics on one of its bases gets a `measurement_change` baseline
  epoch (caller `all`) starting NOW — the moment the change first applied on
  this installation; samples before it no longer describe normal, so the
  shift the new measurement produces cannot fire as drift;
* the entry is recorded with the epochs it wrote, in the same transaction,
  so it never applies again. A fresh installation (no baselines) records it
  with zero epochs: its baselines are learned on the new measurement.

Idempotent and safe under concurrency: the entry's row is its claim (primary
key), and a failure rolls the whole entry back so the next start retries it.
Never raises into startup — a failed application is logged and retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.constants.drift_resolution import EPOCH_ALL_CALLERS, KIND_MEASUREMENT_CHANGE
from app.constants.measurement_changes import (
    DECLARED_BY_PLATFORM,
    REGISTRY,
    MeasurementChange,
)
from app.env_vars import BRUTOR_VERSION

log = logging.getLogger(__name__)


@dataclass
class Applied:
    id: str
    epochs_written: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def apply_pending(
    db: Session,
    *,
    registry: Iterable[MeasurementChange] = REGISTRY,
    now: Optional[datetime] = None,
    platform_version: Optional[str] = None,
) -> List[Applied]:
    """Apply every registry entry this installation has not applied yet.
    Commits per entry. Returns what it applied (empty when nothing was
    pending)."""
    from app.database.models.ai_system_drift import AISystemBaseline
    from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
    from app.database.models.platform_measurement_change import PlatformMeasurementChange

    version = platform_version if platform_version is not None else BRUTOR_VERSION.require()
    done: List[Applied] = []
    for entry in registry:
        if db.get(PlatformMeasurementChange, entry.id) is not None:
            continue
        at = now or _now()
        try:
            targets = (
                db.query(
                    AISystemBaseline.tenant_id,
                    AISystemBaseline.ai_system_id,
                    AISystemBaseline.metric_key,
                    AISystemBaseline.basis,
                )
                .filter(
                    AISystemBaseline.metric_key.in_(entry.metric_keys),
                    AISystemBaseline.basis.in_(entry.bases),
                )
                .distinct()
                .order_by(
                    AISystemBaseline.tenant_id,
                    AISystemBaseline.ai_system_id,
                    AISystemBaseline.metric_key,
                    AISystemBaseline.basis,
                )
                .all()
            )
            written = 0
            for tenant_id, ai_system_id, metric_key, basis in targets:
                db.add(
                    AISystemBaselineEpoch(
                        id=f"epoch-{ULID()}",
                        tenant_id=tenant_id,
                        ai_system_id=ai_system_id,
                        metric_key=metric_key,
                        basis=basis,
                        caller_system_id=EPOCH_ALL_CALLERS,
                        starts_at=at,
                        kind=KIND_MEASUREMENT_CHANGE,
                        measurement_change_id=entry.id,
                        reason=entry.reason,
                        declared_by=DECLARED_BY_PLATFORM,
                        declared_at=at,
                    )
                )
                written += 1
            db.add(
                PlatformMeasurementChange(
                    id=entry.id,
                    metric_keys=list(entry.metric_keys),
                    bases=list(entry.bases),
                    reason=entry.reason,
                    introduced_in_version=entry.introduced_in_version,
                    platform_version=version,
                    applied_at=at,
                    epochs_written=written,
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001 — never fail startup; the next start retries
            db.rollback()
            log.exception("measurement change %s could not be applied; will retry", entry.id)
            continue
        log.info(
            "measurement change %s applied: %d baseline epoch(s) written", entry.id, written
        )
        done.append(Applied(id=entry.id, epochs_written=written))
    return done


def apply_pending_at_startup() -> None:
    """`init_database()` hook: own session, never raises."""
    from app.database import SessionLocal

    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001
        log.exception("measurement changes: no session")
        return
    try:
        apply_pending(db)
    except Exception:  # noqa: BLE001
        log.exception("measurement changes: application failed; will retry at next start")
    finally:
        db.close()


__all__ = ["Applied", "apply_pending", "apply_pending_at_startup"]
