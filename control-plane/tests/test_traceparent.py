# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for :func:`make_traceparent`.

Pin three things:

1. The output matches the W3C spec format exactly. The proxy's
   downstream handler may reject malformed values silently; a
   regression here would manifest as "no trace correlation" rather
   than a noisy failure, so we test it explicitly.
2. Each call returns a distinct trace-id (the whole point — admin
   probes shouldn't collide into one trace tree).
3. The flags byte is fixed at ``01`` (sampled). Admin calls are
   low-volume; we always want them in the trace.
"""

import re

from app.services.observability import make_traceparent


# Spec format: 00-{32 hex}-{16 hex}-{2 hex}
_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def test_format_matches_w3c_spec():
    tp = make_traceparent()
    assert _TRACEPARENT_RE.match(tp), f"malformed traceparent: {tp!r}"


def test_each_call_returns_distinct_trace_id():
    # Across 100 calls the trace-id collision probability is
    # negligible (2^-128 birthday space). If two ever match,
    # something is non-random.
    seen = {make_traceparent().split("-")[1] for _ in range(100)}
    assert len(seen) == 100


def test_flags_are_sampled():
    # We always sample admin traces — volume is low and the value
    # of having them in Grafana is high.
    tp = make_traceparent()
    assert tp.endswith("-01"), f"expected sampled flag (01), got: {tp!r}"


def test_version_is_zero_zero():
    # Version 00 is the only currently-defined W3C version. We
    # explicitly pin it rather than letting a future helper change
    # silently — a version bump must be a deliberate decision.
    tp = make_traceparent()
    assert tp.startswith("00-")
