/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for ResourceMetaFields — the AI Resource identity block.
 *
 * The property worth defending is the asymmetry: **version is editable,
 * authorship is not**. Rendering a system-set field as an input invites an
 * operator to type into it and discover on save that it did nothing, which
 * is a small betrayal every time.
 *
 * The other is honesty about missing data. A row with no recorded author
 * has no creator to show, and saying so beats inventing "system" — an
 * account that never existed and that someone will eventually go looking
 * for.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import ResourceMetaFields from '../ResourceMetaFields';

const meta = {
    version: '2.1',
    created_by: 'alice',
    created_at: '2026-07-01T10:00:00Z',
    updated_by: 'bob',
    updated_at: '2026-07-27T12:30:00Z',
};

describe('ResourceMetaFields', () => {
    it('renders version as an editable input when a handler is given', () => {
        render(<ResourceMetaFields meta={meta} onVersionChange={vi.fn()} />);
        const input = screen.getByTestId<HTMLInputElement>('resource-version-input');
        expect(input.value).toBe('2.1');
    });

    it('reports version edits', () => {
        const onChange = vi.fn();
        render(<ResourceMetaFields meta={meta} onVersionChange={onChange} />);
        fireEvent.change(screen.getByTestId('resource-version-input'), {
            target: { value: '3.0' },
        });
        expect(onChange).toHaveBeenCalledWith('3.0');
    });

    it('renders version read-only when no handler is given', () => {
        render(<ResourceMetaFields meta={meta} />);
        expect(screen.getByTestId('resource-version-readonly')).toHaveTextContent('2.1');
        expect(screen.queryByTestId('resource-version-input')).not.toBeInTheDocument();
    });

    it('never renders authorship as an input', () => {
        render(<ResourceMetaFields meta={meta} onVersionChange={vi.fn()} />);
        const block = screen.getByTestId('resource-authorship');
        // The API refuses these fields on every request model, so an input
        // here could only mislead.
        expect(within(block).queryAllByRole('textbox')).toHaveLength(0);
        expect(block).toHaveTextContent('alice');
        expect(block).toHaveTextContent('bob');
    });

    it('says "unknown" rather than inventing an account', () => {
        render(
            <ResourceMetaFields
                meta={{ version: '1.0', created_by: null, updated_by: null }}
                onVersionChange={vi.fn()}
            />
        );
        const block = screen.getByTestId('resource-authorship');
        expect(within(block).getAllByText('unknown').length).toBe(2);
        expect(block).not.toHaveTextContent('system');
    });

    it('renders a dash for missing timestamps', () => {
        render(<ResourceMetaFields meta={{ version: '1.0' }} onVersionChange={vi.fn()} />);
        const block = screen.getByTestId('resource-authorship');
        expect(within(block).getAllByText('—').length).toBe(2);
    });

    it('survives an unparseable timestamp', () => {
        render(
            <ResourceMetaFields
                meta={{ ...meta, created_at: 'not-a-date' }}
                onVersionChange={vi.fn()}
            />
        );
        expect(screen.getByTestId('resource-authorship')).toHaveTextContent('—');
    });

    it('hides version entirely for resources that have none', () => {
        // A server config takes its version from the MCP server it
        // configures; showing an empty field would imply it has one.
        render(<ResourceMetaFields meta={{ created_by: 'alice' }} versionHidden />);
        expect(screen.queryByTestId('resource-version-input')).not.toBeInTheDocument();
        expect(screen.queryByTestId('resource-version-readonly')).not.toBeInTheDocument();
        expect(screen.getByTestId('resource-authorship')).toBeInTheDocument();
    });

    it('explains where a derived version comes from', () => {
        render(
            <ResourceMetaFields
                meta={{ version: '4.2' }}
                versionDerivedFrom="MCP server"
            />
        );
        expect(screen.getByText(/Determined by the MCP server/)).toBeInTheDocument();
    });

    it('does not offer editing while disabled', () => {
        render(<ResourceMetaFields meta={meta} onVersionChange={vi.fn()} disabled />);
        expect(screen.queryByTestId('resource-version-input')).not.toBeInTheDocument();
        expect(screen.getByTestId('resource-version-readonly')).toHaveTextContent('2.1');
    });
});
