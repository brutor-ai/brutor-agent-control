/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import type { ContextSignal } from '../../../lib/aiSystemTypes';
import { ContextSizeSection } from '../ContextSizeSection';

const MEASURED: ContextSignal = {
    runs: 40,
    measured_runs: 30,
    median_peak_tokens: 12_500,
    p95_peak_tokens: 88_000,
    max_peak_tokens: 120_000,
    median_growth: 3.25,
    p95_growth: 8,
    utilization_measured_runs: 20,
    p95_utilization: 0.44,
    max_utilization: 0.6,
};

describe('ContextSizeSection', () => {
    afterEach(cleanup);

    it('states the figures beside their coverage', () => {
        render(<ContextSizeSection context={MEASURED} />);
        expect(screen.getByTestId('context-median-peak')).toHaveTextContent('12,500');
        expect(screen.getByTestId('context-median-peak')).toHaveTextContent('30 of 40 processed runs measured');
        expect(screen.getByTestId('context-p95-peak')).toHaveTextContent('88,000');
        expect(screen.getByTestId('context-p95-peak')).toHaveTextContent('max 120,000');
        expect(screen.getByTestId('context-growth')).toHaveTextContent('3.3×');
        expect(screen.getByTestId('context-utilization')).toHaveTextContent('44.0%');
        expect(screen.getByTestId('context-utilization')).toHaveTextContent('20 of 30 measured runs');
    });

    it('nothing measured reads "Not measured", never a zero', () => {
        render(<ContextSizeSection context={{ runs: 12, measured_runs: 0, utilization_measured_runs: 0 }} />);
        const tile = screen.getByTestId('context-not-measured');
        expect(tile).toHaveTextContent('Not measured');
        expect(tile).toHaveTextContent('0 of 12 processed runs reported usage');
        expect(screen.queryByTestId('context-median-peak')).toBeNull();
    });

    it('an unknown window reads "Unknown", never 0%', () => {
        render(
            <ContextSizeSection
                context={{ ...MEASURED, utilization_measured_runs: 0, p95_utilization: null, max_utilization: null }}
            />
        );
        expect(screen.getByTestId('context-utilization-unknown')).toHaveTextContent('Unknown');
        expect(screen.queryByTestId('context-utilization')).toBeNull();
    });

    it('applies no good-or-bad tone: movement is judged by drift, not a threshold', () => {
        render(<ContextSizeSection context={{ ...MEASURED, p95_utilization: 0.99, max_peak_tokens: 900_000 }} />);
        expect(screen.getByTestId('signal-context').querySelector('[data-tone="warn"]')).toBeNull();
    });
});
