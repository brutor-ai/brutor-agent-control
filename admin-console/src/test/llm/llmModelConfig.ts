/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A stored LLM model, as the Control Plane answers it: every field the type
 * requires, with the values of an enabled OpenAI chat model that has not
 * been probed. A test names the fields it is about.
 */

import type { LLModelConfig } from '../../lib/llmTypes';

export function buildLLModelConfig(overrides: Partial<LLModelConfig> = {}): LLModelConfig {
    return {
        id: 'llm-1',
        tenant_id: 't1',
        name: 'Model',
        provider: 'openai',
        model_name: 'gpt-test',
        mode: 'chat',
        api_url_auto_discovered: true,
        auth_type: 'api_key',
        default_temperature: 0.7,
        default_max_tokens: 1024,
        supports_streaming: true,
        supports_function_calling: false,
        supports_vision: false,
        supports_batch: false,
        routing_weight: 1,
        timeout: 60,
        enabled: true,
        health_status: { status: 'unknown', consecutive_failures: 0 },
        performance_metrics: {
            total_requests: 0,
            failed_requests: 0,
            success_rate: 0,
            total_tokens_consumed: 0,
        },
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-01T00:00:00Z',
        ...overrides,
    };
}
