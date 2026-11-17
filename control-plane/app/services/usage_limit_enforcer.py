# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Usage Limit Enforcer

Cache invalidation hooks for usage limits.
Enforcement is handled by the Rust proxy, and so is the resolution of usage
alerts whose period has ended (`services::usage::resolve_expired_alerts` in
brutor-gateway-core). This service manages cache invalidation, called when
an admin changes limits.
"""

from app.core.logging_system import BrutorLogger


class UsageLimitEnforcer:
    """
    Manages usage limit cache invalidation.

    Enforcement (pre-request quota checks) is handled by the Rust proxy.
    This class provides cache invalidation hooks for admin config changes.
    """

    def __init__(
        self,
        logger: BrutorLogger,
        cache_ttl_seconds: float = 30.0,
        cache_max_size: int = 500
    ):
        self.logger = logger

    # =========================================================================
    # CACHE MANAGEMENT
    # =========================================================================

    def invalidate_group(self, tenant_id: str, group_id: str):
        """Invalidate cache for a specific group (call after limit config changes)."""
        # Cache is now managed by the Rust proxy; this is a no-op placeholder
        # kept for API compatibility with resource_groups admin endpoint.
        pass

    def invalidate_tenant(self, tenant_id: str):
        """Invalidate all cached quota for a tenant."""
        pass

    def clear_cache(self):
        """Clear entire quota cache."""
        pass
