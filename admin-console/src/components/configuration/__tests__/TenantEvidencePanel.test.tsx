/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant → Evidence: the witness table with its dialogs, the
 * key card, and the scope rule — a tenant admin adds tenant-scoped
 * witnesses only and cannot edit platform ones.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const listEvidenceWitnesses = vi.fn();
const listEvidenceLogs = vi.fn();
const createEvidenceWitness = vi.fn();
const updateEvidenceWitness = vi.fn();
const deleteEvidenceWitness = vi.fn();
const getEvidenceSettings = vi.fn();
const putEvidenceSettings = vi.fn();
const listEvidenceSigningKeys = vi.fn();
const rotateEvidenceKey = vi.fn();
const toast = { success: vi.fn(), error: vi.fn() };
const confirm = vi.fn();
let systemAdmin = false;

const gateway = {
    gatewayClient: {
        listEvidenceWitnesses, listEvidenceLogs, createEvidenceWitness, updateEvidenceWitness, deleteEvidenceWitness,
        getEvidenceSettings, putEvidenceSettings, listEvidenceSigningKeys, rotateEvidenceKey,
    },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => ({ principal: systemAdmin ? { id: 'system' } : null }) }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import TenantEvidencePanel from '../TenantEvidencePanel';

const WITNESSES = {
    items: [
        { id: 'w-plat', scope: 'platform', tenant_id: null, kind: 'scitt', url: 'https://witness.brutor.ai', operator_label: 'Brutor', pinned_key: 'abcdef0123456789abcdef', enabled: true, same_operator: true },
        { id: 'w-ten', scope: 'tenant', tenant_id: 'default', kind: 'rfc3161', url: 'https://tsa.example.org', operator_label: 'Example TSA', pinned_key: null, enabled: false, same_operator: false },
    ],
    total: 2,
};

beforeEach(() => {
    vi.clearAllMocks();
    systemAdmin = false;
    listEvidenceWitnesses.mockResolvedValue(WITNESSES);
    listEvidenceLogs.mockResolvedValue({ items: [{ log_id: 'log-1', tenant_id: 'default', tree_size: 10, root: 'r', kid: 'kid-xyz', heads_count: 1, receipts_count: 0 }], total: 1 });
    createEvidenceWitness.mockResolvedValue({});
    updateEvidenceWitness.mockResolvedValue({});
    deleteEvidenceWitness.mockResolvedValue({ deleted: true });
    confirm.mockResolvedValue(true);
    getEvidenceSettings.mockResolvedValue({ tenant_id: 'default', per_tenant_key: false, salt_digests_default: false });
    listEvidenceSigningKeys.mockResolvedValue({ items: [], total: 0, log_kid: 'kid-xyz', per_tenant_key: false });
});

