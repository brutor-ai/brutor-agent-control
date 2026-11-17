/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for AuditInfoTab — the panel that renders identically on all nine
 * AI Resource forms.
 *
 * Two properties are worth pinning. First, `versionHidden`: a server config
 * takes its version from the MCP server it configures, and rendering a
 * second version field here would invite the two to disagree — with the
 * one on the config being the wrong answer. Second, the new-resource
 * notice, which exists so nobody hunts for empty authorship fields on a
 * resource that has not been saved yet.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import AuditInfoTab from '../AuditInfoTab';

const meta = {
    version: '1.4',
    created_by: 'alice',
    created_at: '2026-07-01T10:00:00Z',
    updated_by: 'bob',
    updated_at: '2026-07-27T12:30:00Z',
};

describe('AuditInfoTab', () => {
    it('renders the shared meta block', () => {
        render(<AuditInfoTab meta={meta} onVersionChange={vi.fn()} />);
        expect(screen.getByTestId('audit-info-tab')).toBeInTheDocument();
        expect(screen.getByTestId('resource-meta-fields')).toBeInTheDocument();
        expect(screen.getByTestId('resource-version-input')).toHaveValue('1.4');
    });

    it('hides version entirely for resources that derive it elsewhere', () => {
        render(
            <AuditInfoTab
                meta={{ created_by: 'alice' }}
                versionHidden
                versionDerivedFrom="MCP server"
            />,
        );
        expect(screen.queryByTestId('resource-version-input')).not.toBeInTheDocument();
        expect(screen.queryByTestId('resource-version-readonly')).not.toBeInTheDocument();
        // But say where it does come from — an operator who expected the
        // field should not be left wondering whether it is missing or broken.
        expect(screen.getByTestId('resource-version-derived')).toHaveTextContent(
            /determined by the MCP server/i,
        );
    });

    it('says nothing about a derived version when there is no source to name', () => {
        render(<AuditInfoTab meta={{ created_by: 'alice' }} versionHidden />);
        expect(screen.queryByTestId('resource-version-derived')).not.toBeInTheDocument();
    });

    it('drops version from its own description when version is hidden', () => {
        const { rerender } = render(<AuditInfoTab meta={meta} onVersionChange={vi.fn()} />);
        expect(screen.getByTestId('audit-info-tab')).toHaveTextContent(/Version, and who/i);

        rerender(<AuditInfoTab meta={meta} versionHidden />);
        expect(screen.getByTestId('audit-info-tab')).not.toHaveTextContent(/Version, and who/i);
    });

    it('explains that authorship is recorded on save for a new resource', () => {
        render(<AuditInfoTab meta={{}} onVersionChange={vi.fn()} isNew />);
        expect(screen.getByTestId('audit-info-new')).toHaveTextContent(/recorded when you save/i);
    });

    it('omits that notice once the resource exists', () => {
        render(<AuditInfoTab meta={meta} onVersionChange={vi.fn()} />);
        expect(screen.queryByTestId('audit-info-new')).not.toBeInTheDocument();
    });

    it('suppresses its own heading when the form already prints one', () => {
        // The routing group form's section chrome prints "Audit Info" above
        // the content; two identical headings stacked read as a bug.
        const { rerender } = render(<AuditInfoTab meta={meta} onVersionChange={vi.fn()} />);
        expect(screen.getByRole('heading', { name: 'Audit Info' })).toBeInTheDocument();

        rerender(<AuditInfoTab meta={meta} onVersionChange={vi.fn()} headingHidden />);
        expect(screen.queryByRole('heading', { name: 'Audit Info' })).not.toBeInTheDocument();
        // The fields themselves stay — only the heading goes.
        expect(screen.getByTestId('resource-meta-fields')).toBeInTheDocument();
    });

    it('renders read-only when no version handler is supplied', () => {
        render(<AuditInfoTab meta={meta} />);
        expect(screen.getByTestId('resource-version-readonly')).toHaveTextContent('1.4');
        expect(screen.queryByTestId('resource-version-input')).not.toBeInTheDocument();
    });
});
