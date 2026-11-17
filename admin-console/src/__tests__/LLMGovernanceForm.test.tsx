/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLMGovernanceForm — behavioral-governance form (temperature,
 * context window, tool use, system prompt). These tests pin the
 * onChange nested-JSON contract the Rust enforcement layer reads:
 * every field renders, edits emit the right section.field shape, and
 * other sections/fields are preserved on edit.
 *
 * NOTE on the clear contract: unlike LLMLimitsForm, this component's
 * `updateField` does NOT delete keys/sections when a field is cleared —
 * it writes `undefined` for that field and keeps siblings. The tests
 * assert the component's ACTUAL behavior (sibling preserved, field
 * becomes undefined), not the delete-on-clear behavior.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import LLMGovernanceForm from '../components/groups/subtabs/LLMGovernanceForm';
import { LLMGovernance } from '../lib/groupTypes';

/** Find the control (input/select/textarea) that follows a label. */
function control(label: string, tag: 'input' | 'select' | 'textarea', idx = 0) {
    const labels = screen.getAllByText(label);
    const el = labels[idx].parentElement!.querySelector(tag);
    if (!el) throw new Error(`no <${tag}> found for label "${label}" [${idx}]`);
    return el;
}

describe('LLMGovernanceForm — section rendering', () => {
    it('renders all four section headers', () => {
        render(<LLMGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByText('Temperature')).toBeInTheDocument();
        expect(screen.getByText('Context Window')).toBeInTheDocument();
        expect(screen.getByText('Tool Use')).toBeInTheDocument();
        expect(screen.getByText('System Prompt')).toBeInTheDocument();
    });

    it('renders every field label', () => {
        render(<LLMGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByText('Max Temperature')).toBeInTheDocument();
        expect(screen.getAllByText('Enforce Mode')).toHaveLength(2); // temperature + context_window
        expect(screen.getByText('Max Input Tokens')).toBeInTheDocument();
        expect(screen.getByText('Max Output Tokens')).toBeInTheDocument();
        expect(screen.getByText('Access')).toBeInTheDocument();
        // A model's tool calls are never held for a person: no approval timeout.
        expect(screen.queryByText('Approval Timeout (seconds)')).toBeNull();
        expect(screen.getByText('Allowed Tools')).toBeInTheDocument();
        expect(screen.getByText('Blocked Tools')).toBeInTheDocument();
        expect(screen.getByText('Max Tool Calls Per Request')).toBeInTheDocument();
        expect(screen.getByText('Max Tool Calls Per Hour')).toBeInTheDocument();
        expect(screen.getByText('Max Tool Calls Per Day')).toBeInTheDocument();
        expect(screen.getByText('Banned Argument Patterns')).toBeInTheDocument();
        expect(screen.getByText('Mandatory Fragments')).toBeInTheDocument();
        expect(screen.getByText('Banned Patterns')).toBeInTheDocument();
    });
});

describe('LLMGovernanceForm — temperature', () => {
    it('writes temperature.max (parsed as float)', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Temperature', 'input'), { target: { value: '1.5' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ temperature: { max: 1.5 } }),
        );
    });

    it('offers clamp/reject enforce options and writes the selection', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        const sel = control('Enforce Mode', 'select', 0) as HTMLSelectElement;
        const values = Array.from(sel.options).map((o) => o.value);
        expect(values).toEqual(expect.arrayContaining(['', 'clamp', 'reject']));
        const labels = Array.from(sel.options).map((o) => o.textContent);
        expect(labels).toEqual(
            expect.arrayContaining(['Clamp (adjust to limit)', 'Reject (block request)']),
        );
        fireEvent.change(sel, { target: { value: 'reject' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ temperature: { enforce: 'reject' } }),
        );
    });

    it('preserves the other section field when one temperature field changes', () => {
        const onChange = vi.fn();
        const initial: LLMGovernance = { temperature: { enforce: 'clamp' } };
        render(<LLMGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.change(control('Max Temperature', 'input'), { target: { value: '0.9' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ temperature: { enforce: 'clamp', max: 0.9 } }),
        );
    });

    it('clearing a number sets it undefined and keeps siblings (no delete)', () => {
        const onChange = vi.fn();
        const initial: LLMGovernance = { temperature: { max: 1.2, enforce: 'clamp' } };
        render(<LLMGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.change(control('Max Temperature', 'input'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0] as LLMGovernance;
        expect(next.temperature!.enforce).toBe('clamp');
        expect(next.temperature!.max).toBeUndefined();
    });
});

describe('LLMGovernanceForm — context window', () => {
    it('writes context_window.max_input_tokens', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Input Tokens', 'input'), { target: { value: '8000' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ context_window: { max_input_tokens: 8000 } }),
        );
    });

    it('writes context_window.max_output_tokens', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Output Tokens', 'input'), { target: { value: '2000' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ context_window: { max_output_tokens: 2000 } }),
        );
    });

    it('writes context_window.enforce via the second Enforce Mode select', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Enforce Mode', 'select', 1), { target: { value: 'clamp' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ context_window: { enforce: 'clamp' } }),
        );
    });

    it('preserves temperature when a context_window field changes', () => {
        const onChange = vi.fn();
        const initial: LLMGovernance = { temperature: { max: 1 } };
        render(<LLMGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.change(control('Max Input Tokens', 'input'), { target: { value: '5000' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                temperature: { max: 1 },
                context_window: { max_input_tokens: 5000 },
            }),
        );
    });
});

