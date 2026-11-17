/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-model LLM limits dialog.
 *
 * Pinned: edits go to this group and model, the form is told the group-wide
 * limits so it can show min(group, per-model), Clear removes the row instead of
 * saving zeroes, and a failure neither claims success nor closes the dialog.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const gateway = { fetchLLMModelLimits: vi.fn(), updateLLMModelLimits: vi.fn(), clearLLMModelLimits: vi.fn() };
const toast = { success: vi.fn(), error: vi.fn() };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../LLMLimitsForm', () => ({
    default: (p: { limits: unknown; globalLimits: unknown; mode: string; onChange: (v: unknown) => void }) => (
        <div>
            <pre data-testid="limits">{JSON.stringify(p.limits)}</pre>
            <pre data-testid="global">{JSON.stringify(p.globalLimits)}</pre>
            <span data-testid="mode">{p.mode}</span>
            <button onClick={() => p.onChange({ monthly_budget: 5 })}>edit</button>
        </div>
    ),
}));

import LLMModelLimitsDialog from '../LLMModelLimitsDialog';

const onClose = vi.fn();
const onSave = vi.fn();
const MODEL = { id: 'm1', name: 'gpt-5' };

async function open(model: { id: string; name?: string; model_name?: string } = MODEL) {
    render(<LLMModelLimitsDialog isOpen onClose={onClose} onSave={onSave} groupId="rg-1" groupName="Eng" model={model} />);
    await screen.findByTestId('limits');
}

describe('LLMModelLimitsDialog', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        for (const fn of Object.values(gateway)) fn.mockReset();
        gateway.fetchLLMModelLimits.mockResolvedValue({
            limits: { monthly_budget: 9 },
            effective_global_limits: { monthly_budget: 100 },
        });
        gateway.updateLLMModelLimits.mockResolvedValue({});
        gateway.clearLLMModelLimits.mockResolvedValue({});
    });

    afterEach(cleanup);

    it('renders and loads nothing while closed', () => {
        const { container } = render(
            <LLMModelLimitsDialog isOpen={false} onClose={onClose} groupId="rg-1" groupName="Eng" model={MODEL} />,
        );
        expect(container).toBeEmptyDOMElement();
        expect(gateway.fetchLLMModelLimits).not.toHaveBeenCalled();
    });

    it('feeds the form the row and the group-wide limits, in model mode', async () => {
        await open();
        expect(gateway.fetchLLMModelLimits).toHaveBeenCalledWith('rg-1', 'm1');
        expect(screen.getByTestId('limits')).toHaveTextContent('{"monthly_budget":9}');
        expect(screen.getByTestId('global')).toHaveTextContent('{"monthly_budget":100}');
        expect(screen.getByTestId('mode')).toHaveTextContent('model');
        expect(screen.getByText('Per-Model Limits — gpt-5')).toBeInTheDocument();
    });

    it.each([
        [{ id: 'm2', model_name: 'raw-name' }, 'raw-name'],
        [{ id: 'm3' }, 'm3'],
    ])('names the model from what it has', async (model, shown) => {
        await open(model);
        expect(screen.getByText(`Per-Model Limits — ${shown}`)).toBeInTheDocument();
    });

    it('saves the edit for this group and model, then closes', async () => {
        await open();
        fireEvent.click(screen.getByText('edit'));
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(gateway.updateLLMModelLimits).toHaveBeenCalledWith('rg-1', 'm1', { monthly_budget: 5 }),
        );
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Saved', 'Per-model LLM limits saved'));
        expect(onSave).toHaveBeenCalledTimes(1);
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('a refused save is reported and stays open', async () => {
        gateway.updateLLMModelLimits.mockRejectedValue(new Error('above the group limit'));
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() =>
            expect(toast.error).toHaveBeenCalledWith('Failed to save model limits', 'above the group limit'),
        );
        expect(onSave).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    });

    it('Clear removes the row rather than saving, then closes', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Clear all per-model limits' }));
        await waitFor(() => expect(gateway.clearLLMModelLimits).toHaveBeenCalledWith('rg-1', 'm1'));
        expect(gateway.updateLLMModelLimits).not.toHaveBeenCalled();
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Cleared', 'Per-model LLM limits removed'));
        expect(onSave).toHaveBeenCalledTimes(1);
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('a failed clear is reported and stays open', async () => {
        gateway.clearLLMModelLimits.mockRejectedValue(new Error('locked'));
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Clear all per-model limits' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to clear model limits', 'locked'));
        expect(onClose).not.toHaveBeenCalled();
    });

    it('a failed load is reported, and Cancel closes without writing', async () => {
        gateway.fetchLLMModelLimits.mockRejectedValue(new Error('gone'));
        await open();
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to load model limits', 'gone'));
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(gateway.updateLLMModelLimits).not.toHaveBeenCalled();
    });
});
