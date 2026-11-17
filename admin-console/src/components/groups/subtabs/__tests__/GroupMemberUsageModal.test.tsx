/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-member usage drilldown.
 *
 * Pinned: the request is scoped to this group, this member (as a number, because
 * usage_logs.user_id is an integer) and the chosen period; a non-numeric id never
 * reaches the server; a failure shows its message rather than "[object Object]";
 * and each surface's panel appears only when it has activity.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gateway = { fetchGroupUsage: vi.fn() };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../hooks/useTimespanPreference', async () => {
    const { useState } = await vi.importActual<typeof import('react')>('react');
    return { useTimespanPreference: (_supported: unknown, fallback: string) => useState(fallback) };
});

import GroupMemberUsageModal from '../GroupMemberUsageModal';

const onClose = vi.fn();

const USAGE = {
    summary: { total_requests: 1234, total_cost: 0.5, total_errors: 2, error_rate: 1.5 },
    llm: {
        request_count: 1,
        total_tokens: 9000,
        avg_tokens_per_request: 300,
        avg_response_time_ms: 1500,
        total_cost: 0.25,
        top_models: [{ model_name: 'gpt-5', provider: 'openai', request_count: 7, total_tokens: 70, total_cost: 0.005 }],
    },
    mcp: {
        request_count: 3,
        total_tool_calls: 3,
        avg_execution_time_ms: 40,
        error_count: 1,
        top_tools: [{ tool_name: 'search', server_name: 'github', call_count: 3, error_count: 1 }],
    },
    a2a: {
        request_count: 2,
        avg_duration_ms: 0,
        top_cards: [{ card_id: 'c1', card_name: 'scout', request_count: 2, error_count: 0 }],
    },
    media: {
        request_count: 4,
        estimated_cost_usd: 1.5,
        modes: {
            image_generation: { request_count: 1, images_generated: 1, cost_usd: 0.1 },
            audio_speech: { request_count: 1, audio_chars: 500, cost_usd: 0.1 },
            audio_transcription: { request_count: 1, audio_seconds_input: 2.5, cost_usd: 0.1 },
            video_generation: { request_count: 1, video_seconds_output: 3.5, cost_usd: 0.1 },
        },
    },
};

const renderModal = (userId = '42') =>
    render(
        <GroupMemberUsageModal
            groupId="rg-1"
            groupName="Eng"
            userId={userId}
            username="alice"
            displayName="Alice L"
            onClose={onClose}
        />,
    );
/** The summary tile, which precedes any table column of the same name. */
const tile = (label: string) => screen.getAllByText(label)[0].parentElement?.parentElement as HTMLElement;

describe('GroupMemberUsageModal', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gateway.fetchGroupUsage.mockReset();
        gateway.fetchGroupUsage.mockResolvedValue(USAGE);
    });

    afterEach(cleanup);

    it('asks for this member in this group as a number, on the default period', async () => {
        renderModal();
        expect(screen.getByText('Usage — Alice L')).toBeInTheDocument();
        expect(screen.getByText('@alice · in group Eng')).toBeInTheDocument();
        await waitFor(() =>
            expect(gateway.fetchGroupUsage).toHaveBeenCalledWith('rg-1', { period: '30d', user_id: 42 }),
        );
    });

    it('changing the period re-queries', async () => {
        renderModal();
        await waitFor(() => expect(gateway.fetchGroupUsage).toHaveBeenCalledTimes(1));
        fireEvent.click(screen.getByRole('button', { name: '7d' }));
        await waitFor(() =>
            expect(gateway.fetchGroupUsage).toHaveBeenLastCalledWith('rg-1', { period: '7d', user_id: 42 }),
        );
    });

    it.each(['abc', '0', '-3'])('never sends the non-usable id %s', async (id) => {
        renderModal(id);
        expect(await screen.findByText('User id is not numeric — usage rollup unavailable.')).toBeInTheDocument();
        expect(gateway.fetchGroupUsage).not.toHaveBeenCalled();
    });

    it('shows the summary and a panel per active surface', async () => {
        renderModal();
        await screen.findByText('Requests');
        expect(tile('Requests')).toHaveTextContent((1234).toLocaleString());
        expect(tile('Total cost')).toHaveTextContent('$0.5000');
        expect(tile('Errors')).toHaveTextContent('1.5% error rate');
        expect(tile('Tokens (LLM)')).toHaveTextContent('avg 300/req');

        expect(screen.getByText('LLM (1 request)')).toBeInTheDocument();
        expect(screen.getByText('avg 1.5s · $0.2500')).toBeInTheDocument();
        expect(screen.getByText('gpt-5')).toBeInTheDocument();
        expect(screen.getByText('MCP (3 tool calls)')).toBeInTheDocument();
        expect(screen.getByText('avg 40ms · 1 error')).toBeInTheDocument();
        expect(screen.getByText('search')).toBeInTheDocument();
        expect(screen.getByText('A2A (2 requests)')).toBeInTheDocument();
        expect(screen.getByText('avg 0ms')).toBeInTheDocument();
        expect(screen.getByText('Media (4 requests)')).toBeInTheDocument();
        expect(screen.getByText('$1.50 · already counted in LLM total')).toBeInTheDocument();
        expect(screen.getByText('1 req · 1 image')).toBeInTheDocument();
        expect(screen.getByText('1 req · 500 chars')).toBeInTheDocument();
        expect(screen.getByText('1 req · 2.5s in')).toBeInTheDocument();
        expect(screen.getByText('1 req · 3.5s out')).toBeInTheDocument();
    });

    it('hides a surface with no activity', async () => {
        gateway.fetchGroupUsage.mockResolvedValue({
            ...USAGE,
            mcp: { ...USAGE.mcp, request_count: 0 },
            a2a: { ...USAGE.a2a, request_count: 0 },
            media: { ...USAGE.media, request_count: 0 },
        });
        renderModal();
        await screen.findByText(/^LLM \(/);
        expect(screen.queryByText(/^MCP \(/)).toBeNull();
        expect(screen.queryByText(/^A2A \(/)).toBeNull();
        expect(screen.queryByText(/^Media \(/)).toBeNull();
    });

    it('says so when the member did nothing in the period', async () => {
        gateway.fetchGroupUsage.mockResolvedValue({ summary: { total_requests: 0, total_cost: 0, total_errors: 0, error_rate: 0 } });
        renderModal();
        expect(await screen.findByText(/No activity in the last 30d/)).toBeInTheDocument();
    });

    it('shows a failure message, including for a non-Error rejection, and Refresh retries', async () => {
        gateway.fetchGroupUsage.mockRejectedValueOnce({ detail: 'forbidden' });
        renderModal();
        expect(await screen.findByText('forbidden')).toBeInTheDocument();
        expect(screen.queryByText('[object Object]')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
        expect(await screen.findByText('Requests')).toBeInTheDocument();
        expect(screen.queryByText('forbidden')).toBeNull();
    });

    it('closes by button and by Escape', async () => {
        renderModal();
        await screen.findByText('Requests');
        // The header X and the footer button.
        for (const button of screen.getAllByRole('button', { name: 'Close' })) fireEvent.click(button);
        expect(onClose).toHaveBeenCalledTimes(2);
        fireEvent.keyDown(window, { key: 'Escape' });
        expect(onClose).toHaveBeenCalledTimes(3);
        fireEvent.keyDown(window, { key: 'a' });
        expect(onClose).toHaveBeenCalledTimes(3);
    });
});
