/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AuditExportForm Splunk HEC submission contract
 *
 * The splunk_hec destination_type has a config shape of its own:
 *   - url      (POST endpoint)
 *   - token    (HEC token; encrypted at rest)
 *   - index    (optional Splunk index)
 *   - sourcetype (defaults brutor:proxy_log)
 *   - source / host (optional metadata)
 *
 * These tests pin down the form → API contract for that backend. The
 * token is stored and never returned (`has_token` says whether one is):
 * the field starts empty, and the token is sent only when one is typed.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AuditExportForm from '../components/audit-export/AuditExportForm';
import { AuditExportDestination } from '../lib/types';

const mockCreate = vi.fn().mockResolvedValue({});
const mockUpdate = vi.fn().mockResolvedValue({});

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({
        createAuditExportDestination: mockCreate,
        updateAuditExportDestination: mockUpdate,
    }),
}));


function storedHecDestination(): AuditExportDestination {
    return {
        id: 'axd-hec-1',
        tenant_id: 'tenant-a',
        name: 'Splunk HEC Prod',
        destination_type: 'splunk_hec',
        enabled: true,
        config: {
            url: 'https://splunk.example.com:8088/services/collector/event',
            has_token: true,
            index: 'audit_prod',
            sourcetype: 'brutor:proxy_log',
            source: 'brutor-tenant-a',
            host: 'gw-01',
        },
        field_allowlist: null,
        interval_seconds: 300,
    };
}


describe('AuditExportForm — Splunk HEC backend', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockClear();
    });

    it('renders Splunk HEC dropdown option and switches to HEC tab', () => {
        render(<AuditExportForm onCancel={() => {}} onSuccess={() => {}} />);
        // The type dropdown — pick HEC.
        const select = screen.getByDisplayValue(/S3 \/ S3-compatible/i);
        fireEvent.change(select, { target: { value: 'splunk_hec' } });

        // Backend tab now shows HEC-specific inputs.
        fireEvent.click(screen.getByText('Backend'));
        expect(
            screen.getByPlaceholderText(/services\/collector\/event/i),
        ).toBeInTheDocument();
    });

    it('sends url + token + optional metadata on a fresh HEC create', async () => {
        render(<AuditExportForm onCancel={() => {}} onSuccess={() => {}} />);

        // Name
        fireEvent.change(
            screen.getByPlaceholderText(/Splunk evidence vault/i),
            { target: { value: 'Splunk Prod' } },
        );
        // Switch type
        fireEvent.change(screen.getByDisplayValue(/S3 \/ S3-compatible/i), {
            target: { value: 'splunk_hec' },
        });
        // Switch to backend tab and fill HEC fields
        fireEvent.click(screen.getByText('Backend'));

        fireEvent.change(
            screen.getByPlaceholderText(/services\/collector\/event/i),
            { target: { value: 'https://splunk.example.com:8088/services/collector/event' } },
        );
        fireEvent.change(
            screen.getByPlaceholderText(/HTTP Event Collector/i),
            { target: { value: 'HEC-TEST-TOKEN' } },
        );
        fireEvent.change(
            screen.getByPlaceholderText(/^audit_prod$/i),
            { target: { value: 'audit_prod' } },
        );

        fireEvent.click(screen.getByText('Create Destination'));
        await vi.waitFor(() => expect(mockCreate).toHaveBeenCalled());
        const payload = mockCreate.mock.calls[0][0];
        expect(payload.destination_type).toBe('splunk_hec');
        const cfg = payload.config as Record<string, unknown>;
        expect(cfg.url).toBe('https://splunk.example.com:8088/services/collector/event');
        expect(cfg.token).toBe('HEC-TEST-TOKEN');
        expect(cfg.index).toBe('audit_prod');
        // Default sourcetype carried through even if untouched.
        expect(cfg.sourcetype).toBe('brutor:proxy_log');
    });

    it('keeps the stored token by not naming it', async () => {
        render(<AuditExportForm destination={storedHecDestination()} isEdit onCancel={() => {}} onSuccess={() => {}} />);

        fireEvent.click(screen.getByText('Backend'));
        expect(await screen.findByLabelText('HEC Token')).toHaveValue('');
        expect(screen.getByTestId('audit-export-token-stored')).toHaveTextContent('A Splunk HEC token is stored.');
        // Splunk refuses a request without it: removing it is not offered.
        expect(screen.queryByLabelText('Remove the stored Splunk HEC token')).toBeNull();

        fireEvent.click(screen.getByText('Save Changes'));
        await vi.waitFor(() => expect(mockUpdate).toHaveBeenCalled());

        const [, updates] = mockUpdate.mock.calls[0];
        expect((updates as { config: Record<string, unknown> }).config).toEqual({
            url: 'https://splunk.example.com:8088/services/collector/event',
            index: 'audit_prod',
            sourcetype: 'brutor:proxy_log',
            source: 'brutor-tenant-a',
            host: 'gw-01',
        });
    });

    it('replaces the token with the one typed', async () => {
        render(<AuditExportForm destination={storedHecDestination()} isEdit onCancel={() => {}} onSuccess={() => {}} />);

        fireEvent.click(screen.getByText('Backend'));
        fireEvent.change(await screen.findByLabelText('HEC Token'), { target: { value: 'HEC-NEW-ROTATED' } });

        fireEvent.click(screen.getByText('Save Changes'));
        await vi.waitFor(() => expect(mockUpdate).toHaveBeenCalled());

        const [, updates] = mockUpdate.mock.calls[0];
        const cfg = (updates as { config: Record<string, unknown> }).config;
        expect(cfg.token).toBe('HEC-NEW-ROTATED');
    });

    it('blocks submit when HEC URL is missing', async () => {
        render(<AuditExportForm onCancel={() => {}} onSuccess={() => {}} />);

        fireEvent.change(
            screen.getByPlaceholderText(/Splunk evidence vault/i),
            { target: { value: 'HEC' } },
        );
        fireEvent.change(screen.getByDisplayValue(/S3 \/ S3-compatible/i), {
            target: { value: 'splunk_hec' },
        });
        fireEvent.click(screen.getByText('Backend'));
        // Don't fill URL — fill token only.
        fireEvent.change(
            screen.getByPlaceholderText(/HTTP Event Collector/i),
            { target: { value: 'T' } },
        );

        fireEvent.click(screen.getByText('Create Destination'));

        // No call — validation blocked it.
        expect(mockCreate).not.toHaveBeenCalled();
        expect(screen.getByText(/HEC URL is required/i)).toBeInTheDocument();
    });
});
