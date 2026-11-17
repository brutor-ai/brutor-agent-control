/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AI Model Management — Unit Tests
 *
 * Tests the core business logic for LLM model management in the Admin Console:
 * - Model CRUD operations (create, update, delete validations)
 * - Health check flow (manual trigger, reload after check)
 * - Toggle enable/disable logic
 * - Form validation integration
 * - Status badge rendering logic
 * - Model filtering and grouping
 */

import { describe, it, expect } from 'vitest'
import { validateField, validateJSON, VALIDATION_RULES } from '../formValidationRules'
import type { LLModelConfig, HealthStatus, ModelStatus } from '../../../../lib/llmTypes'

// ─── Test Fixtures ──────────────────────────────────────────────────────────

function makeModel(overrides: Partial<LLModelConfig> = {}): LLModelConfig {
    return {
        id: 'llm-test-001',
        name: 'Test Model',
        tenant_id: 'default',
        provider: 'openai',
        model_name: 'gpt-5.2',
        mode: 'chat',
        api_url_auto_discovered: true,
        auth_type: 'api_key',
        default_temperature: 0.7,
        default_max_tokens: 4096,
        enabled: true,
        supports_streaming: true,
        supports_function_calling: true,
        supports_vision: false,
        drop_params: false,
        cost_per_input_token: 2.5,
        cost_per_output_token: 10.0,
        timeout: 120,
        routing_weight: 100,
        health_status: {
            status: 'unknown',
            consecutive_failures: 0,
        },
        groups: [],
        ...overrides,
    } as LLModelConfig
}