describe('TenantEvidencePanel', () => {
    it('shows the key card and the witnesses with the same-operator badge', async () => {
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witness-row-w-plat');
        expect(screen.getByTestId('tenant-evidence-key')).toHaveTextContent('kid-xyz');
        expect(screen.getByTestId('witness-same-operator-w-plat')).toHaveTextContent('same operator');
        expect(screen.getByTestId('witness-row-w-ten')).toHaveTextContent('none pinned');
        expect(screen.getByTestId('witness-row-w-ten')).toHaveTextContent('off');
    });

    it('a tenant admin can edit tenant witnesses only and creates with tenant scope', async () => {
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witness-row-w-plat');
        expect(screen.queryByTestId('witness-edit-w-plat')).toBeNull();
        expect(screen.getByTestId('witness-edit-w-ten')).toBeInTheDocument();

        fireEvent.click(screen.getByTestId('witness-add'));
        const scope = screen.getByTestId<HTMLSelectElement>('witness-scope');
        expect(scope).toBeDisabled();
        expect(scope.value).toBe('tenant');
        fireEvent.change(screen.getByTestId('witness-url'), { target: { value: 'https://w.example.org' } });
        fireEvent.change(screen.getByTestId('witness-operator'), { target: { value: 'Example' } });
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: ` ${'AB'.repeat(32)} ` } });
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() =>
            expect(createEvidenceWitness).toHaveBeenCalledWith({
                scope: 'tenant', kind: 'scitt', url: 'https://w.example.org', operator_label: 'Example',
                pinned_key: 'ab'.repeat(32), enabled: true, same_operator: false,
            }),
        );
        expect(toast.success).toHaveBeenCalledWith('Witness added');
        await waitFor(() => expect(listEvidenceWitnesses).toHaveBeenCalledTimes(2));
    });

    it('a system admin may choose platform scope and edit platform witnesses', async () => {
        systemAdmin = true;
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witness-row-w-plat');
        expect(screen.getByTestId('witness-edit-w-plat')).toBeInTheDocument();
        fireEvent.click(screen.getByTestId('witness-add'));
        const scope = screen.getByTestId<HTMLSelectElement>('witness-scope');
        expect(scope).not.toBeDisabled();
        expect(Array.from(scope.options).map((o) => o.value)).toEqual(['tenant', 'platform']);
    });

    it('edits through PATCH', async () => {
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witness-row-w-ten');
        fireEvent.click(screen.getByTestId('witness-edit-w-ten'));
        expect(screen.queryByTestId('witness-scope')).toBeNull();
        expect(screen.getByTestId<HTMLSelectElement>('witness-kind')).toBeDisabled();
        fireEvent.click(screen.getByTestId('witness-enabled'));
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() =>
            expect(updateEvidenceWitness).toHaveBeenCalledWith('w-ten', expect.objectContaining({ enabled: true, operator_label: 'Example TSA' })),
        );
    });

    it('deletes after confirmation and not without it', async () => {
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witness-row-w-ten');
        confirm.mockResolvedValueOnce(false);
        fireEvent.click(screen.getByTestId('witness-delete-w-ten'));
        await waitFor(() => expect(confirm).toHaveBeenCalled());
        expect(deleteEvidenceWitness).not.toHaveBeenCalled();
        fireEvent.click(screen.getByTestId('witness-delete-w-ten'));
        await waitFor(() => expect(deleteEvidenceWitness).toHaveBeenCalledWith('w-ten'));
    });

    it('renders no-log and no-witness states and a load error', async () => {
        listEvidenceLogs.mockResolvedValueOnce({ items: [], total: 0 });
        listEvidenceWitnesses.mockResolvedValueOnce({ items: [], total: 0 });
        const { unmount } = render(<TenantEvidencePanel />);
        expect(await screen.findByTestId('witnesses-empty')).toBeInTheDocument();
        expect(screen.getByTestId('tenant-evidence-no-log')).toBeInTheDocument();
        unmount();

        listEvidenceWitnesses.mockRejectedValueOnce(new Error('witnesses down'));
        render(<TenantEvidencePanel />);
        expect(await screen.findByTestId('tenant-evidence-error')).toHaveTextContent('witnesses down');
    });

    it('says the tenant key cannot be read, and to rotate it, when the gateway seals nothing for the tenant', async () => {
        // The Control Plane's answer: the code `tenant_key_unavailable`, with the gateway's sentence.
        const { thrownByClient } = await import('../../../test/serverAnswers');
        getEvidenceSettings.mockResolvedValue({ tenant_id: 'default', per_tenant_key: true, salt_digests_default: false });
        rotateEvidenceKey.mockRejectedValue(
            await thrownByClient(503, {
                detail: {
                    error: 'tenant_key_unavailable',
                    detail:
                        "this tenant's own evidence signing key cannot be used; nothing is sealed until it is rotated (POST /v1/evidence/keys/{tenant}/rotate) or ENCRYPTION_KEY is restored",
                },
            }),
        );
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-rotate-key'));
        const note = await screen.findByTestId('evidence-rotate-note');
        expect(note).toHaveTextContent("The gateway cannot read this tenant's signing key, so it seals no evidence for the tenant.");
        expect(note).toHaveTextContent('The rotation was refused for the same reason');
        expect(note).not.toHaveTextContent('HTTP 503');
    });
});
