/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP server-limits form: Execution / Data / Frequency.
 *
 * Three sections over one flat dict, and every field has to land on the
 * right `section.field` path or the operator caps a different thing from
 * the one they typed into. The floors matter too — a timeout, a memory
 * ceiling or a concurrency slot of 0 is not "unlimited", it is "nothing
 * runs", so those fields floor at 1 while a cooldown of 0 is legitimate.
 *
 * The help modal is per-section: an operator asking about Data limits must
 * not land on the Execution page.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import MCPLimitsForm from '../MCPLimitsForm';

function renderForm(props: Partial<React.ComponentProps<typeof MCPLimitsForm>> = {}) {
    const onChange = vi.fn();
    const view = render(<MCPLimitsForm value={{}} onChange={onChange} {...props} />);
    return { onChange, ...view };
}

const input = (key: string) => screen.getByTestId(`limit-input-${key}`);

describe('MCPLimitsForm — the three sections', () => {
    it('renders execution, data and frequency together', () => {
        renderForm();
        expect(screen.getByText('Execution Limits')).toBeInTheDocument();
        expect(screen.getByText('Data Limits')).toBeInTheDocument();
        expect(screen.getByText('Frequency Limits')).toBeInTheDocument();
    });

    it('puts each field on its own section.field path', () => {
        renderForm();
        for (const key of [
            'execution.timeout_seconds',
            'execution.max_concurrency_per_user',
            'execution.max_concurrency_per_group',
            'data.max_payload_size_mb',
            'frequency.max_calls_per_minute',
            'frequency.max_calls_per_hour',
            'frequency.max_calls_per_day',
            'frequency.burst_limit',
            'frequency.cooldown_seconds',
        ]) {
            expect(screen.getByTestId(`limit-field-${key}`)).toBeInTheDocument();
        }
    });

    it('separates per-user from per-group concurrency', () => {
        renderForm();
        expect(screen.getByTestId('limit-field-execution.max_concurrency_per_user')).toHaveTextContent(
            'Max Concurrency (per user)',
        );
        expect(screen.getByTestId('limit-field-execution.max_concurrency_per_group')).toHaveTextContent(
            'Max Concurrency (per group)',
        );
    });
});

describe('MCPLimitsForm — floors that keep a limit meaningful', () => {
    it.each([
        'execution.timeout_seconds',
        'execution.max_concurrency_per_user',
        'frequency.max_calls_per_minute',
    ])('%s cannot be driven to zero — that would stop all traffic', (key) => {
        renderForm();
        expect(input(key)).toHaveAttribute('min', '1');
    });

    it('a payload cap allows fractions of a megabyte', () => {
        renderForm();
        expect(input('data.max_payload_size_mb')).toHaveAttribute('step', '0.01');
        expect(input('data.max_payload_size_mb')).toHaveAttribute('min', '0.01');
    });

    it('a cooldown of zero is legitimate — no cooldown at all', () => {
        renderForm();
        expect(input('frequency.cooldown_seconds')).toHaveAttribute('min', '0');
    });
});

describe('MCPLimitsForm — editing', () => {
    it('reads back the stored dict', () => {
        renderForm({
            value: { execution: { timeout_seconds: 30 }, frequency: { max_calls_per_day: 5000 } },
        });
        expect(input('execution.timeout_seconds')).toHaveValue(30);
        expect(input('frequency.max_calls_per_day')).toHaveValue(5000);
    });

    it('an edit leaves sibling sections and sibling fields alone', () => {
        const { onChange } = renderForm({
            value: {
                execution: { timeout_seconds: 30, max_concurrency_per_user: 2 },
                data: { max_payload_size_mb: 4 },
            },
        });
        fireEvent.change(input('execution.timeout_seconds'), { target: { value: '60' } });
        expect(onChange).toHaveBeenCalledWith({
            execution: { timeout_seconds: 60, max_concurrency_per_user: 2 },
            data: { max_payload_size_mb: 4 },
        });
    });

    it('clearing a field emits undefined, not zero', () => {
        const { onChange } = renderForm({ value: { frequency: { burst_limit: 10 } } });
        fireEvent.change(input('frequency.burst_limit'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ frequency: { burst_limit: undefined } });
    });

    it('a read-only caller gets fields it cannot edit', () => {
        renderForm({ disabled: true });
        expect(input('execution.timeout_seconds')).toBeDisabled();
        expect(input('frequency.burst_limit')).toBeDisabled();
    });
});

