# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Observability primitives for cross-process trace propagation.

Today's single export is :func:`make_traceparent`, a tiny helper for
generating a W3C-spec traceparent header so the control plane's
admin requests are linkable to the proxy-side trace they trigger.
The reset-resilience hang we hit during operations work was harder
to diagnose than necessary because the control plane's "test
connection" trace and the proxy's "/v1/proxy/llm/admin/{id}/...
reset-resilience" trace had separate trace IDs — this closes
that gap.
"""

from app.services.observability.traceparent import make_traceparent

__all__ = ["make_traceparent"]
