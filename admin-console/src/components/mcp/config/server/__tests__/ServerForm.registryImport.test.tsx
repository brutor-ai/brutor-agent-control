/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP server form and importing a server from a registry.
 *
 * The registry search is an item of the `mcp-server-form.import` slot. Pinned
 * with it contributed: when registries are loaded, when the "Registry
 * Search" section is offered, and what choosing a server does to the form.
 * The last block is the form with nothing contributed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../../../test/mcp/serverForm'));
vi.mock('../../../../../hooks/useGateway.ts', async () => (await shared()).useGatewayModule());
vi.mock('../../../../common/Toast.tsx', async () => (await shared()).toastModule());
vi.mock('../ServerIcon.tsx', async () => (await shared()).blankModule());
vi.mock('../oauth/OAuthDiscoveryDisplay.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPLimitsForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPGovernanceForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../common/AuditInfoTab', async () => (await shared()).blankModule());

import ServerForm from '../ServerForm';
import type { MCPServerConfig } from '../../../../../lib/types.ts';
import { WithBuiltinSlots } from '../../../../../test/builtinSlots';
import { SlotProvider } from '../../../../../app/SlotProvider';
import type { ImportedServer, ServerImportSlotItem } from '../../../../../app/slots';
import { gateway, resetServerForm } from '../../../../../test/mcp/serverForm';

/** `slots: false` renders the form with nothing contributed to its slots. */
function renderForm(server: MCPServerConfig | null = null, { slots = true } = {}) {
    return render(
        <ServerForm server={server} onSave={vi.fn()} onCancel={vi.fn()} />,
        slots ? { wrapper: WithBuiltinSlots } : undefined,
    );
}

beforeEach(resetServerForm);

afterEach(cleanup);

describe('ServerForm — importing from a registry', () => {

    it('does not offer the search when no registry is enabled', () => {
        gateway.hasEnabledRegistries = false;
        renderForm();
        expect(screen.queryByText('Search Registries')).toBeNull();
        expect(Array.from(document.querySelectorAll('h4'))[0].textContent).toBe('Basic Information');
    });
});

describe('ServerForm — with nothing contributed', () => {
    it('offers no import, loads no registries, and is still the form', () => {
        renderForm(null, { slots: false });
        expect(gateway.loadRegistries).not.toHaveBeenCalled();
        expect(screen.queryByText('Search Registries')).toBeNull();
        expect(screen.queryByTestId('registry-dialog')).toBeNull();
        expect(Array.from(document.querySelectorAll('h4'))[0].textContent).toBe('Basic Information');
    });
});


