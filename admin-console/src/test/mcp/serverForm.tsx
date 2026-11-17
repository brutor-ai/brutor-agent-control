/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of `ServerForm.registryImport.test` share: the gateway
 * double and the stubs of the form's children. The Community half is the MCP
 * server form with nothing contributed; importing from a registry is the
 * Enterprise extension's, and its cases are under `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the form, so that a mock factory can await it.
 */

import { vi } from 'vitest';

export const gateway = {
    hasEnabledRegistries: true,
    loadRegistries: vi.fn(() => Promise.resolve()),
    convertServerToFormInfo: vi.fn(),
    gatewayClient: {},
    createIdPConfig: vi.fn(),
};

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => gateway });
/** `components/common/Toast`. */
export const toastModule = () => ({
    useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
});
/** A child of the form that these cases do not look at: an empty element. */
export const blankModule = () => ({ default: () => <div /> });

/** How every case starts: registries enabled, and a server of a registry converted to form values. For a `beforeEach`. */
export function resetServerForm(): void {
    vi.clearAllMocks();
    gateway.hasEnabledRegistries = true;
    gateway.loadRegistries.mockImplementation(() => Promise.resolve());
    gateway.convertServerToFormInfo.mockImplementation((detail: { name: string }, registryName: string) => ({
        name: detail.name,
        description: `from ${registryName}`,
        url: 'https://weather.example.com:8443/v1/mcp',
    }));
    vi.spyOn(console, 'log').mockImplementation(() => {});
}
