/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The doubles of the tests of the tenant settings page (`TenantSettingsTab`):
 * the gateway client's `request`, the confirmation dialog and the toast, and
 * the stub of the evidence panel. Shared by the Community halves under
 * `components/configuration/__tests__/` and the Enterprise halves under
 * `enterprise/src/host/` (the default models and operator sub-tabs are
 * contributed by the Enterprise extension).
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the page, so that a mock factory can await it.
 */

import { vi } from 'vitest';

export const request = vi.fn();
export const confirm = vi.fn();
export const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };

/** `hooks/useGateway`. */
/** The client's own methods for the tenant's record and for the words the lifecycle gate knows, as the client issues them: through `request`. */
const getCurrentTenant = () => request('/v1/admin/tenants/current');
const getGatePolicyVocabulary = () => request('/v1/admin/ai-systems/gate-policy/vocabulary');
/** The client's own method for the settings update, as the client issues it: a PATCH through `request`. */
const updateCurrentTenantSettings = (body: unknown) => request('/v1/admin/tenants/current', { method: 'PATCH', body: JSON.stringify(body) });
/**
 * The methods a contributed model section asks the volume of a tenant's
 * Knowledge Base and its re-embedding with, as a client issues them.
 */
const getTenantKbStats = (tenantId: string) => request(`/v1/admin/tenants/${tenantId}/kb-stats`);
const rebuildTenantKbIndex = (tenantId: string) => request(`/v1/admin/tenants/${tenantId}/re-embed-kb`, { method: 'POST' });
/** The client's own method for the list of models, as the client issues it: the query the route names, through `request`. */
const listLLMs = (query?: { mode?: string; enabled?: boolean }): unknown => {
    const params = new URLSearchParams();
    if (query?.mode) params.append('mode', query.mode);
    if (query?.enabled !== undefined) params.append('enabled', String(query.enabled));
    const qs = params.toString();
    return request(`/v1/admin/llms${qs ? `?${qs}` : ''}`, { method: 'GET' });
};
export const useGatewayModule = () => ({
    useGateway: () => ({ gatewayClient: { request, getCurrentTenant, getGatePolicyVocabulary, updateCurrentTenantSettings, getTenantKbStats, rebuildTenantKbIndex, listLLMs } }),
});
/** `components/common/ConfirmationDialog`. */
export const confirmModule = () => ({ useConfirm: () => confirm });
/** `components/common/Toast`. */
export const toastModule = () => ({ useToast: () => toast });
/** `TenantEvidencePanel`. */
export const evidencePanelModule = () => ({ default: () => <div>evidence panel</div> });

/** The doubles as every case starts: nothing asked, every confirmation accepted. */
export function resetTenantSettingsDoubles(): void {
    request.mockReset();
    confirm.mockReset();
    confirm.mockResolvedValue(true);
    Object.values(toast).forEach((fn) => fn.mockReset());
}
