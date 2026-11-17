/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * FormValidation Unit Tests
 *
 * Tests the pure validation functions used by the LLModel form.
 * These guard all model create/edit submissions.
 */

import { describe, it, expect } from 'vitest'
import { validateField, validateJSON, VALIDATION_RULES } from '../formValidationRules'

describe('FormValidation', () => {
    // =========================================================================
    // validateField — name
    // =========================================================================

    describe('name validation', () => {
        it('rejects empty string (required)', () => {
            expect(validateField('name', '', VALIDATION_RULES)).toBeDefined()
        })

        it('rejects null/undefined', () => {
            expect(validateField('name', null, VALIDATION_RULES)).toBeDefined()
            expect(validateField('name', undefined, VALIDATION_RULES)).toBeDefined()
        })

        it('accepts valid name', () => {
            expect(validateField('name', 'OpenAI GPT-5.2', VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts name with special characters', () => {
            expect(validateField('name', "Claude Sonnet 4 (Latest) [v2]", VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects name over 100 characters', () => {
            const longName = 'A'.repeat(101)
            expect(validateField('name', longName, VALIDATION_RULES)).toBeDefined()
        })

        it('accepts name at exactly 100 characters', () => {
            const name = 'A'.repeat(100)
            expect(validateField('name', name, VALIDATION_RULES)).toBeUndefined()
        })
    })

    // =========================================================================
    // validateField — model_name
    // =========================================================================

    describe('model_name validation', () => {
        it('rejects empty string (required)', () => {
            expect(validateField('model_name', '', VALIDATION_RULES)).toBeDefined()
        })

        it('accepts valid model name', () => {
            expect(validateField('model_name', 'gpt-5.2', VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts model name with slashes', () => {
            expect(validateField('model_name', 'anthropic/claude-sonnet-4', VALIDATION_RULES)).toBeUndefined()
        })
    })

    // =========================================================================
    // validateField — temperature
    // =========================================================================

    describe('temperature validation', () => {
        it('accepts 0', () => {
            expect(validateField('temperature', 0, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 2', () => {
            expect(validateField('temperature', 2, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 0.7', () => {
            expect(validateField('temperature', 0.7, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects negative', () => {
            expect(validateField('temperature', -0.1, VALIDATION_RULES)).toBeDefined()
        })

        it('rejects above 2', () => {
            expect(validateField('temperature', 2.1, VALIDATION_RULES)).toBeDefined()
        })

        it('accepts empty (not required)', () => {
            expect(validateField('temperature', '', VALIDATION_RULES)).toBeUndefined()
        })
    })

    // =========================================================================
    // validateField — maxTokens (the minimum is 0)
    // =========================================================================

    describe('maxTokens validation', () => {
        it('accepts 0', () => {
            expect(validateField('maxTokens', 0, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 4096', () => {
            expect(validateField('maxTokens', 4096, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 1000000', () => {
            expect(validateField('maxTokens', 1000000, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects above 1000000', () => {
            expect(validateField('maxTokens', 1000001, VALIDATION_RULES)).toBeDefined()
        })

        it('rejects negative', () => {
            expect(validateField('maxTokens', -1, VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — topP
    // =========================================================================

    describe('topP validation', () => {
        it('accepts 0', () => {
            expect(validateField('topP', 0, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 1', () => {
            expect(validateField('topP', 1, VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts 0.5', () => {
            expect(validateField('topP', 0.5, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects above 1', () => {
            expect(validateField('topP', 1.1, VALIDATION_RULES)).toBeDefined()
        })

        it('rejects negative', () => {
            expect(validateField('topP', -0.1, VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — penalties
    // =========================================================================

    describe('penalty validation', () => {
        it('accepts presence penalty -2 to 2', () => {
            expect(validateField('presencePenalty', -2, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('presencePenalty', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('presencePenalty', 2, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects presence penalty outside range', () => {
            expect(validateField('presencePenalty', -2.1, VALIDATION_RULES)).toBeDefined()
            expect(validateField('presencePenalty', 2.1, VALIDATION_RULES)).toBeDefined()
        })

        it('accepts frequency penalty -2 to 2', () => {
            expect(validateField('frequencyPenalty', -2, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('frequencyPenalty', 2, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects frequency penalty outside range', () => {
            expect(validateField('frequencyPenalty', -2.1, VALIDATION_RULES)).toBeDefined()
            expect(validateField('frequencyPenalty', 2.1, VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — apiUrl
    // =========================================================================

    describe('apiUrl validation', () => {
        it('accepts https URL', () => {
            expect(validateField('apiUrl', 'https://api.openai.com/v1', VALIDATION_RULES)).toBeUndefined()
        })

        it('accepts http URL', () => {
            expect(validateField('apiUrl', 'http://localhost:8080', VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects non-http URL', () => {
            expect(validateField('apiUrl', 'ftp://example.com', VALIDATION_RULES)).toBeDefined()
        })

        it('rejects bare domain', () => {
            expect(validateField('apiUrl', 'api.openai.com', VALIDATION_RULES)).toBeDefined()
        })

        it('accepts empty (not required)', () => {
            expect(validateField('apiUrl', '', VALIDATION_RULES)).toBeUndefined()
        })
    })

    // =========================================================================
    // validateField — timeout and retries
    // =========================================================================

    describe('timeout validation', () => {
        it('accepts 1-600 range', () => {
            expect(validateField('timeout', 1, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('timeout', 120, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('timeout', 600, VALIDATION_RULES)).toBeUndefined()
        })

        it('treats 0 as empty (skipped — not required field)', () => {
            // timeout value 0 is falsy, so validateField treats it as empty
            // and returns undefined (not required). This is the actual behavior.
            expect(validateField('timeout', 0, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects above 600', () => {
            expect(validateField('timeout', 601, VALIDATION_RULES)).toBeDefined()
        })
    })

    describe('retryAttempts validation', () => {
        it('accepts 0-10 range', () => {
            expect(validateField('retryAttempts', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('retryAttempts', 3, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('retryAttempts', 10, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects above 10', () => {
            expect(validateField('retryAttempts', 11, VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — routing weight
    // =========================================================================

    describe('routingWeight validation', () => {
        it('accepts 0-100 range', () => {
            expect(validateField('routingWeight', 0, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('routingWeight', 50, VALIDATION_RULES)).toBeUndefined()
            expect(validateField('routingWeight', 100, VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects above 100', () => {
            expect(validateField('routingWeight', 101, VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — string length limits
    // =========================================================================

    describe('string length validation', () => {
        it('rejects apiVersion over 50 chars', () => {
            expect(validateField('apiVersion', 'A'.repeat(51), VALIDATION_RULES)).toBeDefined()
        })

        it('accepts apiVersion at 50 chars', () => {
            expect(validateField('apiVersion', 'A'.repeat(50), VALIDATION_RULES)).toBeUndefined()
        })

        it('rejects systemPrompt over 100000 chars', () => {
            expect(validateField('systemPrompt', 'A'.repeat(100001), VALIDATION_RULES)).toBeDefined()
        })
    })

    // =========================================================================
    // validateField — unknown field
    // =========================================================================

    describe('unknown field', () => {
        it('returns undefined for unknown field name', () => {
            expect(validateField('nonexistent', 'any value', VALIDATION_RULES)).toBeUndefined()
        })
    })

    // =========================================================================
    // validateJSON
    // =========================================================================

    describe('validateJSON', () => {
        it('accepts valid JSON object', () => {
            expect(validateJSON('{"key": "value"}', 'Test')).toBeUndefined()
        })

        it('accepts valid JSON array', () => {
            expect(validateJSON('[1, 2, 3]', 'Test')).toBeUndefined()
        })

        it('rejects invalid JSON', () => {
            expect(validateJSON('{invalid}', 'Test')).toBeDefined()
            expect(validateJSON('{invalid}', 'Test')).toContain('valid JSON')
        })

        it('accepts empty string', () => {
            expect(validateJSON('', 'Test')).toBeUndefined()
        })

        it('accepts whitespace-only string', () => {
            expect(validateJSON('   ', 'Test')).toBeUndefined()
        })

        it('includes field name in error message', () => {
            expect(validateJSON('{bad', 'Response Format')).toContain('Response Format')
        })
    })
})