describe('ServerForm — what the form gives a contributed import source', () => {
    /** What the dialog of a source hands the form when "import" is pressed. */
    const offered: { server: ImportedServer } = { server: { name: '', description: '' } };
    /** A source: a section with a button, and a dialog that says how it is mounted and imports `offered.server`. */
    const fakeSource = (id: string): ServerImportSlotItem => ({
        id,
        Section: ({ onOpen }) => (
            <div>
                <h4>{`Import from ${id}`}</h4>
                <button type="button" onClick={onOpen}>{`open ${id}`}</button>
            </div>
        ),
        Dialog: ({ creating, isOpen, onClose, onImport }) => (
            <div data-testid={`dialog-${id}`} data-open={String(isOpen)} data-creating={String(creating)}>
                <button type="button" onClick={() => onImport(offered.server)}>import</button>
                <button type="button" onClick={onClose}>close</button>
            </div>
        ),
    });
    const dialog = (id: string) => screen.getByTestId(`dialog-${id}`);
    const headings = () => Array.from(document.querySelectorAll('h4')).map((h) => h.textContent);

    const existing = {
        id: 'srv-1',
        name: 'billing',
        description: 'Billing tools',
        version: '1.0',
        base_url: 'https://billing.example.com',
        mcp_endpoint_path: '/mcp',
        auth_type: 'none',
        timeout: 30,
        enabled: true,
    } as unknown as MCPServerConfig;

    function renderWith(ids: string[], server: MCPServerConfig | null = null) {
        return render(
            <SlotProvider slots={{ 'mcp-server-form.import': ids.map(fakeSource) }}>
                <ServerForm server={server} onSave={vi.fn()} onCancel={vi.fn()} />
            </SlotProvider>,
        );
    }

    it('creating a server: the sections stand in order first on the General tab only, and the dialogs are mounted closed', () => {
        renderWith(['one', 'two']);
        expect(headings().slice(0, 3)).toEqual(['Import from one', 'Import from two', 'Basic Information']);
        for (const id of ['one', 'two']) {
            expect(dialog(id)).toHaveAttribute('data-open', 'false');
            expect(dialog(id)).toHaveAttribute('data-creating', 'true');
        }
        fireEvent.click(screen.getByText('Advanced'));
        expect(screen.queryByRole('button', { name: 'open one' })).toBeNull();
        expect(headings()).not.toContain('Import from one');
        // The dialogs stay mounted whatever the tab.
        expect(dialog('one')).toHaveAttribute('data-open', 'false');
        fireEvent.click(screen.getByText('General'));
        expect(headings().slice(0, 3)).toEqual(['Import from one', 'Import from two', 'Basic Information']);
    });

    it('editing a server: no section is offered, and the dialog is mounted closed and told so', () => {
        renderWith(['one'], existing);
        expect(screen.queryByRole('button', { name: 'open one' })).toBeNull();
        expect(headings()[0]).toBe('Basic Information');
        expect(dialog('one')).toHaveAttribute('data-open', 'false');
        expect(dialog('one')).toHaveAttribute('data-creating', 'false');
    });

    it('a section opens its own dialog only, and closing it changes nothing in the form', () => {
        offered.server = { name: 'io.acme/weather', description: 'never imported' };
        renderWith(['one', 'two']);
        fireEvent.click(screen.getByRole('button', { name: 'open two' }));
        expect(dialog('two')).toHaveAttribute('data-open', 'true');
        expect(dialog('one')).toHaveAttribute('data-open', 'false');
        fireEvent.click(within(dialog('two')).getByText('close'));
        expect(dialog('two')).toHaveAttribute('data-open', 'false');
        expect(screen.queryByDisplayValue('weather')).toBeNull();
        expect(screen.queryByDisplayValue('never imported')).toBeNull();
    });

    it('an imported server fills the name, the description and the split URL, and closes the dialog', () => {
        offered.server = {
            name: 'io.acme/weather',
            description: 'Weather tools',
            url: 'https://weather.example.com:8443/v1/mcp',
        };
        renderWith(['one']);
        fireEvent.click(screen.getByRole('button', { name: 'open one' }));
        expect(dialog('one')).toHaveAttribute('data-open', 'true');
        fireEvent.click(within(dialog('one')).getByText('import'));
        // The name is the last segment of the name handed over.
        expect(screen.getByDisplayValue('weather')).toBeInTheDocument();
        expect(screen.queryByDisplayValue('io.acme/weather')).toBeNull();
        expect(screen.getByDisplayValue('Weather tools')).toBeInTheDocument();
        // The URL is split into the server's base URL and the MCP endpoint path.
        expect(screen.getByDisplayValue('https://weather.example.com:8443')).toBeInTheDocument();
        expect(screen.getByDisplayValue('/v1/mcp')).toBeInTheDocument();
        expect(dialog('one')).toHaveAttribute('data-open', 'false');
    });

    it('an import whose URL cannot be split keeps it whole, and one without a URL clears what an earlier import filled', () => {
        offered.server = { name: 'first', description: 'with a url', url: 'https://first.example.com/v1/mcp' };
        renderWith(['one']);
        fireEvent.click(screen.getByRole('button', { name: 'open one' }));
        fireEvent.click(within(dialog('one')).getByText('import'));
        expect(screen.getByDisplayValue('https://first.example.com')).toBeInTheDocument();
        expect(screen.getByDisplayValue('/v1/mcp')).toBeInTheDocument();

        offered.server = { name: 'plain', description: 'no remote', url: 'not a url' };
        fireEvent.click(screen.getByRole('button', { name: 'open one' }));
        fireEvent.click(within(dialog('one')).getByText('import'));
        expect(screen.getByDisplayValue('plain')).toBeInTheDocument();
        expect(screen.getByDisplayValue('no remote')).toBeInTheDocument();
        expect(screen.getByDisplayValue('not a url')).toBeInTheDocument();
        expect(screen.queryByDisplayValue('/v1/mcp')).toBeNull();

        offered.server = { name: 'bare', description: 'nothing' };
        fireEvent.click(screen.getByRole('button', { name: 'open one' }));
        fireEvent.click(within(dialog('one')).getByText('import'));
        expect(screen.getByDisplayValue('bare')).toBeInTheDocument();
        expect(screen.getByDisplayValue('nothing')).toBeInTheDocument();
        expect(screen.queryByDisplayValue('not a url')).toBeNull();
        expect(dialog('one')).toHaveAttribute('data-open', 'false');
    });
});
