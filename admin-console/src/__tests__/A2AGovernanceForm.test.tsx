/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A2AGovernanceForm — pure presentational A2A governance form.
 *
 * These tests pin the contract:
 *   (1) capability.default_access (select) and
 *       capability.approval_timeout_seconds (number) round-trip
 *       through `onChange`.
 *   (2) The two chip-lists (data.deny_classifications and
 *       remote.allowed_globs) support add AND remove, emitting the
 *       exact nested-JSON array shape the enforcement layer reads.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import A2AGovernanceForm, {
    type A2AGovernance,
} from '../components/groups/subtabs/A2AGovernanceForm';

describe('A2AGovernanceForm — capability section', () => {
    it('renders all three cards', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByText('Capability access')).toBeInTheDocument();
        expect(screen.getByText('Deny classifications')).toBeInTheDocument();
        expect(screen.getByText('Allowed remote URL globs')).toBeInTheDocument();
    });

    it('round-trips capability.default_access', () => {
        const onChange = vi.fn();
        render(<A2AGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(screen.getByRole('combobox'), {
            target: { value: 'approval_required' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                capability: { default_access: 'approval_required' },
            }),
        );
    });

    it('clearing default_access to the (not set) option emits undefined', () => {
        const onChange = vi.fn();
        const initial: A2AGovernance = {
            capability: { default_access: 'disabled' },
        };
        render(<A2AGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                capability: { default_access: undefined },
            }),
        );
    });

    it('round-trips capability.approval_timeout_seconds', () => {
        const onChange = vi.fn();
        render(<A2AGovernanceForm value={{}} onChange={onChange} />);
        const label = screen.getByText('approval_timeout_seconds');
        const input = label.parentElement!.parentElement!.querySelector(
            'input[type="number"]',
        ) as HTMLInputElement;
        fireEvent.change(input, { target: { value: '30' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                capability: { approval_timeout_seconds: 30 },
            }),
        );
    });
});

describe('A2AGovernanceForm — deny_classifications chip-list', () => {
    it('adds a chip', () => {
        const onChange = vi.fn();
        render(<A2AGovernanceForm value={{}} onChange={onChange} />);
        const input = screen.getByPlaceholderText('PII');
        fireEvent.change(input, { target: { value: 'secret' } });
        // The Add button is the element immediately after the input.
        fireEvent.click(input.nextElementSibling as HTMLElement);
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                data: { deny_classifications: ['secret'] },
            }),
        );
    });

    it('removes a chip', () => {
        const onChange = vi.fn();
        const initial: A2AGovernance = {
            data: { deny_classifications: ['PII', 'PHI'] },
        };
        render(<A2AGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.click(screen.getByLabelText('Remove PII'));
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                data: { deny_classifications: ['PHI'] },
            }),
        );
    });
});

describe('A2AGovernanceForm — allowed_globs chip-list', () => {
    it('adds a chip', () => {
        const onChange = vi.fn();
        render(<A2AGovernanceForm value={{}} onChange={onChange} />);
        const input = screen.getByPlaceholderText(
            'https://*.partner.com/*',
        );
        fireEvent.change(input, { target: { value: 'https://x.example/*' } });
        fireEvent.click(input.nextElementSibling as HTMLElement);
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                remote: { allowed_globs: ['https://x.example/*'] },
            }),
        );
    });

    it('removes a chip', () => {
        const onChange = vi.fn();
        const initial: A2AGovernance = {
            remote: { allowed_globs: ['https://a.example/*'] },
        };
        render(<A2AGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.click(screen.getByLabelText('Remove https://a.example/*'));
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                remote: { allowed_globs: [] },
            }),
        );
    });
});

