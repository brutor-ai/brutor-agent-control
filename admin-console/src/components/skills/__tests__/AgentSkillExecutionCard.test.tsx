/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An execution on a skill's Executions tab.
 *
 * A skill that needs approval is held in Mission Control → Approvals, and an
 * execution exists only once it runs: there is no execution that waits for a
 * person. The Control Plane still carries `approval_status` on an execution;
 * the card shows what the execution did and nothing about an approval,
 * whatever that field holds, so that no execution reads as something to act
 * on here.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApprovalStatus, ExecutionSummary } from '../../../lib/agentSkillTypes';
import { ExecutionCard } from '../AgentSkillExecutionCard';

const execution = (over: Partial<ExecutionSummary> = {}): ExecutionSummary => ({
    id: '01JEXECUTION0000000000000A',
    execution_status: 'completed',
    approval_status: 'not_required',
    started_at: '2026-11-10T12:00:00Z',
    duration_ms: 840,
    total_tokens: 1200,
    estimated_cost_usd: 0.0123,
    ...over,
});

describe('ExecutionCard', () => {
    it('shows what the execution did: its outcome, its id, how long it took and what it cost', () => {
        const { container } = render(<ExecutionCard execution={execution()} />);
        expect(screen.getByText('Completed')).toBeInTheDocument();
        expect(screen.getByText('01JEXECUTION0000...')).toBeInTheDocument();
        expect(container).toHaveTextContent('840ms');
        expect(container).toHaveTextContent('1,200 tokens');
        expect(container).toHaveTextContent('$0.0123');
    });

    it('shows why a failed execution failed', () => {
        render(<ExecutionCard execution={execution({ execution_status: 'failed', error_message: 'tool budget exceeded' })} />);
        expect(screen.getByText('Failed')).toBeInTheDocument();
        expect(screen.getByText('tool budget exceeded')).toBeInTheDocument();
    });

    it.each<ApprovalStatus>(['not_required', 'pending', 'approved', 'rejected', 'expired'])(
        'says nothing about an approval when the stored approval status is %s',
        (approval_status) => {
            const { container } = render(<ExecutionCard execution={execution({ execution_status: 'running', approval_status })} />);
            expect(screen.getByText('Running')).toBeInTheDocument();
            expect(container).not.toHaveTextContent(/approv|awaiting|auto|rejected|expired/i);
        },
    );
});
