/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Validation rules + field validators. Kept apart from FormValidation.tsx so that
// file exports only components — a mixed file opts out of Fast Refresh
// (react-refresh/only-export-components).

export const VALIDATION_RULES = {
    name: {
        required: true,
        minLength: 1,
        maxLength: 100,
        pattern: /^[a-zA-Z0-9\s\-_.()[\]:,&+'"/#@]+$/,
        message: 'Name must be 1-100 characters, alphanumeric with spaces and common punctuation'
    },
    model_name: {
        required: true,
        minLength: 1,
        maxLength: 100,
        message: 'Model name is required (e.g., gpt-5.2, claude-sonnet-5-5)'
    },
    temperature: {
        min: 0,
        max: 2,
        message: 'Temperature must be between 0 and 2'
    },
    maxTokens: {
        min: 0,
        max: 1000000,
        message: 'Max tokens must be between 0 and 1,000,000'
    },
    topP: {
        min: 0,
        max: 1,
        message: 'Top P must be between 0 and 1'
    },
    presencePenalty: {
        min: -2,
        max: 2,
        message: 'Presence penalty must be between -2 and 2'
    },
    frequencyPenalty: {
        min: -2,
        max: 2,
        message: 'Frequency penalty must be between -2 and 2'
    },
    routingWeight: {
        min: 0,
        max: 100,
        message: 'Routing weight must be between 0 and 100'
    },
    timeout: {
        min: 1,
        max: 600,
        message: 'Timeout must be between 1 and 600 seconds'
    },
    retryAttempts: {
        min: 0,
        max: 10,
        message: 'Retry attempts must be between 0 and 10'
    },
    apiUrl: {
        pattern: /^https?:\/\/.+/,
        message: 'API URL must start with http:// or https://'
    },
    apiVersion: {
        maxLength: 50,
        message: 'API version must be 50 characters or less'
    },
    systemPrompt: {
        maxLength: 100000,
        message: 'System prompt must be 100,000 characters or less'
    }
};

/**
 * Form fields only ever hold primitives. Anything else is a caller bug, and
 * stringifying it would yield `[object Object]` — a value that silently fails
 * a pattern rule with an unreadable message. Treat it as "nothing entered"
 * instead, so a `required` rule reports the honest error.
 */
const asFieldString = (value: unknown): string =>
    typeof value === 'string'
        ? value
        : typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint'
          ? String(value)
          : '';

export const validateField = (name: string, value: unknown, rules: typeof VALIDATION_RULES): string | undefined => {
    const rule = rules[name as keyof typeof rules];
    if (!rule) return undefined;

    // `!value` (rather than a pure emptiness test on the string) is deliberate:
    // it keeps `0` and `false` in the "nothing entered" bucket, which is what
    // the numeric form fields rely on to mean "left blank / inherit".
    const isBlank = !value || asFieldString(value).trim() === '';

    // Required check
    if ('required' in rule && rule.required && isBlank) {
        return `${name.replace(/_/g, ' ')} is required`;
    }

    // Skip other validations if empty and not required
    if (isBlank) {
        return undefined;
    }

    const strValue = asFieldString(value);

    // Min length
    if ('minLength' in rule && rule.minLength && strValue.length < rule.minLength) {
        return rule.message || `Must be at least ${rule.minLength} characters`;
    }

    // Max length
    if ('maxLength' in rule && rule.maxLength && strValue.length > rule.maxLength) {
        return rule.message || `Must be ${rule.maxLength} characters or less`;
    }

    // Pattern
    if ('pattern' in rule && rule.pattern && !rule.pattern.test(strValue)) {
        return rule.message || 'Invalid format';
    }

    // Numeric range
    const numValue = parseFloat(strValue);
    if (!isNaN(numValue)) {
        if ('min' in rule && rule.min !== undefined && numValue < rule.min) {
            return rule.message || `Must be at least ${rule.min}`;
        }
        if ('max' in rule && rule.max !== undefined && numValue > rule.max) {
            return rule.message || `Must be at most ${rule.max}`;
        }
    }

    return undefined;
};

export const validateJSON = (value: string, fieldName: string): string | undefined => {
    if (!value || value.trim() === '') return undefined;

    try {
        JSON.parse(value);
        return undefined;
    } catch {
        return `${fieldName} must be valid JSON`;
    }
};
