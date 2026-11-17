# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Server status types
class ServerStatusType(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    # Restricted-tier models (OpenAI Codex preview, Pro tier, o1-pro)
    # need a tier-eligible API key. When the configured key isn't
    # eligible, the health probe is rejected by the provider — but
    # this isn't a failure of *our* config, so we surface it as a
    # distinct status so dashboards can show "Requires Pro tier"
    # instead of red "Unhealthy".
    REQUIRES_TIER = "requires_tier"