/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The owning tab around the media limits form.
 *
 * All five modalities live in ONE dict and the operator edits one at a time,
 * so the promise printed on the page — "sibling modes you don't touch are
 * preserved" — is the property this tab has to keep. Editing TTS and saving
 * must not wipe the image-generation caps.
 *
 * The dot on a mode button is the only way to see, without clicking through
 * all five, which modalities are actually governed. And "Clear all X limits"
 * has to remove the modality's key entirely rather than leave an empty
 * object behind, which reads to the control plane as a governed-but-empty
 * surface.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockFetchMediaGlobalLimits = vi.fn();
const mockUpdateMediaGlobalLimits = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchMediaGlobalLimits: mockFetchMediaGlobalLimits,
        updateMediaGlobalLimits: mockUpdateMediaGlobalLimits,
    }),
}));

import GroupMediaLimitsTab from '../GroupMediaLimitsTab';
import type { ResourceGroup } from '../../../../lib/groupTypes';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../../../../test/controlPlaneRefusal';

const GROUP = { id: 'rg-1', name: 'creative' } as ResourceGroup;

function renderTab() {
    const onUpdate = vi.fn();
    render(<GroupMediaLimitsTab group={GROUP} onUpdate={onUpdate} />);
    return { onUpdate };
}

const saveButton = () => screen.getByRole('button', { name: /^Save$/ });
const discardButton = () => screen.getByRole('button', { name: /discard/i });

beforeEach(() => {
    vi.clearAllMocks();
    mockFetchMediaGlobalLimits.mockResolvedValue({ media_global_limits: {} });
    mockUpdateMediaGlobalLimits.mockResolvedValue({});
});

describe('GroupMediaLimitsTab — loading', () => {
    it('says it is loading before showing an empty field set', () => {
        mockFetchMediaGlobalLimits.mockReturnValue(new Promise(() => { /* never settles */ }));
        renderTab();
        expect(screen.getByText('Loading…')).toBeInTheDocument();
    });

    it('reads this group’s media limits and opens on TTS', async () => {
        renderTab();
        await waitFor(() => expect(mockFetchMediaGlobalLimits).toHaveBeenCalledWith('rg-1'));
        expect(await screen.findByText('Daily input characters synthesized')).toBeInTheDocument();
    });

    it('a failed load says why', async () => {
        mockFetchMediaGlobalLimits.mockRejectedValue(new Error('media limits unavailable'));
        renderTab();
        expect(await screen.findByText('media limits unavailable')).toBeInTheDocument();
    });

    it('survives a response with no limits blob', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({});
        renderTab();
        expect(await screen.findByText('Daily input characters synthesized')).toBeInTheDocument();
    });
});

describe('GroupMediaLimitsTab — the mode picker', () => {
    it('marks which modalities are actually governed', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({
            media_global_limits: { image_generation: { daily_images: 50 } },
        });
        renderTab();
        const governed = (await screen.findByText('Image generation')).closest('button') as HTMLElement;
        const ungoverned = screen.getByText('Embeddings').closest('button') as HTMLElement;
        expect(governed.querySelector('.bg-primary')).not.toBeNull();
        expect(ungoverned.querySelector('.bg-primary')).toBeNull();
    });

    it('switching modality swaps the field set', async () => {
        renderTab();
        fireEvent.click(await screen.findByText('Image generation'));
        expect(screen.getByText('Daily images')).toBeInTheDocument();
        expect(screen.queryByText('Daily input characters synthesized')).toBeNull();
    });

    it('switching modality alone is not an edit', async () => {
        renderTab();
        fireEvent.click(await screen.findByText('Embeddings'));
        expect(saveButton()).toBeDisabled();
    });
});

