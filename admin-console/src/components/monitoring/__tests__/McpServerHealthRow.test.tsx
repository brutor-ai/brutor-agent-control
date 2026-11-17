/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One MCP server on Mission Control's infrastructure panel: its health in
 * words, for each status a server is stored with and for a status that is
 * none of them.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import McpServerHealthRow from '../McpServerHealthRow';

describe('McpServerHealthRow', () => {
    it.each([
        ['healthy', 'Healthy'],
        ['degraded', 'Degraded'],
        ['unhealthy', 'Unhealthy'],
        ['unknown', 'Unknown'],
        ['warning', 'Unknown status: warning'],
    ])('reads the status %s as "%s"', (status, label) => {
        render(<McpServerHealthRow name="payments" health={{ status, consecutive_failures: 0 }} />);
        const row = screen.getByTestId('mcp-server-health');
        expect(row).toHaveTextContent('payments');
        expect(row).toHaveTextContent(label);
        expect(row).toHaveAttribute('data-health', status === 'warning' ? 'unrecognised' : status);
    });

    it('reads a server with no health as Unknown, with no time of a check', () => {
        render(<McpServerHealthRow name="payments" health={undefined} />);
        const row = screen.getByTestId('mcp-server-health');
        expect(row).toHaveTextContent('Unknown');
        expect(row).toHaveAttribute('data-health', 'unknown');
        expect(row).toHaveTextContent('—');
    });

    it('shows the error of the last check beside the status, and when the check was', () => {
        const lastCheck = '2026-11-10T12:00:00Z';
        render(
            <McpServerHealthRow
                name="payments"
                health={{ status: 'unhealthy', consecutive_failures: 5, error_message: 'Circuit breaker open', last_check: lastCheck }}
            />,
        );
        const row = screen.getByTestId('mcp-server-health');
        expect(row).toHaveTextContent('Unhealthy');
        expect(row).toHaveTextContent('Circuit breaker open');
        expect(row).toHaveTextContent(new Date(lastCheck).toLocaleTimeString());
    });
});
