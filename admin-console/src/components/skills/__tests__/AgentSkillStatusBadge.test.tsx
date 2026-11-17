/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two skill badges are where an operator reads a skill's lifecycle stage
 * and an execution's outcome. They are small, but they are load-bearing:
 * "Published" vs "Draft" decides whether the skill is executable at all.
 *
 * Each badge indexes a config map by a backend-supplied string. The tests that
 * matter are therefore: the right label for each known value, and a sane
 * fallback when the backend sends a value this build has never heard of —
 * a bare map lookup would render `undefined` or throw on `.label`.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import AgentSkillStatusBadge, { AgentSkillExecutionStatusBadge } from '../AgentSkillStatusBadge';
import type { SkillStatus, ExecutionStatus } from '../../../lib/agentSkillTypes';

describe('AgentSkillStatusBadge — lifecycle', () => {
    it.each([
        ['draft', 'Draft'],
        ['validated', 'Validated'],
        ['published', 'Published'],
        ['deprecated', 'Deprecated'],
        ['archived', 'Archived'],
    ])('labels %s as "%s"', (status, label) => {
        render(<AgentSkillStatusBadge status={status as SkillStatus} />);
        expect(screen.getByText(label)).toBeInTheDocument();
    });

    it('carries the stage meaning as a tooltip, not just a colour', () => {
        // Colour alone does not tell an operator that a deprecated skill still
        // runs. The description has to be reachable.
        render(<AgentSkillStatusBadge status="deprecated" />);
        expect(screen.getByTitle('Superseded, still executable')).toBeInTheDocument();
    });

    it('falls back to Draft for a status this build does not know', () => {
        render(<AgentSkillStatusBadge status={'quarantined' as SkillStatus} />);
        expect(screen.getByText('Draft')).toBeInTheDocument();
    });

    it('renders larger text at md than at sm', () => {
        const { container: small } = render(<AgentSkillStatusBadge status="published" />);
        const { container: medium } = render(
            <AgentSkillStatusBadge status="published" size="md" />
        );
        expect(small.querySelector('span')).toHaveClass('text-xs');
        expect(medium.querySelector('span')).toHaveClass('text-sm');
    });
});

describe('AgentSkillExecutionStatusBadge — run outcome', () => {
    it.each([
        ['pending', 'Pending'],
        ['running', 'Running'],
        ['completed', 'Completed'],
        ['failed', 'Failed'],
        ['cancelled', 'Cancelled'],
        ['timed_out', 'Timed Out'],
    ])('labels %s as "%s"', (status, label) => {
        render(
            <AgentSkillExecutionStatusBadge status={status as ExecutionStatus} />
        );
        expect(screen.getByText(label)).toBeInTheDocument();
    });

    it('distinguishes a timeout from an ordinary failure', () => {
        // These are the same colour but a different diagnosis: one is the
        // skill's fault, the other is the budget's.
        render(<AgentSkillExecutionStatusBadge status="timed_out" />);
        expect(screen.getByText('Timed Out')).toBeInTheDocument();
        expect(screen.queryByText('Failed')).not.toBeInTheDocument();
    });

    it('falls back to Pending for an unknown execution status', () => {
        render(
            <AgentSkillExecutionStatusBadge status={'queued' as ExecutionStatus} />
        );
        expect(screen.getByText('Pending')).toBeInTheDocument();
    });
});
