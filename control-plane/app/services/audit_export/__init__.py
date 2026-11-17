# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Scheduled audit-export pipeline — re-exports `proxy_logs` to operator-
configured S3 / webhook sinks on a fixed interval. See:

  * `app/database/models/audit_export_destination.py` — schema + design
  * `app/services/audit_export/service.py`           — single-shot run
  * `app/services/audit_export/backends.py`          — S3 + webhook
  * `app/services/audit_export/scheduler.py`         — background loop

Public surface (what other modules should import):
"""

from app.services.audit_export.service import (
    AuditExportService,
    RunOutcome,
    RunResult,
)
from app.services.audit_export.scheduler import (
    audit_export_scheduler_loop,
    start_audit_export_scheduler,
)

__all__ = [
    "AuditExportService",
    "RunOutcome",
    "RunResult",
    "audit_export_scheduler_loop",
    "start_audit_export_scheduler",
]
