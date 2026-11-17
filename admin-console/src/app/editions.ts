/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Enterprise Edition features a screen, a navigation entry or a slot item
 * can belong to.
 *
 * Something without a feature belongs to the Community Edition.
 */
export type EnterpriseFeature =
    | 'deployment'
    | 'traffic_import'
    | 'response_cache'
    | 'prompt_cache'
    | 'mcp_registry'
    | 'knowledge_base'
    | 'policy_as_code'
    | 'batch_jobs'
    | 'multi_tenant'
    | 'portal'
    | 'discovery';
