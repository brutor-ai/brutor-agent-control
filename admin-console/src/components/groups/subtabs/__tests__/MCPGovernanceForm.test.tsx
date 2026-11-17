/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP default-access form.
 *
 * Governance inherits RESTRICTIVELY: a child may tighten what an ancestor
 * set, never loosen it. The API refuses a relaxation with a 400, so this
 * form's job is to predict that refusal while the operator is choosing —
 * "approval_required" under an inherited "disabled" has to light up red
 * before the save, not after it.
 *
 * The empty option matters for the same reason: "Not set" on a root group
 * means ungoverned, but under a parent it means "whatever is inherited", so
 * the option relabels itself to say which inherited value that is.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import MCPGovernanceForm from '../MCPGovernanceForm';

function renderForm(props: Partial<React.ComponentProps<typeof MCPGovernanceForm>> = {}) {
    const onChange = vi.fn();
    const view = render(<MCPGovernanceForm value={{}} onChange={onChange} {...props} />);
    return { onChange, ...view };
}

const field = (name: string) => screen.getByTestId(`governance-input-${name}`);

describe('MCPGovernanceForm — the field set', () => {
    it('governs all four capability kinds plus the approval timeout', () => {
        renderForm();
        for (const name of [
            'default_tool_access',
            'default_resource_access',
            'default_prompt_access',
            'default_resource_template_access',
            'approval_timeout_seconds',
        ]) {
            expect(field(name)).toBeInTheDocument();
        }
    });

    it('offers the three access levels', () => {
        renderForm();
        for (const label of ['Enabled', 'Approval Required', 'Disabled']) {
            expect(screen.getAllByRole('option', { name: label }).length).toBeGreaterThan(0);
        }
    });

    it('reads back what the group has set', () => {
        renderForm({ value: { default_tool_access: 'approval_required', approval_timeout_seconds: 300 } });
        expect(field('default_tool_access')).toHaveValue('approval_required');
        expect(field('approval_timeout_seconds')).toHaveValue(300);
    });

    it('a read-only caller gets fields it cannot edit', () => {
        renderForm({ disabled: true });
        expect(field('default_tool_access')).toBeDisabled();
        expect(field('approval_timeout_seconds')).toBeDisabled();
    });
});

describe('MCPGovernanceForm — editing', () => {
    it('emits the whole blob with only the edited key changed', () => {
        const { onChange } = renderForm({
            value: { default_tool_access: 'enabled', default_prompt_access: 'disabled' },
        });
        fireEvent.change(field('default_tool_access'), { target: { value: 'approval_required' } });
        expect(onChange).toHaveBeenCalledWith({
            default_tool_access: 'approval_required',
            default_prompt_access: 'disabled',
        });
    });

    it('choosing the empty option means "unset", not an empty string', () => {
        const { onChange } = renderForm({ value: { default_tool_access: 'enabled' } });
        fireEvent.change(field('default_tool_access'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ default_tool_access: undefined });
    });

    it('clearing the timeout emits undefined rather than zero', () => {
        const { onChange } = renderForm({ value: { approval_timeout_seconds: 300 } });
        fireEvent.change(field('approval_timeout_seconds'), { target: { value: '' } });
        expect(onChange).toHaveBeenCalledWith({ approval_timeout_seconds: undefined });
    });

    it('the timeout is a whole number of seconds from 1 to 604800', () => {
        renderForm();
        expect(field('approval_timeout_seconds')).toHaveAttribute('step', '1');
        expect(field('approval_timeout_seconds')).toHaveAttribute('min', '1');
        expect(field('approval_timeout_seconds')).toHaveAttribute('max', '604800');
    });
});

describe('MCPGovernanceForm — inheritance', () => {
    it('a root group is told nothing about parents', () => {
        renderForm();
        expect(screen.queryByText(/inherited:/i)).toBeNull();
        expect(screen.getAllByRole('option', { name: 'Not set' })).toHaveLength(4);
    });

    it('the empty option names what would be inherited instead of reading as "off"', () => {
        renderForm({ inherited: { default_tool_access: 'approval_required' } });
        expect(screen.getByRole('option', { name: 'Inherited: approval_required' })).toBeInTheDocument();
    });

    it('shows the inherited access level beside the field', () => {
        renderForm({ inherited: { default_tool_access: 'disabled' } });
        expect(screen.getByText(/inherited: disabled/)).toBeInTheDocument();
    });

    it('shows the inherited timeout ceiling as a maximum', () => {
        renderForm({ inherited: { approval_timeout_seconds: 120 } });
        expect(screen.getByText('max 120')).toBeInTheDocument();
    });
});

describe('MCPGovernanceForm — predicting the refusal', () => {
    it('flags an access level looser than the inherited one', () => {
        renderForm({
            value: { default_tool_access: 'enabled' },
            inherited: { default_tool_access: 'approval_required' },
        });
        expect(screen.getByTestId('governance-error-default_tool_access')).toHaveTextContent(
            'Looser than the inherited "approval required" — the save will be rejected',
        );
        expect(field('default_tool_access')).toHaveAttribute('aria-invalid', 'true');
    });

    it('accepts a TIGHTER choice without complaint', () => {
        renderForm({
            value: { default_tool_access: 'disabled' },
            inherited: { default_tool_access: 'approval_required' },
        });
        expect(screen.queryByTestId('governance-error-default_tool_access')).toBeNull();
        expect(field('default_tool_access')).not.toHaveAttribute('aria-invalid');
    });

    it('flags a timeout above the inherited maximum', () => {
        renderForm({
            value: { approval_timeout_seconds: 600 },
            inherited: { approval_timeout_seconds: 120 },
        });
        expect(screen.getByTestId('governance-error-approval_timeout_seconds')).toHaveTextContent(
            'Above the inherited maximum of 120 — the save will be rejected',
        );
    });

    it('flags each relaxed field independently', () => {
        renderForm({
            value: { default_tool_access: 'enabled', default_prompt_access: 'disabled' },
            inherited: { default_tool_access: 'disabled', default_prompt_access: 'disabled' },
        });
        expect(screen.getByTestId('governance-error-default_tool_access')).toBeInTheDocument();
        expect(screen.queryByTestId('governance-error-default_prompt_access')).toBeNull();
    });

    it('says nothing about a field the group has not set', () => {
        renderForm({ inherited: { default_tool_access: 'disabled' } });
        expect(screen.queryByTestId('governance-error-default_tool_access')).toBeNull();
    });
});

describe('MCPGovernanceForm — the effective hint', () => {
    it('discloses a merge that tightened the group’s own timeout', () => {
        renderForm({
            value: { approval_timeout_seconds: 60 },
            inherited: { approval_timeout_seconds: 120 },
            effective: { approval_timeout_seconds: 30 },
        });
        expect(screen.getByText('Effective: 30')).toBeInTheDocument();
    });

    it('stays quiet when the merge changed nothing', () => {
        renderForm({
            value: { approval_timeout_seconds: 60 },
            inherited: { approval_timeout_seconds: 120 },
            effective: { approval_timeout_seconds: 60 },
        });
        expect(screen.queryByText(/Effective:/)).toBeNull();
    });

    it('yields to the relaxation error rather than showing both', () => {
        renderForm({
            value: { approval_timeout_seconds: 600 },
            inherited: { approval_timeout_seconds: 120 },
            effective: { approval_timeout_seconds: 120 },
        });
        expect(screen.getByTestId('governance-error-approval_timeout_seconds')).toBeInTheDocument();
        expect(screen.queryByText(/Effective:/)).toBeNull();
    });
});