describe('A2AGovernanceForm: what the gateway does with the policy', () => {
    it('says every level is judged on its own and the strictest verdict wins', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByTestId('a2a-levels-help')).toHaveTextContent(
            'Each level that sets a policy is judged on its own: the caller\'s group, every group above it, and the agent card. A refusal at any level refuses the call; otherwise a hold at any level holds it.',
        );
    });

    it('says approval_required holds the call for a person, and what is not held', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByTestId('a2a-access-help')).toHaveTextContent(
            'enabled lets the call through. approval_required holds a call that does work (a message, an invocation) until a person approves or rejects it in Mission Control → Approvals; a call that only reads is not held. disabled refuses every A2A call.',
        );
    });

    it('says how long a held call waits', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByTestId('a2a-approval-window-help')).toHaveTextContent(
            "How long a held call waits for a decision, in seconds. When the time runs out, the call is not made. The shortest time any level states applies. Empty: the group's default approval window, then the tenant's, then 5 minutes. At most 7 days.",
        );
    });

    it('says the allow-list judges outbound calls, and that the address must match the list of every level', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByTestId('a2a-allow-list-help')).toHaveTextContent(
            'Judges outbound calls only: the address a call leaves for must match at least one pattern. An inbound call has no remote address, so the list does not judge it. Every level that states a list is judged on its own, so the address must match the list of each of them. An empty list restricts nothing at this level.',
        );
    });

    it('shows the allow-list of each level above by its group, never one merged list', () => {
        render(
            <A2AGovernanceForm
                value={{ remote: { allowed_globs: ['https://a.partner.example/*'] } }}
                onChange={vi.fn()}
                inherited={{ remote: { allowed_globs: [] }, data: { deny_classifications: ['PII'] } }}
                allowListsAbove={[
                    { level: 'Tenant', globs: ['https://*.partner.example/*'] },
                    { level: 'Payments', globs: ['https://a.partner.example/*', 'https://b.partner.example/*'] },
                ]}
            />,
        );
        const above = screen.getByTestId('a2a-allow-lists-above');
        expect(above).toHaveTextContent('The address must also match the allow-list of each of these groups:');
        const rows = Array.from(above.querySelectorAll('li')).map((li) => li.textContent);
        expect(rows).toEqual(['Tenant: https://*.partner.example/*', 'Payments: https://a.partner.example/*, https://b.partner.example/*']);
        // The classifications are a union, shown as before.
        expect(screen.getByText('Inherited: PII')).toBeInTheDocument();
    });

    it('does not show an inherited allow-list merged from several levels', () => {
        render(
            <A2AGovernanceForm
                value={{}}
                onChange={vi.fn()}
                inherited={{ remote: { allowed_globs: ['https://merged.example/*'] } }}
            />,
        );
        expect(screen.queryByText(/merged\.example/)).toBeNull();
        expect(screen.queryByTestId('a2a-allow-lists-above')).toBeNull();
    });
});

describe('A2AGovernanceForm — approval timeout bounds', () => {
    const RANGE = 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).';

    it('bounds the field to 1 .. 604800 seconds', () => {
        render(<A2AGovernanceForm value={{}} onChange={vi.fn()} />);
        const input = screen.getByPlaceholderText<HTMLInputElement>('—');
        expect(input.min).toBe('1');
        expect(input.max).toBe('604800');
        expect(screen.queryByTestId('approval-timeout-problem')).toBeNull();
    });

    it('says the range beside a value outside it', () => {
        const { rerender } = render(<A2AGovernanceForm value={{ capability: { approval_timeout_seconds: 0 } }} onChange={vi.fn()} />);
        expect(screen.getByTestId('approval-timeout-problem')).toHaveTextContent(RANGE);
        rerender(<A2AGovernanceForm value={{ capability: { approval_timeout_seconds: 604801 } }} onChange={vi.fn()} />);
        expect(screen.getByTestId('approval-timeout-problem')).toHaveTextContent(RANGE);
        rerender(<A2AGovernanceForm value={{ capability: { approval_timeout_seconds: 300 } }} onChange={vi.fn()} />);
        expect(screen.queryByTestId('approval-timeout-problem')).toBeNull();
    });
});
