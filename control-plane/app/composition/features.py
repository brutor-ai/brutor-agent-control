# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The names of the Enterprise Edition features.

Every registry of the composition layer (routers, startup steps, seed and
provisioning steps, permissions) tags what it holds with one of these names,
or with nothing for the Community Edition. The names are the keys of
`enterprise_features` in the edition boundary file (`editions.yaml`).

This module imports nothing, so that anything may import it.
"""

DEPLOYMENT = "deployment"
TRAFFIC_IMPORT = "traffic_import"
RESPONSE_CACHE = "response_cache"
PROMPT_CACHE = "prompt_cache"
MCP_REGISTRY = "mcp_registry"
KNOWLEDGE_BASE = "knowledge_base"
POLICY_AS_CODE = "policy_as_code"
BATCH_JOBS = "batch_jobs"
MULTI_TENANT = "multi_tenant"
PORTAL = "portal"
DISCOVERY = "discovery"

ENTERPRISE_FEATURES = frozenset(
    {
        DEPLOYMENT,
        TRAFFIC_IMPORT,
        RESPONSE_CACHE,
        PROMPT_CACHE,
        MCP_REGISTRY,
        KNOWLEDGE_BASE,
        POLICY_AS_CODE,
        BATCH_JOBS,
        MULTI_TENANT,
        PORTAL,
        DISCOVERY,
    }
)
