# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
W3C ``traceparent`` header generation.

The header format, per https://www.w3.org/TR/trace-context/#traceparent-header :

    traceparent = 00 - <trace-id-32-hex> - <span-id-16-hex> - <flags-2-hex>

We use:

* version ``00`` (only one defined today).
* a fresh 16-byte random ``trace-id``.
* a fresh 8-byte random ``span-id``.
* flags ``01`` ("sampled") so downstream services emit the trace.

The control plane injects this on outbound admin requests to the
proxy; the proxy's tower middleware (in
``brutor-gateway-core``) extracts it and links its OTel spans into
the same trace tree. Without that link, the
``_test_connection_via_proxy`` → ``_reset_proxy_resilience`` chain
shows up as two unrelated traces in Grafana — exactly the gap that
made the original reset-resilience hang harder to diagnose than it
needed to be.
"""

from __future__ import annotations

import secrets


def make_traceparent() -> str:
    """Return a fresh W3C ``traceparent`` value.

    Generates 16 random bytes for the trace-id and 8 for the span-
    id using ``secrets.token_hex`` (CSPRNG — adequate for trace
    uniqueness without a coordinator). Flags fixed at ``01`` so
    the proxy keeps the span (we always want admin traces sampled
    — they're rare, and operator-initiated, so volume isn't a
    concern).

    Returns
    -------
    str
        ``00-<32-hex>-<16-hex>-01`` — drop directly into a
        ``traceparent`` HTTP header.
    """
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-01"
