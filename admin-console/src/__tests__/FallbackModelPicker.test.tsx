/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * FallbackModelPicker — the orderable model-fallback chain picker used by the
 * "Fallbacks" tab of the LLM model form (all modes). Pins the contract:
 *   (1) selected models render in priority order with numbered badges;
 *   (2) the dropdown only offers models not already selected;
 *   (3) add / reorder (↑/↓) / remove each emit the exact new ordered id list
 *       through onChange — that array is what the API persists as the
 *       model→model failover chain.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { FallbackModelPicker } from '../components/llm/config/LLModelForm';

const OPTIONS = [
    { id: 'm-a', name: 'Model A' },
    { id: 'm-b', name: 'Model B' },
    { id: 'm-c', name: 'Model C' },
];

function renderPicker(selected: string[], onChange = vi.fn()) {
    render(
        <FallbackModelPicker
            label="Fallback models"
            hint="ordered chain"
            selected={selected}
            onChange={onChange}
            options={OPTIONS}
        />,
    );
    return onChange;
}

describe('FallbackModelPicker', () => {
    it('renders selected models in priority order with numbered badges', () => {
        renderPicker(['m-a', 'm-b']);
        expect(screen.getByText('Model A')).toBeInTheDocument();
        expect(screen.getByText('Model B')).toBeInTheDocument();
        // priority badges (top = first fallback tried)
        expect(screen.getByText('1')).toBeInTheDocument();
        expect(screen.getByText('2')).toBeInTheDocument();
    });

    it('only offers models that are not already selected', () => {
        renderPicker(['m-a', 'm-b']);
        // one candidate left (Model C)
        fireEvent.click(screen.getByText(/Add fallback model \(1 available\)/));
        expect(screen.getByText('Model C')).toBeInTheDocument();
    });

    it('appends a chosen model to the end of the chain', () => {
        const onChange = renderPicker(['m-a']);
        fireEvent.click(screen.getByText(/Add fallback model/));
        fireEvent.click(screen.getByText('Model C'));
        expect(onChange).toHaveBeenCalledWith(['m-a', 'm-c']);
    });

    it('reorders with move-up (raises priority)', () => {
        const onChange = renderPicker(['m-a', 'm-b']);
        // second chip (Model B) move-up → B before A
        const ups = screen.getAllByTitle('Move up (higher priority)');
        fireEvent.click(ups[1]);
        expect(onChange).toHaveBeenCalledWith(['m-b', 'm-a']);
    });

    it('reorders with move-down (lowers priority)', () => {
        const onChange = renderPicker(['m-a', 'm-b']);
        const downs = screen.getAllByTitle('Move down (lower priority)');
        fireEvent.click(downs[0]); // Model A down → A after B
        expect(onChange).toHaveBeenCalledWith(['m-b', 'm-a']);
    });

    it('removes a model from the chain', () => {
        const onChange = renderPicker(['m-a', 'm-b']);
        const removes = screen.getAllByTitle('Remove');
        fireEvent.click(removes[0]); // remove Model A
        expect(onChange).toHaveBeenCalledWith(['m-b']);
    });

    it('shows the empty-state hint when no other models exist', () => {
        render(
            <FallbackModelPicker
                label="Fallback models"
                hint="ordered chain"
                selected={[]}
                onChange={vi.fn()}
                options={[]}
            />,
        );
        expect(screen.getByText(/No other models of this mode available/)).toBeInTheDocument();
    });
});