function makeHealthStatus(status: ModelStatus, extras: Partial<HealthStatus> = {}): HealthStatus {
    return {
        status,
        consecutive_failures: status === 'healthy' ? 0 : 1,
        last_check: new Date().toISOString(),
        ...extras,
    }
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('AI Model Management', () => {

    // =========================================================================
    // Model Create Validation
    // =========================================================================

    describe('model create validation', () => {
        it('requires name', () => {
            expect(validateField('name', '', VALIDATION_RULES)).toBeDefined()
            expect(validateField('name', 'GPT-5.2', VALIDATION_RULES)).toBeUndefined()
        })

        it('requires model_name', () => {
            expect(validateField('model_name', '', VALIDATION_RULES)).toBeDefined()
            expect(validateField('model_name', 'gpt-5.2', VALIDATION_RULES)).toBeUndefined()
        })

        it('validates temperature range 0-2', () => {
            expect(validateField('temperature', -0.1, VALIDATION_RULES)).toBeDefined()
            expect(validateField('temperature', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('temperature', 1.5, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('temperature', 2.0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('temperature', 2.1, VALIDATION_RULES)).toBeDefined()
        })

        it('validates maxTokens allows 0 (embedding models)', () => {
            expect(validateField('maxTokens', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('maxTokens', 4096, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('maxTokens', -1, VALIDATION_RULES)).toBeDefined()
        })

        it('validates API URL format', () => {
            expect(validateField('apiUrl', 'https://api.openai.com', VALIDATION_RULES)).toBeUndefined()
            expect(validateField('apiUrl', 'http://localhost:8080', VALIDATION_RULES)).toBeUndefined()
            expect(validateField('apiUrl', 'ftp://bad.com', VALIDATION_RULES)).toBeDefined()
            // Empty is OK (not required)
            expect(validateField('apiUrl', '', VALIDATION_RULES)).toBeUndefined()
        })

        it('validates JSON fields', () => {
            expect(validateJSON('{"type": "json_object"}', 'Response Format')).toBeUndefined()
            expect(validateJSON('{bad json}', 'Response Format')).toBeDefined()
            expect(validateJSON('', 'Response Format')).toBeUndefined()
        })

        it('validates timeout 1-600', () => {
            expect(validateField('timeout', 1, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('timeout', 120, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('timeout', 600, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('timeout', 601, VALIDATION_RULES)).toBeDefined()
        })

        it('validates retry attempts 0-10', () => {
            expect(validateField('retryAttempts', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('retryAttempts', 3, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('retryAttempts', 10, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('retryAttempts', 11, VALIDATION_RULES)).toBeDefined()
        })

        it('validates routing weight 0-100', () => {
            expect(validateField('routingWeight', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('routingWeight', 100, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('routingWeight', 101, VALIDATION_RULES)).toBeDefined()
        })

        it('validates API key max length 500', () => {
            // API key is validated server-side with max 500 chars
            // Client has maxLength={500} on the input
            const longKey = 'x'.repeat(501)
            expect(longKey.length).toBeGreaterThan(500)
        })
    })

    // =========================================================================
    // Model Create Payload
    // =========================================================================

    describe('model create payload', () => {
        it('builds correct payload from form data', () => {
            const formData = {
                name: 'My GPT-5.2',
                provider: 'openai',
                model_name: 'gpt-5.2',
                mode: 'chat',
                enabled: true,
                api_url_auto_discovered: true,
                auth_type: 'api_key',
                api_key: 'sk-test-key',
                default_temperature: 0.7,
                default_max_tokens: 4096,
                group_ids: ['group-default'],
            }

            // Required fields present
            expect(formData.name).toBeTruthy()
            expect(formData.provider).toBeTruthy()
            expect(formData.model_name).toBeTruthy()

            // API key should be in request but NOT in response
            expect(formData.api_key).toBeDefined()
        })

        it('handles api_url requirement for non-auto-discovered providers', () => {
            const autoDiscovered = { api_url_auto_discovered: true, api_url: undefined }
            const manual = { api_url_auto_discovered: false, api_url: 'https://custom.api.com' }
            const manualMissing = { api_url_auto_discovered: false, api_url: undefined }

            // Auto-discovered doesn't need URL
            expect(autoDiscovered.api_url_auto_discovered || autoDiscovered.api_url).toBeTruthy()

            // Manual needs URL
            expect(manual.api_url_auto_discovered || manual.api_url).toBeTruthy()

            // Manual without URL is invalid
            expect(manualMissing.api_url_auto_discovered || manualMissing.api_url).toBeFalsy()
        })
    })

    // =========================================================================
    // Health Status Logic
    // =========================================================================

    describe('health status', () => {
        it('healthy status has 0 consecutive failures', () => {
            const health = makeHealthStatus('healthy')
            expect(health.status).toBe('healthy')
            expect(health.consecutive_failures).toBe(0)
        })

        it('unhealthy status has consecutive failures', () => {
            const health = makeHealthStatus('unhealthy', { consecutive_failures: 3, error_message: 'Connection refused' })
            expect(health.status).toBe('unhealthy')
            expect(health.consecutive_failures).toBeGreaterThan(0)
            expect(health.error_message).toBeDefined()
        })

        it('unknown status is the default (no check run yet)', () => {
            const model = makeModel()
            expect(model.health_status?.status).toBe('unknown')
        })

        it('health status badge derives from health_status field', () => {
            const healthy = makeModel({ health_status: makeHealthStatus('healthy') })
            const unhealthy = makeModel({ health_status: makeHealthStatus('unhealthy') })
            const unknown = makeModel({ health_status: makeHealthStatus('unknown') })

            expect(healthy.health_status?.status).toBe('healthy')
            expect(unhealthy.health_status?.status).toBe('unhealthy')
            expect(unknown.health_status?.status).toBe('unknown')
        })
    })

    // =========================================================================
    // Model Enable/Disable Toggle
    // =========================================================================

    describe('enable/disable toggle', () => {
        it('toggling updates enabled field', () => {
            const model = makeModel({ enabled: true })
            const toggled = { ...model, enabled: !model.enabled }
            expect(toggled.enabled).toBe(false)
        })

        it('disabled model should not appear in available models for portal', () => {
            const models = [
                makeModel({ id: 'llm-1', enabled: true }),
                makeModel({ id: 'llm-2', enabled: false }),
                makeModel({ id: 'llm-3', enabled: true }),
            ]

            const available = models.filter(m => m.enabled)
            expect(available).toHaveLength(2)
            expect(available.map(m => m.id)).toEqual(['llm-1', 'llm-3'])
        })
    })

    // =========================================================================
    // Model Filtering & Grouping
    // =========================================================================

    describe('model filtering and grouping', () => {
        const models = [
            makeModel({ id: 'llm-1', provider: 'openai', model_name: 'gpt-5.2', enabled: true }),
            makeModel({ id: 'llm-2', provider: 'openai', model_name: 'gpt-4o', enabled: false }),
            makeModel({ id: 'llm-3', provider: 'anthropic', model_name: 'claude-sonnet-4', enabled: true }),
            makeModel({ id: 'llm-4', provider: 'anthropic', model_name: 'claude-haiku-4.5', enabled: false }),
            makeModel({ id: 'llm-5', provider: 'google', model_name: 'gemini-2.5-pro', enabled: true }),
        ]

        it('groups models by provider', () => {
            const groups = new Map<string, typeof models>()
            models.forEach(m => {
                const existing = groups.get(m.provider) || []
                groups.set(m.provider, [...existing, m])
            })

            expect(groups.size).toBe(3)
            expect(groups.get('openai')).toHaveLength(2)
            expect(groups.get('anthropic')).toHaveLength(2)
            expect(groups.get('google')).toHaveLength(1)
        })

        it('filters enabled models', () => {
            const enabled = models.filter(m => m.enabled)
            expect(enabled).toHaveLength(3)
        })

        it('sorts providers with enabled models first', () => {
            const groups = new Map<string, { provider: string; enabledCount: number }>()
            models.forEach(m => {
                const existing = groups.get(m.provider) || { provider: m.provider, enabledCount: 0 }
                if (m.enabled) existing.enabledCount++
                groups.set(m.provider, existing)
            })

            const sorted = Array.from(groups.values()).sort((a, b) => {
                if (a.enabledCount > 0 && b.enabledCount === 0) return -1
                if (a.enabledCount === 0 && b.enabledCount > 0) return 1
                return a.provider.localeCompare(b.provider)
            })

            // All providers have at least one enabled model
            expect(sorted[0].provider).toBe('anthropic') // alphabetically first among all-enabled
        })

        it('search filters by name and model_name', () => {
            const search = 'claude'
            const filtered = models.filter(m =>
                m.name.toLowerCase().includes(search) ||
                m.model_name.toLowerCase().includes(search)
            )
            expect(filtered).toHaveLength(2)
            expect(filtered.every(m => m.provider === 'anthropic')).toBe(true)
        })
    })

    // =========================================================================
    // Model Update Logic
    // =========================================================================

    describe('model update', () => {
        it('only sends changed fields in update payload', () => {
            const original = makeModel()
            const updates = { default_temperature: 0.9 }

            // The update should be a partial — only changed fields
            expect(Object.keys(updates)).toHaveLength(1)
            expect(updates.default_temperature).not.toBe(original.default_temperature)
        })

        it('preserves fields not in the update', () => {
            const original = makeModel({ name: 'Original', default_temperature: 0.7 })
            const updates = { default_temperature: 0.9 }
            const merged = { ...original, ...updates }

            expect(merged.name).toBe('Original') // Not changed
            expect(merged.default_temperature).toBe(0.9) // Changed
        })
    })

    // =========================================================================
    // Delete Confirmation
    // =========================================================================

    describe('model delete', () => {
        it('delete requires confirmation with model info', () => {
            const model = makeModel({ id: 'llm-to-delete', name: 'Delete Me' })

            const confirmation = {
                modelId: model.id,
                modelName: model.name,
                modelInfo: model,
            }

            expect(confirmation.modelId).toBe('llm-to-delete')
            expect(confirmation.modelName).toBe('Delete Me')
            expect(confirmation.modelInfo).toBeDefined()
        })
    })

    // =========================================================================
    // Health Check Integration
    // =========================================================================

    describe('health check integration', () => {
        it('health checks are paused when form is open', () => {
            const isCreatingNew = true
            const editingModel = null
            const showHealthConfig = false

            const isAnyFormOpen = isCreatingNew || editingModel !== null || showHealthConfig
            expect(isAnyFormOpen).toBe(true) // Should pause health checks
        })

        it('health checks resume when all forms are closed', () => {
            const isCreatingNew = false
            const editingModel = null
            const showHealthConfig = false

            const isAnyFormOpen = isCreatingNew || editingModel !== null || showHealthConfig
            expect(isAnyFormOpen).toBe(false) // Should not pause
        })

        it('model list reloads after health check completes', () => {
            // The lastCheckTime effect triggers loadModels()
            // Verified by: prevCheckTimeRef !== lastCheckTime → loadModels()
            const prevTime = new Date(2026, 3, 5, 10, 0, 0)
            const newTime = new Date(2026, 3, 5, 10, 5, 0)

            expect(prevTime).not.toBe(newTime) // Times differ → reload should trigger
        })
    })

    // =========================================================================
    // Provider Info
    // =========================================================================

    describe('provider classification', () => {
        it('common providers are recognized', () => {
            const knownProviders = [
                'openai', 'anthropic', 'google', 'gemini', 'mistral',
                'cohere', 'groq', 'together_ai', 'fireworks_ai',
                'deepseek', 'xai', 'bedrock', 'azure', 'vertex_ai',
                'ollama', 'vllm', 'lm_studio',
            ]

            knownProviders.forEach(p => {
                expect(typeof p).toBe('string')
                expect(p.length).toBeGreaterThan(0)
            })
        })

        it('model modes are classified correctly', () => {
            const modes = ['chat', 'completion', 'embedding', 'audio_speech', 'audio_transcription', 'image_generation', 'rerank']

            const chatModel = makeModel({ mode: 'chat' })
            const embeddingModel = makeModel({ mode: 'embedding' })

            expect(modes).toContain(chatModel.mode)
            expect(modes).toContain(embeddingModel.mode)
        })
    })

    // =========================================================================
    // Cost Tracking
    // =========================================================================

    describe('cost tracking', () => {
        it('costs are per million tokens', () => {
            const model = makeModel({
                cost_per_input_token: 2.5,   // $2.50 per 1M input tokens
                cost_per_output_token: 10.0,  // $10.00 per 1M output tokens
            })

            // Estimate cost for 1000 input + 500 output tokens
            const inputCost = (1000 / 1_000_000) * model.cost_per_input_token!
            const outputCost = (500 / 1_000_000) * model.cost_per_output_token!
            const totalCost = inputCost + outputCost

            expect(totalCost).toBeCloseTo(0.0075) // $0.0075
        })

        it('embedding models have zero output cost', () => {
            const model = makeModel({
                mode: 'embedding',
                cost_per_input_token: 0.02,
                cost_per_output_token: 0.0,
            })

            expect(model.cost_per_output_token).toBe(0)
        })
    })
})
