# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
There is no route that always says "no alerts".

`GET /v1/admin/metrics/alerts` answered an empty list whatever the
traffic was: nothing evaluated a threshold behind it. An answer of "no
active alerts" that no evaluation stands behind reads as an all-clear.
The alerts the product does raise are the usage alerts
(`/v1/admin/resource-groups/usage-alerts`) and the system alerts.
"""

from tests.app_factory import create_app


def test_the_metrics_alerts_route_is_gone():
    paths = create_app().openapi()["paths"]
    assert "/v1/admin/metrics/alerts" not in paths
    assert "/v1/admin/metrics/summary" in paths or any(p.startswith("/v1/admin/metrics/") for p in paths)
