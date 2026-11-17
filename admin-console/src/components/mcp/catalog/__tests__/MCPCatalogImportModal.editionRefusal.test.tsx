/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The catalog import wizard and a control plane that does not deploy
 * containers (a Community composition).
 *
 * - `deploy_container=true` on a containerised entry is answered 400
 *   `enterprise_edition_feature`: the wizard shows the server's sentence, stays
 *   open, and reports nothing as imported. Only a contributed installer asks
 *   for a container (slot `mcp-catalog-import.steps`); a fake one does here.
 * - A containerised entry imported as configuration succeeds with a message
 *   ending "The server is not deployed: …": that message is what is shown.
 *   This is the Community console: nothing contributed, no container asked
 *   for, and nothing asked of the server before the import.
 *
 * The rejections are the ones the real API client throws for the server's
 * answer (`test/serverAnswers.ts`).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { CONTAINER_INSTALL_REFUSED, thrownByClient } from '../../../../test/serverAnswers';
import { callsWhileIdle } from '../../../../test/idle';
import type { MCPCatalogEntry } from '../../../../lib/mcpCatalogTypes';

// The same client object and the same functions on every render.
const gatewayClient = { request: vi.fn(), importMCPCatalogEntry: vi.fn() };
const gateway = { gatewayClient };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

const tenantFeatures = { isFeatureEnabled: vi.fn() };
vi.mock('../../../../context/TenantFeaturesContext', () => ({ useTenantFeatures: () => tenantFeatures }));

const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));

import MCPCatalogImportModal from '../MCPCatalogImportModal';
import { SlotProvider } from '../../../../app/SlotProvider';
import { FAKE_CATALOG_INSTALL_SLOTS } from '../../../../test/fakeCatalogInstall';

const ENTRY = {
    id: 'cat-fetch',
    name: 'Fetch',
    slug: 'fetch',
    runtime: 'containerized',
    auth_pattern: 'none',
    env_vars: [],
    description: 'Fetches web pages',
    categories: ['web'],
    source: 'system',
    enabled: true,
} as unknown as MCPCatalogEntry;

const REFUSAL = CONTAINER_INSTALL_REFUSED.body.detail.message;

const NOT_DEPLOYED =
    "Successfully imported 'Fetch'. The server is not deployed: this edition does not run " +
    'containerised servers. Run it yourself and set its URL.';

/** The wizard with an installer of containerised entries contributed (a fake one). */
function open(entry: MCPCatalogEntry = ENTRY) {
    const onClose = vi.fn();
    const onImported = vi.fn();
    render(
        <SlotProvider slots={FAKE_CATALOG_INSTALL_SLOTS}>
            <MCPCatalogImportModal isOpen onClose={onClose} entry={entry} onImported={onImported} />
        </SlotProvider>,
    );
    return { onClose, onImported };
}

/** The wizard as the Community console renders it: nothing contributed. */
function openCommunity(entry: MCPCatalogEntry = ENTRY) {
    const onClose = vi.fn();
    const onImported = vi.fn();
    render(<MCPCatalogImportModal isOpen onClose={onClose} entry={entry} onImported={onImported} />);
    return { onClose, onImported };
}

/** Review → Confirm & Import → Import Server. */
async function runImport() {
    fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Import Server' }));
    await waitFor(() => expect(gatewayClient.importMCPCatalogEntry).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    tenantFeatures.isFeatureEnabled.mockReturnValue(true);
    // This composition has no deployment capabilities to report, were it asked.
    gatewayClient.request.mockReset().mockRejectedValue(new Error('HTTP 404: Not Found'));
    gatewayClient.importMCPCatalogEntry.mockReset();
});

