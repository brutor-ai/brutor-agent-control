/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The trend charts under the Signals sub-tabs. Recharts draws nothing in
 * jsdom (no layout), so these pin what the chart SAYS: the captions name
 * the population, the outcome bars are the run classes (no-work grey, never
 * green, no "blocked" series), and every gap is explained in words.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import SignalTrendChart from '../SignalTrendChart';
import type { RunTimeseriesPoint } from '../../../lib/aiSystemTypes';

function point(h: number, over: Partial<RunTimeseriesPoint> = {}): RunTimeseriesPoint {
    return {
        bucket_start: `2026-10-25T${String(h).padStart(2, '0')}:00:00Z`,
        runs: 0, success: 0, no_work: 0, deviation: 0, failed: 0, not_decisive: 0,
        cost_usd: 0, processed_cost_usd: 0, cost_per_processed_outcome: null,
        tool_calls: 0, tool_errors: 0, tool_error_rate: null,
        model_calls: 0, model_errors: 0, model_error_rate: null,
        avg_actions: null,
        ...over,
    };
}

const busy = (h: number) =>
    point(h, {
        runs: 5, success: 3, no_work: 2, processed_cost_usd: 0.06, cost_per_processed_outcome: 0.02,
        tool_calls: 4, tool_error_rate: 0, model_calls: 3, model_error_rate: 0, avg_actions: 10,
    });
// An outage hour: an empty poll succeeded, the screening errored.
const outage = (h: number) =>
    point(h, { runs: 2, no_work: 1, failed: 1, model_calls: 1, model_error_rate: 1, tool_calls: 1, tool_error_rate: 0 });

describe('SignalTrendChart', () => {
    it('draws the outcome bars by run class, no-work grey and no blocked series', () => {
        render(<SignalTrendChart kind="outcomes" data={[busy(16), outage(17)]} bucket="hour" />);
        const chart = screen.getByTestId('trend-outcomes');
        expect(chart).toHaveTextContent('Runs by class over time');
        const legend = screen.getByTestId('trend-outcomes-legend');
        expect(legend).toHaveTextContent('Success');
        expect(legend).toHaveTextContent('No work');
        expect(legend).toHaveTextContent('Deviation');
        expect(legend).toHaveTextContent('Failed');
        expect(legend).not.toHaveTextContent(/blocked/i);
        const swatch = (name: string) =>
            Array.from(legend.querySelectorAll('span')).find((s) => s.textContent === name)?.querySelector('span');
        expect(swatch('No work')?.getAttribute('style')).toContain('#94a3b8');
        expect(swatch('Success')?.getAttribute('style')).toContain('#10b981');
    });

    it('captions cost and length as per processed outcome', () => {
        const { unmount } = render(<SignalTrendChart kind="cost" data={[busy(16), busy(17)]} bucket="hour" />);
        expect(screen.getByTestId('trend-cost')).toHaveTextContent('Cost per processed outcome over time');
        unmount();
        render(<SignalTrendChart kind="actions" data={[busy(16), busy(17)]} bucket="hour" />);
        expect(screen.getByTestId('trend-actions')).toHaveTextContent('Average actions per processed outcome over time');
    });

    it('explains an outage hour on the cost line as "no processed outcomes", not a $0 point', () => {
        render(<SignalTrendChart kind="cost" data={[busy(16), outage(17), point(18), busy(19)]} bucket="hour" />);
        const legend = screen.getByTestId('trend-cost-gap-legend');
        expect(legend).toHaveTextContent('No processed outcomes');
        expect(legend).toHaveTextContent('only empty polls (no work), failures or deviations');
        expect(legend).toHaveTextContent('No runs');
        expect(legend).not.toHaveTextContent('No paid outcomes');
    });

    it('explains an hour of $0 processed outcomes as "no paid outcomes"', () => {
        const free = point(17, { runs: 8, success: 8, processed_cost_usd: 0, cost_per_processed_outcome: 0, avg_actions: 1 });
        render(<SignalTrendChart kind="cost" data={[busy(16), free, busy(18)]} bucket="hour" />);
        const legend = screen.getByTestId('trend-cost-gap-legend');
        expect(legend).toHaveTextContent('No paid outcomes');
        expect(legend).toHaveTextContent('approved decision applied');
        expect(legend).not.toHaveTextContent('No processed outcomes');
    });

    it('draws nothing for a window with no runs', () => {
        const { container } = render(<SignalTrendChart kind="outcomes" data={[point(1), point(2)]} bucket="hour" />);
        expect(container).toBeEmptyDOMElement();
    });
});
