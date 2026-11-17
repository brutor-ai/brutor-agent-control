# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

# Resource group names
class ResourceGroupName(str, Enum):
    DEFAULT = "Default"
    # Platform-internal calls made with the platform service key (model
    # health checks, the assurance judge, knowledge-base embeddings, the KB
    # uploader) are billed here. The core refuses any other call that is
    # not bound to a resource group. Core twin: `constants::groups::UNBOUND_GROUP_NAME`.
    UNBOUND = "Unbound"
