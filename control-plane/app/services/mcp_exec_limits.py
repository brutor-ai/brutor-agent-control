# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The execution limits of an MCP call as the gateway reads them, and the rule
a stated value is held to at save.

Three documents state them: a resource group's `mcp_global_limits`, a server
binding's `limits` (`resource_group_server_configs.limits`) and an MCP
server's `tenant_limits`. The gateway (`brutor-gateway-core`,
`src/services/mcp_limits.rs`) reads four of their fields and refuses EVERY
call of the group to the server when a stated one is not a positive number
(a concurrency: not a positive whole number). So such a value is refused when
it is saved, wherever these documents are written (the admin API, a
Policy-as-Code bundle); `null` is unset. The descriptions here are the ones
the API description and the bundle schema give.
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional, Tuple

POSITIVE_NUMBER = "a positive number"
POSITIVE_WHOLE_NUMBER = "a positive whole number"

_ONLY_CALLS = (
    "Only `tools/call` counts: protocol traffic (initialize, tools/list, ...) and the "
    "listener stream do not."
)
_PER_DESCENDANT = (
    "Stated on an ancestor group, the value applies to each descendant group separately: "
    "every group gets the whole allowance, not a share of it."
)
_UNSET = "Null leaves it unset."

TIMEOUT_DESCRIPTION = (
    "Seconds one MCP call may take in total, a streamed call included (retries and the "
    "whole answer count). It also caps the wait for the answer to start, together with "
    f"the server's own timeout (the lower applies). {POSITIVE_NUMBER.capitalize()}. {_UNSET}"
)
PER_USER_DESCRIPTION = (
    "Calls of one identity in flight at once. The identity is the agent; else the user, "
    "across all of the user's keys and sessions; else the identity provider's subject; "
    f"else the key of a group-scoped key. {_ONLY_CALLS} {_PER_DESCENDANT} "
    f"{POSITIVE_WHOLE_NUMBER.capitalize()}. {_UNSET}"
)
PER_GROUP_DESCRIPTION = (
    "Calls of the group in flight at once, across every gateway instance. "
    f"{_ONLY_CALLS} {_PER_DESCENDANT} {POSITIVE_WHOLE_NUMBER.capitalize()}. {_UNSET}"
)
PAYLOAD_DESCRIPTION = (
    "Megabytes (MiB) the request and the answer of one MCP call may each carry. The "
    "request is never allowed above 10 MiB, whatever is set; answers are not capped "
    f"while this is unset. {POSITIVE_NUMBER.capitalize()}. {_UNSET}"
)

#: (section, field, whole numbers only, description)
FIELDS: Tuple[Tuple[str, str, bool, str], ...] = (
    ("execution", "timeout_seconds", False, TIMEOUT_DESCRIPTION),
    ("execution", "max_concurrency_per_user", True, PER_USER_DESCRIPTION),
    ("execution", "max_concurrency_per_group", True, PER_GROUP_DESCRIPTION),
    ("data", "max_payload_size_mb", False, PAYLOAD_DESCRIPTION),
)

DOCUMENT_DESCRIPTION = (
    "MCP limits. `execution` and `data` hold the four execution limits the gateway "
    "enforces on a call; `frequency` and `per_tool` hold its rate limits. Where a group "
    "(with its ancestors), the server's tenant-wide limits and the group's binding of "
    "the server state the same limit, the most restrictive applies. A stated execution "
    "limit that is not a positive number (a concurrency: a positive whole number) is "
    "refused at save: the gateway would refuse every call of the group to the server."
)

MAX_CONCURRENT_REQUESTS_DESCRIPTION = (
    "Requests to this server in flight at once on one gateway instance (every MCP "
    "request counts; a listener stream gives its place back once its answer starts). "
    "Each instance of a cluster allows this many; a ceiling across instances is a "
    "group's `execution.max_concurrency_per_group`. A positive whole number; null is "
    "the default of 10."
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _readable(value: Any, whole: bool) -> bool:
    if not _is_number(value):
        return False
    try:
        as_float = float(value)
    except OverflowError:
        return False
    if not math.isfinite(as_float) or as_float <= 0:
        return False
    return not whole or as_float.is_integer()


def _shown(value: Any) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def value_problem(name: str, value: Any, whole: bool) -> Optional[str]:
    """The sentence refusing `value` of the limit `name`, or None when the
    gateway reads it (None itself is unset)."""
    if value is None or _readable(value, whole):
        return None
    rule = POSITIVE_WHOLE_NUMBER if whole else POSITIVE_NUMBER
    return f"MCP limit {name} must be {rule}, or null to leave it unset; {_shown(value)} is not."


def problem(limits: Any) -> Optional[str]:
    """The first execution limit of an MCP limits document the gateway could
    not read, as a sentence; None when it reads them all."""
    if not isinstance(limits, dict):
        return None
    for section, field, whole, _ in FIELDS:
        block = limits.get(section)
        if block is None:
            continue
        if not isinstance(block, dict):
            return f"MCP limits section '{section}' must be an object, or null to leave it unset."
        found = value_problem(f"{section}.{field}", block.get(field), whole)
        if found:
            return found
    return None


def max_concurrent_requests_problem(value: Any) -> Optional[str]:
    if value is None or (_readable(value, True) and isinstance(value, int)):
        return None
    return (
        "max_concurrent_requests must be a positive whole number, or null for the default "
        f"of 10; {_shown(value)} is not."
    )
