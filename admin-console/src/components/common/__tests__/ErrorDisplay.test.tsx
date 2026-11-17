/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The error banner's "Show details" prints the answer the control plane gave.
 *
 * For a 422 that answer carries, per error, the `input` that failed. Where the
 * input was a secret the control plane sends the marker `[redacted]` instead:
 * printed as it came, it reads as if the user had typed "[redacted]".
 */

import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

import { ErrorDisplay } from '../ErrorDisplay';
import type { ErrorDetails } from '../../../hooks/useErrorHandler';

function failure(details: unknown): ErrorDetails {
    return {
        id: 'e-1',
        message: 'new_password: String should have at least 8 characters',
        code: '422',
        details,
        timestamp: new Date('2026-11-12T10:00:00Z'),
        retryable: false,
        severity: 'medium',
    };
}

describe('ErrorDisplay', () => {
    afterEach(() => cleanup());

    it('does not print the server\'s redaction marker as the value that was entered', () => {
        render(
            <ErrorDisplay
                onDismiss={() => {}}
                errors={[
                    failure({
                        detail: [
                            { type: 'string_too_short', loc: ['body', 'new_password'], msg: 'String should have at least 8 characters', input: '[redacted]' },
                            { type: 'string_pattern_mismatch', loc: ['body', 'name'], msg: 'String should match pattern', input: 'two words' },
                        ],
                    }),
                ]}
            />,
        );
        const details = screen.getByText(/string_too_short/);
        expect(details.textContent).not.toContain('[redacted]');
        // The rest of the answer is still there, the user's own value included.
        expect(details.textContent).toContain('"new_password"');
        expect(details.textContent).toContain('String should have at least 8 characters');
        expect(details.textContent).toContain('"input": "two words"');
    });

    it('prints other details as they came', () => {
        render(<ErrorDisplay onDismiss={() => {}} errors={[failure({ detail: 'Server not found', code: 'not_found' })]} />);
        expect(screen.getByText(/Server not found/).textContent).toBe(
            JSON.stringify({ detail: 'Server not found', code: 'not_found' }, null, 2),
        );
    });
});
