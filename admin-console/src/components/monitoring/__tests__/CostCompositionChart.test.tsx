/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The stacked cost-composition chart: adaptive grain via the shared
 * rule, "other" band presence, and the honest empty state.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { CostCompositionChart, CostSeriesResponse } from '../MissionControlCharts';

function series(days: number, keys = ['openai', 'other']): CostSeriesResponse {
    const start = new Date('2026-01-01T00:00:00Z');
    return {
        dimension: 'provider',
        grain: 'day',
        keys: keys.map(k => ({ key: k, name: k === 'other' ? 'Other' : k })),
        data: Array.from({ length: days }, (_, i) => {
            const d = new Date(start);
            d.setUTCDate(d.getUTCDate() + i);
            return {
                date: d.toISOString().slice(0, 10),
                values: Object.fromEntries(keys.map(k => [k, { cost: 1, requests: 1 }])),
            };
        }),
    };
}

describe('CostCompositionChart', () => {
    it('labels the grain from the shared day→week→month rule', () => {
        const { rerender } = render(<CostCompositionChart series={series(14)} />);
        expect(screen.getByText(/daily buckets/)).toBeInTheDocument();

        rerender(<CostCompositionChart series={series(90)} />);
        expect(screen.getByText(/weekly buckets/)).toBeInTheDocument();

        rerender(<CostCompositionChart series={series(365)} />);
        expect(screen.getByText(/monthly buckets/)).toBeInTheDocument();
    });

    it('renders the honest empty state without data', () => {
        render(<CostCompositionChart series={null} />);
        expect(screen.getByText('No cost data for this period')).toBeInTheDocument();
    });

    it('names the dimension in the header', () => {
        render(<CostCompositionChart series={series(10)} />);
        expect(screen.getByText(/by provider/)).toBeInTheDocument();
    });
});
