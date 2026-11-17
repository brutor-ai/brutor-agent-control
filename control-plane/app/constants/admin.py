# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Administrator types
class AdminType(str, Enum):
    SYSTEM_ADMIN_TYPE = "system"
    TENANT_ADMIN_TYPE = "tenant"
