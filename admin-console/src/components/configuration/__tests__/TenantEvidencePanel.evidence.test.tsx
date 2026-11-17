/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings → Tenant → Evidence: witness pin
 * validation per kind (scitt = Ed25519 hex; rfc3161 = SHA-256 of the TSA
 * certificate DER or the PEM), the settings switches, the key ids, and
 * "Rotate key" including the CP's 409 `per_tenant_key_disabled`.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { GatewayAPIError } from '../../../lib/gatewayApiClient';

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

const gateway = {
    gatewayClient: {
        listEvidenceWitnesses, listEvidenceLogs, createEvidenceWitness, updateEvidenceWitness, deleteEvidenceWitness,
        getEvidenceSettings, putEvidenceSettings, listEvidenceSigningKeys, rotateEvidenceKey,
    },
};

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => ({ principal: null }) }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import TenantEvidencePanel from '../TenantEvidencePanel';

const HEX = 'ab'.repeat(32);
const PEM = '-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ2VydA==\n-----END CERTIFICATE-----\n';

const SETTINGS = { tenant_id: 'default', per_tenant_key: false, salt_digests_default: false, updated_at: null, updated_by: null };

beforeEach(() => {
    vi.clearAllMocks();
    listEvidenceWitnesses.mockResolvedValue({ items: [], total: 0 });
    listEvidenceLogs.mockResolvedValue({ items: [{ log_id: 'log-1', tenant_id: 'default', tree_size: 3, root: 'r', kid: 'e1'.repeat(32), heads_count: 0, receipts_count: 0 }], total: 1 });
    getEvidenceSettings.mockResolvedValue(SETTINGS);
    listEvidenceSigningKeys.mockResolvedValue({
        items: [
            { kid: 'e1'.repeat(32), scope: 'tenant', status: 'active', created_at: '2026-09-19T10:00:00Z', retired_at: null },
            { kid: 'd1'.repeat(32), scope: 'deployment', status: 'retired', created_at: '2026-09-01T10:00:00Z', retired_at: '2026-09-19T10:00:00Z' },
        ],
        total: 2,
        log_kid: 'e1'.repeat(32),
        per_tenant_key: true,
    });
    createEvidenceWitness.mockResolvedValue({});
    confirm.mockResolvedValue(true);
});

async function openAddDialog() {
    render(<TenantEvidencePanel />);
    await screen.findByTestId('witnesses-empty');
    fireEvent.click(screen.getByTestId('witness-add'));
    fireEvent.change(screen.getByTestId('witness-url'), { target: { value: 'https://w.example.org' } });
    fireEvent.change(screen.getByTestId('witness-operator'), { target: { value: 'Auditor Co' } });
}

describe('witness form validation', () => {
    it('scitt: refuses a pin that is not 64 hex and never calls the CP', async () => {
        await openAddDialog();
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: 'not-a-key' } });
        expect(screen.getByTestId('witness-pin-error')).toHaveTextContent('64 hex');
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() => expect(screen.getByTestId('witness-save')).toBeDisabled());
        expect(createEvidenceWitness).not.toHaveBeenCalled();
    });

    it('scitt: rejects a PEM (that is an rfc3161 pin)', async () => {
        await openAddDialog();
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: PEM } });
        expect(screen.getByTestId('witness-pin-error')).toBeInTheDocument();
    });

    it('rfc3161: accepts the certificate SHA-256 and stores it lowercase', async () => {
        await openAddDialog();
        fireEvent.change(screen.getByTestId('witness-kind'), { target: { value: 'rfc3161' } });
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: HEX.toUpperCase() } });
        expect(screen.queryByTestId('witness-pin-error')).toBeNull();
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() =>
            expect(createEvidenceWitness).toHaveBeenCalledWith(expect.objectContaining({ kind: 'rfc3161', pinned_key: HEX })),
        );
    });

    it('rfc3161: accepts a PEM certificate verbatim', async () => {
        await openAddDialog();
        fireEvent.change(screen.getByTestId('witness-kind'), { target: { value: 'rfc3161' } });
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: PEM } });
        expect(screen.queryByTestId('witness-pin-error')).toBeNull();
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() =>
            expect(createEvidenceWitness).toHaveBeenCalledWith(expect.objectContaining({ pinned_key: PEM.trim() })),
        );
    });

    it('rfc3161: refuses free text', async () => {
        await openAddDialog();
        fireEvent.change(screen.getByTestId('witness-kind'), { target: { value: 'rfc3161' } });
        fireEvent.change(screen.getByTestId('witness-pinned-key'), { target: { value: 'my tsa' } });
        expect(screen.getByTestId('witness-pin-error')).toHaveTextContent('SHA-256');
    });

    it('an empty pin is allowed but says receipts will grade unverified', async () => {
        await openAddDialog();
        expect(screen.getByTestId('witness-pin-empty')).toHaveTextContent('unverified');
        fireEvent.click(screen.getByTestId('witness-save'));
        await waitFor(() => expect(createEvidenceWitness).toHaveBeenCalledWith(expect.objectContaining({ pinned_key: null })));
    });

    it('a non-http URL and a blank operator are refused on submit', async () => {
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witnesses-empty');
        fireEvent.click(screen.getByTestId('witness-add'));
        fireEvent.change(screen.getByTestId('witness-url'), { target: { value: 'ftp://w.example.org' } });
        fireEvent.click(screen.getByTestId('witness-save'));
        expect(await screen.findByTestId('witness-url-error')).toHaveTextContent('http');
        expect(screen.getByTestId('witness-operator-error')).toBeInTheDocument();
        expect(createEvidenceWitness).not.toHaveBeenCalled();
    });
});

