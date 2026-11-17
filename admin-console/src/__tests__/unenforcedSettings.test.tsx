/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings the gateway does not act on in this release are not offered.
 *
 * Each form of a group's limits or governance leaves out the inputs of the
 * settings the Control Plane refuses for that reason, keeps the ones the
 * gateway acts on, shows a value stored before read-only ("not available in
 * this release"), and hands on, at the next change, a document without it.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import MCPLimitsForm from '../components/groups/subtabs/MCPLimitsForm';
import MediaLimitsForm from '../components/groups/subtabs/MediaLimitsForm';
import A2ALimitsForm from '../components/groups/subtabs/A2ALimitsForm';
import SkillGovernanceForm from '../components/groups/subtabs/SkillGovernanceForm';
import MediaGovernanceForm from '../components/groups/subtabs/MediaGovernanceForm';
import LLMGovernanceForm from '../components/groups/subtabs/LLMGovernanceForm';
import { storedUnavailable, withoutUnavailable } from '../lib/unavailableSettings';

const lastChange = (onChange: ReturnType<typeof vi.fn>) => onChange.mock.calls[onChange.mock.calls.length - 1][0] as Record<string, unknown>;

describe('the helper', () => {
    const settings = [{ path: ['execution', 'memory_limit_mb'], label: 'Memory' }, { path: ['data', 'max_rows_per_query'], label: 'Rows' }];
    it('finds what a document holds, and takes it out, an emptied section with it', () => {
        const doc = { execution: { memory_limit_mb: 512, timeout_seconds: 30 }, data: { max_rows_per_query: 10 } };
        expect(storedUnavailable(doc, settings).map((s) => [s.label, s.value])).toEqual([['Memory', 512], ['Rows', 10]]);
        expect(withoutUnavailable(doc, settings)).toEqual({ execution: { timeout_seconds: 30 } });
        expect(doc.execution.memory_limit_mb).toBe(512);
    });
});

describe('MCP limits', () => {
    const stored = {
        execution: { timeout_seconds: 30, memory_limit_mb: 512, cpu_limit_percent: 50, max_concurrency_per_user: 2 },
        data: { max_payload_size_mb: 4, max_context_window_percent: 20, max_rows_per_query: 100, max_items_per_response: 50 },
    };

    it('offers timeout, concurrency and payload; not memory, CPU, context window, rows or items', () => {
        render(<MCPLimitsForm value={{}} onChange={vi.fn()} />);
        for (const label of ['Timeout (seconds)', 'Max Concurrency (per user)', 'Max Concurrency (per group)', 'Max Payload Size (MB)']) {
            expect(screen.getByText(label)).toBeInTheDocument();
        }
        for (const label of ['Memory Limit (MB)', 'CPU Limit (%)', 'Max Context Window (%)', 'Max Rows per Query', 'Max Items per Response']) {
            expect(screen.queryByText(label)).toBeNull();
        }
    });

    it('shows a stored one read-only, and hands on a document without them at the next change', () => {
        const onChange = vi.fn();
        render(<MCPLimitsForm value={stored} onChange={onChange} />);
        expect(screen.getByTestId('unavailable-execution.memory_limit_mb')).toHaveTextContent('512 — not available in this release');
        expect(screen.getByTestId('unavailable-data.max_items_per_response')).toHaveTextContent('50');
        fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '45' } });
        const next = lastChange(onChange);
        expect(next).toEqual({ execution: { timeout_seconds: 45, max_concurrency_per_user: 2 }, data: { max_payload_size_mb: 4 } });
    });
});

describe('media limits', () => {
    it('offers the daily images, and no monthly limit', () => {
        render(<MediaLimitsForm mode="image_generation" value={{}} onChange={vi.fn()} />);
        expect(screen.queryByText(/monthly/i)).toBeNull();
        expect(screen.getByText('Daily images')).toBeInTheDocument();
    });

    it('offers no limits of embeddings, which the group\'s LLM limits bound', () => {
        render(<MediaLimitsForm mode="embedding" value={{}} onChange={vi.fn()} />);
        expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
        expect(screen.getByTestId('media-limits-embedding-unavailable')).toBeInTheDocument();
    });

    it('shows a stored monthly limit read-only and leaves it out at the next change', () => {
        const onChange = vi.fn();
        render(
            <MediaLimitsForm mode="image_generation" value={{ image_generation: { daily_images: 10, monthly_images: 100 } }} onChange={onChange} />,
        );
        expect(screen.getByTestId('unavailable-image_generation.monthly_images')).toHaveTextContent('100');
        fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '12' } });
        expect(JSON.stringify(lastChange(onChange))).not.toContain('monthly_images');
    });
});

describe('A2A limits', () => {
    it('offers no warning percentages', () => {
        render(<A2ALimitsForm value={{}} onChange={vi.fn()} />);
        expect(screen.queryByText(/warning/i)).toBeNull();
    });
});

describe('skill governance', () => {
    // The Control Plane takes the cap and the gateway hands it to the agent
    // with the skill; nothing counts the tool calls of an execution, so the
    // form says it is advisory.
    it('offers the cap on the tool calls of one execution, labelled advisory, and hands it on', () => {
        const onChange = vi.fn();
        render(<SkillGovernanceForm value={{ max_tool_calls_per_execution: 7 }} onChange={onChange} />);
        expect(screen.queryByTestId('unavailable-max_tool_calls_per_execution')).toBeNull();
        const input = screen.getByLabelText(/Max Tool Calls per Execution \(advisory\)/);
        expect(input).toHaveValue(7);
        expect(screen.getByTestId('skill-max-tool-calls-advisory')).toHaveTextContent(
            'Advisory: the gateway hands this number to the agent with the skill, and the agent is expected to stop after it; the gateway does not count the tool calls of an execution.',
        );
        fireEvent.change(input, { target: { value: '9' } });
        expect(lastChange(onChange)).toEqual({ max_tool_calls_per_execution: 9 });
    });
});

describe('media governance', () => {
    it('offers no cap on the tokens of one embedding input', () => {
        render(<MediaGovernanceForm mode="embedding" value={{}} onChange={vi.fn()} />);
        expect(screen.queryByText(/tokens per item/i)).toBeNull();
    });

    it('offers no switch for the guardrail of a transcription prompt, which always runs', () => {
        render(<MediaGovernanceForm mode="audio_transcription" value={{}} onChange={vi.fn()} />);
        expect(screen.queryByText(/prompt guardrail/i)).toBeNull();
    });
});

describe('LLM governance: the context window', () => {
    const options = () =>
        Array.from((screen.getByTestId('governance-input-context_window.enforce') as HTMLSelectElement).options).map((o) => o.value);

    it('offers clamp for an output ceiling alone', () => {
        render(<LLMGovernanceForm value={{ context_window: { max_output_tokens: 1000 } }} onChange={vi.fn()} />);
        expect(options()).toEqual(['', 'clamp', 'reject']);
    });

    it('offers no clamp with an input ceiling: the gateway cannot cut a request\'s input', () => {
        render(<LLMGovernanceForm value={{ context_window: { max_input_tokens: 8000 } }} onChange={vi.fn()} />);
        expect(options()).toEqual(['', 'reject']);
    });

    it('says a stored clamp with an input ceiling is not available in this release', () => {
        render(<LLMGovernanceForm value={{ context_window: { max_input_tokens: 8000, enforce: 'clamp' } }} onChange={vi.fn()} />);
        expect(screen.getByTestId('context-window-clamp-unavailable')).toHaveTextContent('not available in this release');
    });
});
