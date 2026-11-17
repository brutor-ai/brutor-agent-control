/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { UnifiedProxyLogEntry as ProxyLog } from '../../../lib/types';
import ProxyLogDetailsModal from '../ProxyLogDetailsModal';

// Content digests, trace context, tool provenance, deciding model.
// The modal is where an auditor reads a single row; a field the UI does not
// render is evidence nobody can see.

const base = {
    id: 'log-1',
    tenant_id: 't',
    timestamp: '2026-09-03T10:00:00Z',
    proxy_type: 'mcp',
    http_method: 'POST',
    endpoint: 'mcp-proxy',
    success: true,
    correlation_id: 'corr-1',
} as unknown as ProxyLog;

describe('ProxyLogDetailsModal — integrity & provenance', () => {
    it('renders the digests, tool provenance, deciding model and trace ids when recorded', () => {
        const log = {
            ...base,
            request_content_hash: 'a'.repeat(64),
            response_content_hash: 'b'.repeat(64),
            tool_definition_hash: 'c'.repeat(64),
            server_version: '3.1.4',
            deciding_model: 'gpt-5.2',
            trace_id: '0af7651916cd43dd8448eb211c80319c',
            span_id: 'b7ad6b7169203331',
        } as ProxyLog;
        render(<ProxyLogDetailsModal log={log} onClose={vi.fn()} />);

        expect(screen.getByText('Content Integrity & Tool Provenance')).toBeInTheDocument();
        expect(screen.getByText('a'.repeat(64))).toBeInTheDocument();
        expect(screen.getByText('b'.repeat(64))).toBeInTheDocument();
        expect(screen.getByText('c'.repeat(64))).toBeInTheDocument();
        expect(screen.getByText('3.1.4')).toBeInTheDocument();
        expect(screen.getByText('gpt-5.2')).toBeInTheDocument();
        expect(screen.getByText('Trace ID (W3C)')).toBeInTheDocument();
        expect(screen.getByText('0af7651916cd43dd8448eb211c80319c')).toBeInTheDocument();
        expect(screen.getByText('b7ad6b7169203331')).toBeInTheDocument();
    });

    it('omits the section entirely when nothing was recorded — absence is not a claim', () => {
        render(<ProxyLogDetailsModal log={base} onClose={vi.fn()} />);
        expect(screen.queryByText('Content Integrity & Tool Provenance')).toBeNull();
        expect(screen.queryByText('Trace ID (W3C)')).toBeNull();
    });

    it('says in plain words why the gateway refused a call whose arguments were too large to check', () => {
        const recorded =
            'Argument policy (payments-threshold/arguments_truncated): Refused: the arguments are too large or too deeply nested to be checked against this policy';
        render(<ProxyLogDetailsModal log={{ ...base, success: false, error_message: recorded }} onClose={vi.fn()} />);
        expect(screen.getByTestId('gateway-refusal-explained')).toHaveTextContent(
            'its arguments are too large to check against argument policy "payments-threshold"',
        );
        // What the gateway recorded stays on the page.
        expect(screen.getByText(recorded)).toBeInTheDocument();
    });

    it('explains nothing for an error it has no words for', () => {
        render(<ProxyLogDetailsModal log={{ ...base, success: false, error_message: 'upstream 500' }} onClose={vi.fn()} />);
        expect(screen.queryByTestId('gateway-refusal-explained')).toBeNull();
    });
});