describe('GroupMediaLimitsTab — editing and saving', () => {
    it('Save and Discard stay disabled until something changes', async () => {
        renderTab();
        await screen.findByText('Daily input characters synthesized');
        expect(saveButton()).toBeDisabled();
        expect(discardButton()).toBeDisabled();

        fireEvent.change(screen.getAllByPlaceholderText('(unset)')[0], { target: { value: '5' } });
        expect(saveButton()).toBeEnabled();
        expect(discardButton()).toBeEnabled();
    });

    it('an edit to one modality preserves the others on the wire', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({
            media_global_limits: { image_generation: { daily_images: 50 } },
        });
        const { onUpdate } = renderTab();
        await screen.findByText('Daily input characters synthesized');
        fireEvent.change(screen.getAllByPlaceholderText('(unset)')[0], { target: { value: '9' } });
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateMediaGlobalLimits).toHaveBeenCalledWith('rg-1', {
                image_generation: { daily_images: 50 },
                audio_speech: { daily_budget_usd: 9 },
            }),
        );
        await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    });

    it('a saved form goes quiet again', async () => {
        renderTab();
        await screen.findByText('Daily input characters synthesized');
        fireEvent.change(screen.getAllByPlaceholderText('(unset)')[0], { target: { value: '9' } });
        fireEvent.click(saveButton());
        await waitFor(() => expect(saveButton()).toBeDisabled());
    });

    it('a failed save keeps the edit so it can be retried', async () => {
        mockUpdateMediaGlobalLimits.mockRejectedValue(new Error('budget exceeds parent'));
        const { onUpdate } = renderTab();
        await screen.findByText('Daily input characters synthesized');
        fireEvent.change(screen.getAllByPlaceholderText('(unset)')[0], { target: { value: '9' } });
        fireEvent.click(saveButton());

        expect(await screen.findByText('budget exceeds parent')).toBeInTheDocument();
        expect(saveButton()).toBeEnabled();
        expect(onUpdate).not.toHaveBeenCalled();
    });

    it('discard re-reads from the server rather than trusting a local snapshot', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({
            media_global_limits: { audio_speech: { daily_budget_usd: 10 } },
        });
        renderTab();
        await screen.findByDisplayValue('10');
        fireEvent.change(screen.getByDisplayValue('10'), { target: { value: '999' } });

        mockFetchMediaGlobalLimits.mockResolvedValue({
            media_global_limits: { audio_speech: { daily_budget_usd: 42 } },
        });
        fireEvent.click(discardButton());
        await waitFor(() => expect(screen.getByDisplayValue('42')).toBeInTheDocument());
        expect(saveButton()).toBeDisabled();
    });
});

describe('GroupMediaLimitsTab — clearing a modality', () => {
    it('names the modality it will clear', async () => {
        renderTab();
        expect(await screen.findByText('Clear all TTS (audio_speech) limits')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Embeddings'));
        expect(screen.getByText('Clear all Embeddings limits')).toBeInTheDocument();
    });

    it('drops the modality’s key entirely rather than leaving an empty husk', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({
            media_global_limits: {
                audio_speech: { daily_budget_usd: 10 },
                image_generation: { daily_requests: 5 },
            },
        });
        renderTab();
        await screen.findByDisplayValue('10');
        fireEvent.click(screen.getByText('Clear all TTS (audio_speech) limits'));
        fireEvent.click(saveButton());

        await waitFor(() =>
            expect(mockUpdateMediaGlobalLimits).toHaveBeenCalledWith('rg-1', {
                image_generation: { daily_requests: 5 },
            }),
        );
    });
});

// A stored value of a setting the Control Plane now refuses is not
// sent again when another mode is edited and saved.
describe('GroupMediaLimitsTab — a stored setting the Control Plane refuses', () => {
    it('an edit of another mode sends none of it, and the save is accepted', async () => {
        mockFetchMediaGlobalLimits.mockResolvedValue({ media_global_limits: STORED_WITH_REFUSED.media_limits });
        mockUpdateMediaGlobalLimits.mockImplementation((_id: string, doc: unknown) => {
            const refusal = controlPlaneRefusal('media_limits', doc);
            return refusal ? Promise.reject(new Error(refusal)) : Promise.resolve({});
        });
        const { onUpdate } = renderTab();
        await screen.findByText('Daily input characters synthesized');
        fireEvent.change(screen.getAllByRole('spinbutton')[0], { target: { value: '500' } });
        fireEvent.click(saveButton());
        await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
        const sent = mockUpdateMediaGlobalLimits.mock.calls[0][1] as Record<string, unknown>;
        expect(controlPlaneRefusal('media_limits', sent)).toBeNull();
        expect(sent.image_generation).toEqual(STORED_KEPT.media_limits.image_generation);
        expect(sent).not.toHaveProperty('embedding');
    });
});
