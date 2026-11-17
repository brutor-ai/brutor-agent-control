/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The model status badge serves two callers with one component: a plain
 * Enabled/Disabled chip driven by the `enabled` prop, and a health verdict
 * driven by `status` + `healthData`. Which mode it is in is decided implicitly
 * by whether `enabled` was passed at all — `enabled={false}` is simple mode
 * showing "Disabled", not health mode.
 *
 * The things worth defending:
 *   - each health verdict names itself, and `requires_tier` is NOT reported as
 *     "Unhealthy" (the catalogue is fine; the key just lacks the tier);
 *   - `requires_tier` personalises to the tier the backend named;
 *   - a status string this build has not seen degrades to "Unknown" rather
 *     than crashing on `config.label`;
 *   - the card variant surfaces the error message and the consecutive-failure
 *     count, which are the two facts an operator needs to act on;
 *   - simple mode ignores health extras it has no data for.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import LLModelStatusBadge from '../LLModelStatusBadge';
import { GATEWAY_MESSAGES, proxyReturned } from '../../../../lib/__tests__/fixtures/dispatchRefusals';
import type { ModelStatus, HealthStatus } from '../../../../lib/llmTypes';

describe('LLModelStatusBadge — simple enabled/disabled mode', () => {
    it('reads "Enabled" when enabled is true', () => {
        render(<LLModelStatusBadge enabled />);
        expect(screen.getByText('Enabled')).toBeInTheDocument();
    });

    it('reads "Disabled" — not "Unhealthy" — when enabled is false', () => {
        // `enabled={false}` is a deliberate operator choice, not a fault.
        render(<LLModelStatusBadge enabled={false} />);
        expect(screen.getByText('Disabled')).toBeInTheDocument();
        expect(screen.queryByText('Unhealthy')).not.toBeInTheDocument();
    });

    it('ignores health decoration it has no data for', () => {
        render(
            <LLModelStatusBadge
                enabled
                showResponseTime
                healthData={{ avg_latency_ms: 250 } as HealthStatus}
            />
        );
        expect(screen.queryByText('250ms')).not.toBeInTheDocument();
    });

    it('falls back to a badge when asked for the card variant', () => {
        // The card layout is health-only; simple mode has nothing to fill it.
        render(<LLModelStatusBadge enabled variant="card" />);
        expect(screen.getByText('Enabled')).toBeInTheDocument();
        expect(screen.queryByText('Health Status')).not.toBeInTheDocument();
    });
});

describe('LLModelStatusBadge — health mode', () => {
    it.each([
        ['healthy', 'Healthy'],
        ['unhealthy', 'Unhealthy'],
        ['degraded', 'Degraded'],
        ['unknown', 'Unknown'],
        ['requires_tier', 'Requires Tier'],
    ])('labels %s as "%s"', (status, label) => {
        render(<LLModelStatusBadge status={status as ModelStatus} />);
        expect(screen.getByText(label)).toBeInTheDocument();
    });

    it('does not report a tier gate as an outage', () => {
        render(<LLModelStatusBadge status="requires_tier" />);
        expect(screen.queryByText('Unhealthy')).not.toBeInTheDocument();
    });

    it('names the tier the key is missing when the backend supplies it', () => {
        render(
            <LLModelStatusBadge
                status="requires_tier"
                healthData={{ required_tier: 'Pro' } as HealthStatus}
            />
        );
        expect(screen.getByText('Requires Pro')).toBeInTheDocument();
    });

    it('degrades to Unknown for a status string this build does not know', () => {
        render(<LLModelStatusBadge status={'quarantined' as ModelStatus} />);
        expect(screen.getByText('Unknown')).toBeInTheDocument();
    });

    it('defaults to Unknown when no status is supplied at all', () => {
        render(<LLModelStatusBadge />);
        expect(screen.getByText('Unknown')).toBeInTheDocument();
    });

    it('shows latency alongside the verdict when asked', () => {
        render(
            <LLModelStatusBadge
                status="healthy"
                showResponseTime
                healthData={{ avg_latency_ms: 412 } as HealthStatus}
            />
        );
        expect(screen.getByText('412ms')).toBeInTheDocument();
    });

    it('omits latency when the health payload has none', () => {
        render(
            <LLModelStatusBadge
                status="healthy"
                showResponseTime
                healthData={{} as HealthStatus}
            />
        );
        expect(screen.queryByText(/ms$/)).not.toBeInTheDocument();
    });

    it('renders latency parenthesised in the inline variant', () => {
        render(
            <LLModelStatusBadge
                status="degraded"
                variant="inline"
                showResponseTime
                healthData={{ avg_latency_ms: 90 } as HealthStatus}
            />
        );
        expect(screen.getByText('(90ms)')).toBeInTheDocument();
    });
});

describe('LLModelStatusBadge — card variant', () => {
    const failing = {
        avg_latency_ms: 1200,
        error_message: '401 Unauthorized from provider',
        consecutive_failures: 3,
        last_check: '2026-05-01T10:30:00Z',
    } as HealthStatus;

    it('surfaces the provider error rather than only a red chip', () => {
        render(
            <LLModelStatusBadge
                status="unhealthy"
                variant="card"
                showErrorMessage
                healthData={failing}
            />
        );
        expect(screen.getByText('401 Unauthorized from provider')).toBeInTheDocument();
    });

    it('counts consecutive failures, pluralised', () => {
        render(
            <LLModelStatusBadge status="unhealthy" variant="card" healthData={failing} />
        );
        expect(screen.getByText(/3 consecutive failures/)).toBeInTheDocument();
    });

    it('uses the singular for a single failure', () => {
        render(
            <LLModelStatusBadge
                status="degraded"
                variant="card"
                healthData={{ consecutive_failures: 1 } as HealthStatus}
            />
        );
        expect(screen.getByText(/1 consecutive failure(?!s)/)).toBeInTheDocument();
    });

    it('says nothing about failures when the streak is zero', () => {
        render(
            <LLModelStatusBadge
                status="healthy"
                variant="card"
                healthData={{ consecutive_failures: 0 } as HealthStatus}
            />
        );
        expect(screen.queryByText(/consecutive failure/)).not.toBeInTheDocument();
    });

    it('says what to fix for a gateway dispatch refusal, not the raw answer', () => {
        const raw = proxyReturned(GATEWAY_MESSAGES.provider_credential_missing);
        render(
            <LLModelStatusBadge
                status="unhealthy"
                variant="card"
                showErrorMessage
                healthData={{ ...failing, error_message: raw }}
            />
        );
        expect(screen.getByText(/AWS access key ID and secret access key/)).toBeInTheDocument();
        expect(screen.queryByText(raw)).toBeNull();
    });

    it('hides the error message unless explicitly asked for', () => {
        render(
            <LLModelStatusBadge status="unhealthy" variant="card" healthData={failing} />
        );
        expect(
            screen.queryByText('401 Unauthorized from provider')
        ).not.toBeInTheDocument();
    });

    it('shows a last-check timestamp only when requested', () => {
        const { rerender } = render(
            <LLModelStatusBadge status="healthy" variant="card" healthData={failing} />
        );
        expect(screen.getByText('Health Status')).toBeInTheDocument();
        const before = document.body.textContent ?? '';

        rerender(
            <LLModelStatusBadge
                status="healthy"
                variant="card"
                showLastCheck
                healthData={failing}
            />
        );
        // The rendered time is locale-dependent, so assert that requesting it
        // adds content rather than pinning a formatted string.
        expect((document.body.textContent ?? '').length).toBeGreaterThan(before.length);
    });
});