describe('MCPCatalogImportModal: containerised install refused by the edition', () => {
    it.each([
        ['as the API client throws it', () => thrownByClient(CONTAINER_INSTALL_REFUSED.status, CONTAINER_INSTALL_REFUSED.body)],
        ['handed over as the response body', () => Promise.resolve(CONTAINER_INSTALL_REFUSED.body)],
    ])('shows what the server said, %s, and stays open with nothing imported', async (_name, rejection) => {
        gatewayClient.importMCPCatalogEntry.mockRejectedValue(await rejection());
        const { onClose, onImported } = open();
        await runImport();

        expect(await screen.findByText(REFUSAL)).toBeInTheDocument();
        expect(gatewayClient.importMCPCatalogEntry).toHaveBeenCalledWith(
            'cat-fetch',
            { enabled: true, deploy_container: true },
            { timeout: 300_000 },
        );

        // Still the wizard, on its last step, ready to be cancelled or retried.
        expect(screen.getByRole('heading', { name: /Import MCP Server/i })).toBeInTheDocument();
        await waitFor(() => expect(screen.getByRole('button', { name: 'Import Server' })).toBeEnabled());
        expect(onClose).not.toHaveBeenCalled();

        // Nothing was installed, and nothing says it was or might have been.
        expect(onImported).not.toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
        expect(toast.warning).not.toHaveBeenCalled();
        expect(screen.queryByText('Import is still running on the server')).not.toBeInTheDocument();
        expect(screen.queryByText("Imported, but the MCP server isn't healthy")).not.toBeInTheDocument();
        expect(screen.queryByText('Failed to import catalog entry.')).not.toBeInTheDocument();
    });

    it('does not take a refusal that mentions a timeout for a request that timed out', async () => {
        const body = {
            detail: {
                error: 'deployment_driver_unavailable',
                driver: 'docker',
                implemented: true,
                message: 'Container deployment is not available: connection timeout reaching the Docker daemon.',
            },
        };
        gatewayClient.importMCPCatalogEntry.mockRejectedValue(await thrownByClient(400, body));
        const { onImported } = open();
        await runImport();

        expect(await screen.findByText(body.detail.message)).toBeInTheDocument();
        expect(screen.queryByText('Import is still running on the server')).not.toBeInTheDocument();
        expect(onImported).not.toHaveBeenCalled();
    });

    it('still takes a request that timed out for one that may have landed', async () => {
        gatewayClient.importMCPCatalogEntry.mockRejectedValue(new Error('Request timeout after 300000ms'));
        const { onImported } = open();
        await runImport();

        expect(await screen.findByText('Import is still running on the server')).toBeInTheDocument();
        expect(onImported).toHaveBeenCalledTimes(1);
    });

    it('still shows the generic text when the failure says nothing', async () => {
        gatewayClient.importMCPCatalogEntry.mockRejectedValue({});
        open();
        await runImport();
        expect(await screen.findByText('Failed to import catalog entry.')).toBeInTheDocument();
    });

    it('asks the server nothing while it is open: only the installer\'s gate would', async () => {
        open();
        await screen.findByRole('button', { name: 'Next' });
        expect(gatewayClient.request).not.toHaveBeenCalled();
        expect(await callsWhileIdle([gatewayClient.request, gatewayClient.importMCPCatalogEntry])).toBe(0);
    });

    it('the installer\'s gate is shown in place of the wizard where it says so, and closes it', () => {
        const { onClose } = open({ ...ENTRY, name: 'Blocked' });
        expect(screen.getByText('the fake installer cannot install Blocked')).toBeInTheDocument();
        expect(screen.queryByRole('heading', { name: /Import MCP Server/i })).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Close the fake gate' }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('with an installer the last step says a container is deployed', async () => {
        open();
        fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
        expect(screen.getByText('Deploy Container:')).toBeInTheDocument();
    });
});

describe('MCPCatalogImportModal: a containerised entry imported without a deployment', () => {
    it('reports the import with the server\'s message, which says the server is not deployed', async () => {
        gatewayClient.importMCPCatalogEntry.mockResolvedValue({
            mcp_server_id: 'mcp-1',
            idp_server_id: null,
            oauth_client_id: null,
            server_config_id: 'sc-1',
            docker_deployment_id: null,
            message: NOT_DEPLOYED,
            deployment_warnings: [],
            container_log_excerpt: null,
        });
        const { onImported } = openCommunity();
        await runImport();

        // As configuration: no container is asked for, and the call is an ordinary one.
        expect(gatewayClient.importMCPCatalogEntry).toHaveBeenCalledWith('cat-fetch', { enabled: true, deploy_container: false }, { timeout: undefined });

        await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Import Successful', NOT_DEPLOYED));
        expect(toast.success).toHaveBeenCalledTimes(1);
        expect(onImported).toHaveBeenCalledTimes(1);
        // Nothing on the page claims a container.
        expect(screen.queryByText(/container deployed/i)).not.toBeInTheDocument();
        expect(screen.queryByText("Imported, but the MCP server isn't healthy")).not.toBeInTheDocument();
        expect(screen.queryByText('Import is still running on the server')).not.toBeInTheDocument();
    });

    it('asks the server nothing before the import: no deployment capabilities, no other path', async () => {
        openCommunity();
        await screen.findByRole('button', { name: 'Next' });
        expect(gatewayClient.request).not.toHaveBeenCalled();
        expect(await callsWhileIdle([gatewayClient.request, gatewayClient.importMCPCatalogEntry])).toBe(0);
    });

    it('does not offer to deploy a container, whatever the tenant\'s features', () => {
        tenantFeatures.isFeatureEnabled.mockReturnValue(false);
        openCommunity();
        // The wizard, not a view that says deployment is disabled.
        expect(screen.getByRole('heading', { name: /Import MCP Server/i })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(screen.getByRole('button', { name: 'Import Server' })).toBeInTheDocument();
        expect(screen.queryByText('Deploy Container:')).not.toBeInTheDocument();
        expect(screen.queryByText(/MCP Server Deployment/i)).not.toBeInTheDocument();
        expect(tenantFeatures.isFeatureEnabled).not.toHaveBeenCalled();
    });

    it('shows no container stages while the import runs', async () => {
        let finish: (value: unknown) => void = () => {};
        gatewayClient.importMCPCatalogEntry.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
        openCommunity();
        await runImport();
        expect(screen.queryByText(/Validating import request|Pulling Docker image|Starting container/)).not.toBeInTheDocument();
        finish({ message: NOT_DEPLOYED, deployment_warnings: [] });
        await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
    });
});
