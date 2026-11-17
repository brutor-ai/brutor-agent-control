/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ServerDeletionConflictModal shows why a delete was refused. When the
 * Control Plane refused it because virtual servers use the server, its 409
 * lists them (`detail.used_by.virtual_servers`): the modal lists them from
 * that structure, with the way to the Virtual Servers tab. Any other refusal
 * is shown as the Control Plane words it.
 */

import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import ServerDeletionConflictModal from '../ServerDeletionConflictModal';

const MESSAGE = 'MCP server github-mcp is in use and is not deleted: virtual servers: Support Desk, Ops Copilot.';
const IN_USE = {
    message: MESSAGE,
    groups: [
        {
            kind: 'virtual_servers',
            label: 'Virtual servers',
            rows: [
                { id: 'vs-101', name: 'Support Desk' },
                { id: 'vs-202', name: 'Ops Copilot' },
            ],
        },
    ],
};

const renderModal = (overrides: Partial<React.ComponentProps<typeof ServerDeletionConflictModal>> = {}) =>
    render(
        <ServerDeletionConflictModal
            isOpen
            onClose={vi.fn()}
            serverName="github-mcp"
            errorDetail={MESSAGE}
            inUse={IN_USE}
            {...overrides}
        />,
    );

describe('ServerDeletionConflictModal', () => {
    afterEach(() => cleanup());

    it('renders nothing while closed', () => {
        const { container } = renderModal({ isOpen: false });
        expect(container).toBeEmptyDOMElement();
    });

    it('lists every virtual server the refusal names, with its id', () => {
        renderModal();
        expect(screen.getByText('Support Desk')).toBeTruthy();
        expect(screen.getByText('vs-101')).toBeTruthy();
        expect(screen.getByText('Ops Copilot')).toBeTruthy();
        expect(screen.getByText('vs-202')).toBeTruthy();
        expect(screen.getByText(/2 virtual servers/)).toBeTruthy();
        expect(screen.getByText('Server is in use by virtual servers')).toBeTruthy();
        expect(screen.getByText('To delete this server:')).toBeTruthy();
    });

    it('uses the singular when exactly one virtual server blocks the delete', () => {
        renderModal({ inUse: { message: MESSAGE, groups: [{ ...IN_USE.groups[0], rows: [{ id: 'vs-101', name: 'Support Desk' }] }] } });
        expect(screen.getByText(/1 virtual server\./)).toBeTruthy();
        expect(screen.queryByText(/1 virtual servers/)).toBeNull();
    });

    it('does not read the sentence for names: a refusal without the structure lists nothing', () => {
        renderModal({ inUse: null, errorDetail: '  • Support Desk (ID: vs-101)' });
        expect(screen.getByTestId('deletion-refusal')).toHaveTextContent('• Support Desk (ID: vs-101)');
        expect(screen.queryByText('Virtual Servers Using This Server')).toBeNull();
    });

    it('says what the Control Plane answered, and nothing about virtual servers, when the answer names none', () => {
        const onNavigate = vi.fn();
        renderModal({ inUse: null, errorDetail: 'The server is being deployed. Wait for the deployment to end.', onNavigateToVirtualServers: onNavigate });
        expect(screen.getByTestId('deletion-refusal')).toHaveTextContent('The server is being deployed. Wait for the deployment to end.');
        expect(screen.queryByText(/virtual server/i)).toBeNull();
        expect(screen.queryByText('To delete this server:')).toBeNull();
        expect(screen.queryByRole('button', { name: /go to virtual servers/i })).toBeNull();
    });

    it('names the server the operator tried to delete', () => {
        renderModal({ serverName: 'jira-mcp' });
        expect(screen.getByText(/"jira-mcp"/)).toBeTruthy();
    });

    it('navigates to virtual servers and closes in one click', () => {
        const onClose = vi.fn();
        const onNavigate = vi.fn();
        renderModal({ onClose, onNavigateToVirtualServers: onNavigate });

        fireEvent.click(screen.getByRole('button', { name: /go to virtual servers/i }));
        expect(onNavigate).toHaveBeenCalledTimes(1);
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('hides the navigate action when no handler is supplied', () => {
        renderModal({ onNavigateToVirtualServers: undefined });
        expect(screen.queryByRole('button', { name: /go to virtual servers/i })).toBeNull();
    });

    it('closes from the footer', () => {
        const onClose = vi.fn();
        renderModal({ onClose });
        fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});
