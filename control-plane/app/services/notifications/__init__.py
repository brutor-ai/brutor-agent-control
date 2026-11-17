# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Notification helpers shared across admin endpoints.

The portal `notifications` table stores per-recipient inbox entries
(see `app/database/models/notification.py`). Most originating events
(group membership change, resource binding change, tool approval)
need to fan out one row per affected user — this package centralises
those fan-out helpers so each endpoint stays a thin call.
"""

from app.services.notifications.group_resources import (
    notify_group_members_about_resource_change,
)

__all__ = ["notify_group_members_about_resource_change"]
