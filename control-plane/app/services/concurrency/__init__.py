# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Concurrency primitives for control-plane bulk operations.

The single export today is :class:`BulkRunner`, a small shared helper
that bounds parallel fan-out from admin endpoints (bulk health
check, bulk model probe, future "test all servers" flows) so they
don't saturate the proxy's DB pool.
"""

from app.services.concurrency.bulk_runner import (
    BulkRunner,
    BulkRunResult,
    DEFAULT_POOL_RESERVE,
    derive_concurrency_cap,
)

__all__ = [
    "BulkRunner",
    "BulkRunResult",
    "DEFAULT_POOL_RESERVE",
    "derive_concurrency_cap",
]