describe('LLMGovernanceForm — tool use', () => {
    it('offers enabled and disabled as the access, no approval, and writes the selection', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        const sel = control('Access', 'select') as HTMLSelectElement;
        const values = Array.from(sel.options).map((o) => o.value);
        expect(values).toEqual(['', 'enabled', 'disabled']);
        fireEvent.change(sel, { target: { value: 'disabled' } });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ tool_use: { access: 'disabled' } }),
        );
    });

    it('says that a model\'s tool calls are not held for approval, and where an approval goes', () => {
        render(<LLMGovernanceForm value={{}} onChange={vi.fn()} />);
        expect(screen.getByTestId('tool-use-no-approval')).toHaveTextContent(
            'A model\'s tool calls are not held for approval. To have a person approve a call, put the approval on the tool itself: an MCP capability, a skill, or an argument policy on the tool\'s own surface.',
        );
    });

    it('writes tool_use.max_tool_calls_per_request', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Tool Calls Per Request', 'input'), {
            target: { value: '5' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ tool_use: { max_tool_calls_per_request: 5 } }),
        );
    });

    it('writes tool_use.max_tool_calls_per_hour', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Tool Calls Per Hour', 'input'), {
            target: { value: '100' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ tool_use: { max_tool_calls_per_hour: 100 } }),
        );
    });

    it('writes tool_use.max_tool_calls_per_day', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Max Tool Calls Per Day', 'input'), {
            target: { value: '1000' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ tool_use: { max_tool_calls_per_day: 1000 } }),
        );
    });

    it('parses Allowed Tools textarea one item per line', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Allowed Tools', 'textarea'), {
            target: { value: 'search\nfetch\nwrite' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                tool_use: { allowed_tools: ['search', 'fetch', 'write'] },
            }),
        );
    });

    it('parses Blocked Tools textarea one item per line', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Blocked Tools', 'textarea'), {
            target: { value: 'delete\nshell' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({ tool_use: { blocked_tools: ['delete', 'shell'] } }),
        );
    });

    it('parses Banned Argument Patterns textarea one item per line', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Banned Argument Patterns', 'textarea'), {
            target: { value: 'rm -rf\n.*password.*' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                tool_use: { banned_argument_patterns: ['rm -rf', '.*password.*'] },
            }),
        );
    });

    it('clearing a textarea list sets it undefined', () => {
        const onChange = vi.fn();
        const initial: LLMGovernance = { tool_use: { allowed_tools: ['a', 'b'] } };
        render(<LLMGovernanceForm value={initial} onChange={onChange} />);
        fireEvent.change(control('Allowed Tools', 'textarea'), { target: { value: '' } });
        const next = onChange.mock.calls[0][0] as LLMGovernance;
        expect(next.tool_use!.allowed_tools).toBeUndefined();
    });
});

describe('LLMGovernanceForm — system prompt', () => {
    it('parses a [prepend]-prefixed fragment line into a positioned fragment', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Mandatory Fragments', 'textarea'), {
            target: { value: '[prepend] Always answer in English.' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                system_prompt: {
                    mandatory_fragments: [
                        { position: 'prepend', text: 'Always answer in English.' },
                    ],
                },
            }),
        );
    });

    it('defaults a bare fragment line to append', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Mandatory Fragments', 'textarea'), {
            target: { value: 'Cite your sources.' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                system_prompt: {
                    mandatory_fragments: [{ position: 'append', text: 'Cite your sources.' }],
                },
            }),
        );
    });

    it('parses multiple fragments with mixed positions', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Mandatory Fragments', 'textarea'), {
            target: { value: '[prepend] Rule one.\n[append] Rule two.' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                system_prompt: {
                    mandatory_fragments: [
                        { position: 'prepend', text: 'Rule one.' },
                        { position: 'append', text: 'Rule two.' },
                    ],
                },
            }),
        );
    });

    it('round-trips existing fragments back into the textarea display', () => {
        const onChange = vi.fn();
        const initial: LLMGovernance = {
            system_prompt: {
                mandatory_fragments: [{ position: 'prepend', text: 'Hello' }],
            },
        };
        render(<LLMGovernanceForm value={initial} onChange={onChange} />);
        const ta = control('Mandatory Fragments', 'textarea') as HTMLTextAreaElement;
        expect(ta.value).toBe('[prepend] Hello');
    });

    it('parses Banned Patterns textarea one item per line', () => {
        const onChange = vi.fn();
        render(<LLMGovernanceForm value={{}} onChange={onChange} />);
        fireEvent.change(control('Banned Patterns', 'textarea'), {
            target: { value: 'ignore previous\njailbreak' },
        });
        expect(onChange).toHaveBeenCalledWith(
            expect.objectContaining({
                system_prompt: { banned_patterns: ['ignore previous', 'jailbreak'] },
            }),
        );
    });
});
