/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two lifecycle diagrams.
 *
 * Their value is entirely in being ACCURATE — a wrong gate list is worse than
 * none — so the stage rail's requirement labels are pinned against the
 * control plane's `DEFAULT_REQUIREMENTS`, and the contract diagram pins the
 * one sentence that resolves the confusion it exists for.
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ContractStatesDiagram, StageDiagram } from '../ContractLifecycleDiagram';
import { backwardTransition, forwardTransition } from '../../../lib/lifecycleGates';
import type { TransitionOption } from '../../../lib/aiSystemTypes';

const approvedGate: TransitionOption = {
    to_stage: 'approved',
    allowed: false,
    requirements: [
        { key: 'contract_minted', label: 'A contract has been minted', satisfied: true },
        { key: 'owner_set', label: 'An accountable owner is named', satisfied: false },
    ],
    unmet: [{ key: 'owner_set', label: 'An accountable owner is named', satisfied: false }],
};

describe('StageDiagram', () => {
    it('shows every lifecycle stage in order', () => {
        render(<StageDiagram />);
        for (const stage of ['proposed', 'approved', 'active', 'deprecated', 'retired']) {
            expect(screen.getByTestId(`stage-${stage}`)).toBeInTheDocument();
        }
    });

    it('lists each default gate against the control plane requirement keys', () => {
        // Mirrors DEFAULT_REQUIREMENTS in ai_system_lifecycle_gate.py.
        render(<StageDiagram />);
        expect(screen.getAllByText('contract minted').length).toBeGreaterThan(0);
        expect(screen.getByText('owner set')).toBeInTheDocument();
        expect(screen.getByText('intended use')).toBeInTheDocument();
        expect(screen.getByText('approver recorded')).toBeInTheDocument();
        expect(screen.getByText('liveness set')).toBeInTheDocument();
        expect(screen.getByText('REPLAY PASSED')).toBeInTheDocument();
        expect(screen.getByText('successor named')).toBeInTheDocument();
        expect(screen.getByText('no recent runs')).toBeInTheDocument();
    });

    it('marks the current stage and makes only its gate live', () => {
        render(<StageDiagram currentStage="proposed" transitions={[approvedGate]} />);
        expect(screen.getByTestId('stage-proposed')).toHaveAttribute('data-current', 'true');
        expect(screen.getByTestId('stage-active')).toHaveAttribute('data-current', 'false');
        expect(screen.getByTestId('gate-approved')).toHaveAttribute('data-live', 'true');
        expect(screen.getByTestId('gate-deprecated')).toHaveAttribute('data-live', 'false');
        // The live gate reads the API's checklist, met and unmet marked.
        expect(screen.getByText('✓ contract minted')).toBeInTheDocument();
        expect(screen.getByText('✗ owner set')).toBeInTheDocument();
    });

    it('offers the operation for an unmet requirement and reports the click', () => {
        const onAction = vi.fn();
        render(<StageDiagram currentStage="proposed" transitions={[approvedGate]} onAction={onAction} />);
        fireEvent.click(screen.getByTestId('gate-action-owner_set'));
        expect(onAction).toHaveBeenCalledWith('owner_set', 'approved');
        expect(screen.queryByTestId('gate-action-contract_minted')).not.toBeInTheDocument();
    });

    it('finds the forward and backward transitions from the stage order', () => {
        const back: TransitionOption = { to_stage: 'proposed', allowed: true, requirements: [], unmet: [] };
        const fwd: TransitionOption = { to_stage: 'active', allowed: false, requirements: [], unmet: [] };
        expect(forwardTransition('approved', [back, fwd])).toBe(fwd);
        expect(backwardTransition('approved', [back, fwd])).toBe(back);
        expect(forwardTransition('retired', [back])).toBeUndefined();
        expect(backwardTransition('proposed', [])).toBeUndefined();
    });

    it('states the EU AI Act floor, and says when it applies', () => {
        const { rerender } = render(<StageDiagram riskTier="minimal" />);
        expect(screen.getByTestId('high-risk-floor')).toHaveAttribute('data-applies', 'false');
        rerender(<StageDiagram riskTier="high" />);
        expect(screen.getByTestId('high-risk-floor')).toHaveAttribute('data-applies', 'true');
        expect(screen.getByText(/unexpired impact assessment at proposed → approved/i)).toBeInTheDocument();
    });
});

describe('ContractStatesDiagram', () => {
    it('says in its own title that promoting is not a lifecycle transition', () => {
        // The sentence lives on the toggle, not in an explainer box of its own.
        render(<ContractStatesDiagram />);
        expect(screen.getByTestId('contract-states-toggle')).toHaveTextContent(
            /not a lifecycle transition/i,
        );
        expect(screen.queryByText(/promote\(\) checks one thing/i)).not.toBeInTheDocument();
    });

    it('shows the contract states and what each button does', () => {
        render(<ContractStatesDiagram />);
        for (const t of ['draft', 'active', 'superseded', 'rejected']) {
            expect(screen.getByText(t)).toBeInTheDocument();
        }
        expect(screen.getByText(/new version — freeze the live config/)).toBeInTheDocument();
        expect(screen.getByText(/approve — record who signed it/)).toBeInTheDocument();
        expect(screen.getByText(/promote — make it the governing version/)).toBeInTheDocument();
    });

    it('collapses and expands', () => {
        render(<ContractStatesDiagram />);
        fireEvent.click(screen.getByTestId('contract-states-toggle'));
        expect(screen.queryByText('superseded')).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId('contract-states-toggle'));
        expect(screen.getByText('superseded')).toBeInTheDocument();
    });
});