describe('keys and settings', () => {
    it('lists deployment and tenant key ids with status, marking the one signing now', async () => {
        render(<TenantEvidencePanel />);
        const row = await screen.findByTestId(`evidence-key-${'e1'.repeat(32)}`);
        expect(row).toHaveTextContent('active');
        expect(row).toHaveTextContent('signing now');
        expect(screen.getByTestId(`evidence-key-${'d1'.repeat(32)}`)).toHaveTextContent('retired');
        expect(screen.getByTestId(`evidence-key-${'d1'.repeat(32)}`)).toHaveTextContent('deployment');
    });

    it('toggles salt_digests_default through PUT', async () => {
        putEvidenceSettings.mockResolvedValue({ ...SETTINGS, salt_digests_default: true });
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-salt-default'));
        await waitFor(() => expect(putEvidenceSettings).toHaveBeenCalledWith({ salt_digests_default: true }));
        await waitFor(() => expect(screen.getByTestId('evidence-salt-default')).toBeChecked());
    });

    it('turning the per-tenant key off asks first', async () => {
        getEvidenceSettings.mockResolvedValue({ ...SETTINGS, per_tenant_key: true });
        confirm.mockResolvedValueOnce(false);
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-per-tenant-key'));
        await waitFor(() => expect(confirm).toHaveBeenCalled());
        expect(putEvidenceSettings).not.toHaveBeenCalled();
    });
});

describe('rotate key', () => {
    it('explains the 409 per_tenant_key_disabled instead of failing silently', async () => {
        rotateEvidenceKey.mockRejectedValue(
            new GatewayAPIError('HTTP 409: Conflict', 409, undefined, {
                detail: { error: 'per_tenant_key_disabled', detail: 'Enable per_tenant_key' },
            }),
        );
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-rotate-key'));
        const note = await screen.findByTestId('evidence-rotate-note');
        expect(note).toHaveTextContent('Per-tenant keys are off');
        expect(toast.error).not.toHaveBeenCalled();
    });

    it('reports another failure as a failure', async () => {
        rotateEvidenceKey.mockRejectedValue(new GatewayAPIError('core unreachable', 502));
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-rotate-key'));
        expect(await screen.findByTestId('evidence-rotate-note')).toHaveTextContent('Rotation failed: core unreachable');
    });

    it('rotates after confirmation and shows the new kid and rotation record', async () => {
        rotateEvidenceKey.mockResolvedValue({ kid: 'f1'.repeat(32), previous_kid: 'e1'.repeat(32), rotation_record_id: 'aa'.repeat(32) });
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-rotate-key'));
        const note = await screen.findByTestId('evidence-rotate-note');
        expect(note).toHaveTextContent('f1f1f1');
        expect(note).toHaveTextContent('rotation record');
        await waitFor(() => expect(listEvidenceSigningKeys).toHaveBeenCalledTimes(2));
    });

    it('does nothing when the confirmation is declined', async () => {
        confirm.mockResolvedValueOnce(false);
        render(<TenantEvidencePanel />);
        fireEvent.click(await screen.findByTestId('evidence-rotate-key'));
        await waitFor(() => expect(confirm).toHaveBeenCalled());
        expect(rotateEvidenceKey).not.toHaveBeenCalled();
    });
});

describe('a read beside the witnesses that failed', () => {
    it('the log, the settings and the keys each say why, and the witnesses stand', async () => {
        listEvidenceLogs.mockRejectedValue(new Error('log store down'));
        getEvidenceSettings.mockRejectedValue(new Error('settings forbidden'));
        listEvidenceSigningKeys.mockRejectedValue(new Error('core unreachable'));
        render(<TenantEvidencePanel />);
        await screen.findByTestId('witnesses-empty');
        expect(screen.getByTestId('tenant-evidence-log-failed')).toHaveTextContent('Failed to load the evidence log: log store down');
        expect(screen.getByTestId('tenant-evidence-settings-failed')).toHaveTextContent('Failed to load the evidence settings: settings forbidden');
        expect(screen.getByTestId('tenant-evidence-keys-failed')).toHaveTextContent('Failed to load the signing keys: core unreachable');
    });
});