describe('MCPLimitsForm — inheritance', () => {
    it('a root group is shown no ceilings it does not have', () => {
        renderForm({ value: { execution: { timeout_seconds: 30 } } });
        expect(screen.queryByTestId('limit-inherited-execution.timeout_seconds')).toBeNull();
    });

    it('an inherited ceiling shows, and an edit above it is flagged as a coming refusal', () => {
        renderForm({
            value: { execution: { timeout_seconds: 90 } },
            inherited: { execution: { timeout_seconds: 60 } },
        });
        expect(screen.getByTestId('limit-inherited-execution.timeout_seconds')).toHaveTextContent('max 60');
        expect(screen.getByTestId('limit-error-execution.timeout_seconds')).toBeInTheDocument();
    });

    it('capacity alone is enough to mark the group as having a parent', () => {
        renderForm({
            capacity: {
                parent_limit: { frequency: { max_calls_per_day: 10000 } },
                parent_name: 'ACME Corp',
                parent_is_immediate: true,
                siblings: [],
                sibling_total: { frequency: { max_calls_per_day: 4000 } },
                available: { frequency: { max_calls_per_day: 6000 } },
            },
        });
        expect(screen.getByTestId('limit-available-frequency.max_calls_per_day')).toHaveTextContent(
            'available 6000 of 10000',
        );
    });
});

describe('MCPLimitsForm — what each execution and data limit does', () => {
    it.each([
        [
            'execution.timeout_seconds',
            "The total time of a call or of a POST stream; with the server's own timeout it also caps the wait for the answer to start. The listener stream is exempt. Past it the call is refused with 504, and a streamed call ends with a timeout event.",
        ],
        [
            'execution.max_concurrency_per_user',
            "Simultaneous tools/call per identity, in this order: the agent, the user (all of a user's keys share one allowance), the identity-provider subject, the group key; any other caller shares one allowance. Only tools/call counts: protocol traffic and the listener stream do not. Past it: 429 rate_limit_exceeded, scope group-user, with Retry-After.",
        ],
        [
            'execution.max_concurrency_per_group',
            "Simultaneous tools/call in this group, across the cluster. A value set here applies to each descendant group separately. Past it: 429 rate_limit_exceeded, scope group, with Retry-After.",
        ],
        [
            'data.max_payload_size_mb',
            'Caps the request and the answer. A request is never above 10 MiB, and with no value here an answer is not capped. A request too large is refused with 413; an answer too large with 502 payload_too_large (a streamed answer ends with an error event).',
        ],
    ])('%s says what the gateway does with it', (key, text) => {
        renderForm();
        expect(screen.getByTestId(`limit-help-${key}`)).toHaveTextContent(text);
    });

    it('the execution help says a call past an allowance is refused, never queued', () => {
        renderForm();
        fireEvent.click(screen.getAllByTitle('Help')[0]);
        expect(screen.queryByText(/queued/)).toBeNull();
        expect(screen.getAllByText(/429/).length).toBeGreaterThan(0);
    });

    it('the data help states the 10 MiB request ceiling', () => {
        renderForm();
        fireEvent.click(screen.getAllByTitle('Help')[1]);
        expect(screen.getAllByText(/10 MiB/).length).toBeGreaterThan(0);
        expect(screen.queryByText(/100\+ MB/)).toBeNull();
    });
});

describe('MCPLimitsForm — help', () => {
    it('each section has its own help button', () => {
        renderForm();
        expect(screen.getAllByTitle('Help')).toHaveLength(3);
    });

    it('help stays closed until asked for', () => {
        renderForm();
        expect(screen.queryByText('MCP Limits Help')).toBeNull();
    });

    it('opens on the section the operator asked about, not always the first', () => {
        renderForm();
        fireEvent.click(screen.getAllByTitle('Help')[1]);
        expect(screen.getByText('MCP Limits Help')).toBeInTheDocument();
        expect(screen.getByText('Data Limits Overview')).toBeInTheDocument();
        expect(screen.queryByText('Execution Limits Overview')).toBeNull();
    });

    it('the frequency section opens the frequency page', () => {
        renderForm();
        fireEvent.click(screen.getAllByTitle('Help')[2]);
        expect(screen.getByText('Frequency Limits Overview')).toBeInTheDocument();
    });
});
