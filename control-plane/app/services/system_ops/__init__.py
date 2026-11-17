# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Console services.

Helpers shared by every System endpoint:

* `audit_row` — write_system_operation() emits the audit log row that
  every system-page click must persist.
* `notify` — publish_system_notify() validates + emits one Postgres
  NOTIFY message on a registered channel. Subscribers (gateway-core
  and gateway-control) LISTEN; the dispatch propagates to every
  replica within milliseconds.
"""

from app.services.system_ops.audit_row import (
    write_system_operation,
    SENSITIVE_PAYLOAD_KEYS,
    redact_payload,
)
from app.services.system_ops.notify import (
    CHANNEL_CACHE_INVALIDATE,
    CHANNEL_COUNTERS_RESET,
    CHANNEL_LOG_LEVEL,
    CHANNEL_MAINTENANCE,
    CHANNEL_SCHEDULER_PAUSE,
    CHANNEL_SCHEDULER_RESUME,
    CHANNEL_SCHEDULER_TRIGGER,
    CHANNEL_SESSION_REVOKE,
    NotifyError,
    PAYLOAD_BYTE_LIMIT,
    PayloadTooLargeError,
    PayloadValidationError,
    UnknownChannelError,
    publish_system_notify,
    registered_channels,
)

__all__ = [
    # audit row
    "write_system_operation",
    "SENSITIVE_PAYLOAD_KEYS",
    "redact_payload",
    # notify
    "publish_system_notify",
    "registered_channels",
    "CHANNEL_COUNTERS_RESET",
    "CHANNEL_CACHE_INVALIDATE",
    "CHANNEL_SCHEDULER_TRIGGER",
    "CHANNEL_SCHEDULER_PAUSE",
    "CHANNEL_SCHEDULER_RESUME",
    "CHANNEL_SESSION_REVOKE",
    "CHANNEL_LOG_LEVEL",
    "CHANNEL_MAINTENANCE",
    "NotifyError",
    "UnknownChannelError",
    "PayloadValidationError",
    "PayloadTooLargeError",
    "PAYLOAD_BYTE_LIMIT",
]
