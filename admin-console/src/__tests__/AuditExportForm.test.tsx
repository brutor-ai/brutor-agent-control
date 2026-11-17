/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AuditExportForm: the secrets of a destination, and its column allow-list.
 *
 * The Control Plane stores a destination's secrets (`secret_access_key`,
 * `bearer_token`, `token`, a webhook's `headers`) and never returns one: a
 * response says whether each is stored (`has_secret_access_key`,
 * `has_bearer_token`, `has_token`, `has_headers`). On an update a secret the
 * request does not name stays as stored, a value replaces it, and an empty
 * string removes it.
 *
 * So a secret field holds only what is typed now: it starts empty, says
 * whether a secret is stored, and is sent only when something was typed, or
 * as an empty string when removing it was asked for.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
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


function storedS3Destination(): AuditExportDestination {
    return {
        id: 'axd-1',
        tenant_id: 'test-tenant',
        name: 'Evidence vault',
        destination_type: 's3',
        enabled: true,
        config: {
            bucket: 'acme-audit-vault',
            prefix: 'brutor/proxy-logs',
            region: 'us-east-1',
            access_key_id: 'AKIATESTKEY',
            endpoint_url: 'https://minio.example.com',
            sse: 'aws:kms',
            has_secret_access_key: true,
        },
        field_allowlist: null,
        interval_seconds: 3600,
    };
}

function storedWebhookDestination(config: Record<string, unknown>): AuditExportDestination {
    return {
        id: 'axd-2',
        tenant_id: 'test-tenant',
        name: 'Collector',
        destination_type: 'webhook',
        enabled: true,
        config: { url: 'https://collector.example.com/logs', ...config },
        field_allowlist: null,
        interval_seconds: 300,
    };
}

const sentConfig = (): Record<string, unknown> => {
    const [, updates] = mockUpdate.mock.calls[0] as [string, { config: Record<string, unknown> }];
    return updates.config;
};

describe('AuditExportForm: the stored secret of an S3 destination', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockReset().mockResolvedValue({});
    });

    it('starts empty, says a secret is stored, and keeps it by not naming it', async () => {
        render(<AuditExportForm destination={storedS3Destination()} isEdit onCancel={() => {}} onSuccess={() => {}} />);
        fireEvent.click(screen.getByText('Backend'));

        const secret = await screen.findByLabelText('Secret Access Key');
        expect(secret).toHaveValue('');
        expect(screen.getByTestId('audit-export-secret-access-key-stored')).toHaveTextContent(
            'A secret access key is stored. It is never shown. Leave the field empty to keep it, or type a new one to replace it.',
        );
        // The destination cannot run without it: removing it is not offered.
        expect(screen.queryByLabelText('Remove the stored secret access key')).toBeNull();

        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        const config = sentConfig();
        expect(config).not.toHaveProperty('secret_access_key');
        expect(config.bucket).toBe('acme-audit-vault');
        expect(config.access_key_id).toBe('AKIATESTKEY');
    });

    it('sends neither what says a secret is stored nor any key the Control Plane refuses', async () => {
        render(<AuditExportForm destination={storedS3Destination()} isEdit onCancel={() => {}} onSuccess={() => {}} />);
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        // The configuration replaces the stored one: what the form has no
        // field for (the endpoint, the encryption) is sent back as it was.
        expect(sentConfig()).toEqual({
            bucket: 'acme-audit-vault',
            prefix: 'brutor/proxy-logs',
            region: 'us-east-1',
            access_key_id: 'AKIATESTKEY',
            endpoint_url: 'https://minio.example.com',
            sse: 'aws:kms',
        });
    });

    it('replaces the stored secret with the one typed', async () => {
        render(<AuditExportForm destination={storedS3Destination()} isEdit onCancel={() => {}} onSuccess={() => {}} />);
        fireEvent.click(screen.getByText('Backend'));
        fireEvent.change(await screen.findByLabelText('Secret Access Key'), { target: { value: 'NEW-SECRET-VALUE-123' } });
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        expect(sentConfig().secret_access_key).toBe('NEW-SECRET-VALUE-123');
    });

    it('asks for the secret when none is stored', async () => {
        const destination = storedS3Destination();
        destination.config = { ...destination.config, has_secret_access_key: false };
        render(<AuditExportForm destination={destination} isEdit onCancel={() => {}} onSuccess={() => {}} />);
        fireEvent.click(screen.getByText('Backend'));
        expect(await screen.findByTestId('audit-export-secret-access-key-stored')).toHaveTextContent(
            'No secret access key is stored. Type the secret access key of this destination.',
        );
        fireEvent.click(screen.getByText('Save Changes'));
        expect(await screen.findByText('Secret access key is required')).toBeInTheDocument();
        expect(mockUpdate).not.toHaveBeenCalled();
    });
});


describe('AuditExportForm: the stored bearer token and headers of a webhook', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockReset().mockResolvedValue({});
    });

    it('keeps the stored token and headers by naming neither', async () => {
        render(
            <AuditExportForm
                destination={storedWebhookDestination({ has_bearer_token: true, has_headers: true, header_names: ['DD-API-KEY', 'X-Team'] })}
                isEdit
                onCancel={() => {}}
                onSuccess={() => {}}
            />,
        );
        fireEvent.click(screen.getByText('Backend'));
        expect(await screen.findByLabelText('Bearer Token')).toHaveValue('');
        expect(screen.getByTestId('audit-export-bearer-token-stored')).toHaveTextContent('A bearer token is stored.');
        expect(screen.getByTestId('audit-export-headers-stored')).toHaveTextContent(
            'Stored request headers: DD-API-KEY, X-Team. Their values are never shown, and saving keeps them.',
        );

        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        expect(sentConfig()).toEqual({ url: 'https://collector.example.com/logs' });
    });

    it('removes the stored token when asked to, by sending it empty', async () => {
        render(
            <AuditExportForm destination={storedWebhookDestination({ has_bearer_token: true })} isEdit onCancel={() => {}} onSuccess={() => {}} />,
        );
        fireEvent.click(screen.getByText('Backend'));
        fireEvent.click(await screen.findByLabelText('Remove the stored bearer token'));
        expect(screen.getByLabelText('Bearer Token')).toBeDisabled();
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        expect(sentConfig()).toEqual({ url: 'https://collector.example.com/logs', bearer_token: '' });
    });

    it('offers no removal and names no token when none is stored', async () => {
        render(
            <AuditExportForm destination={storedWebhookDestination({ has_bearer_token: false })} isEdit onCancel={() => {}} onSuccess={() => {}} />,
        );
        fireEvent.click(screen.getByText('Backend'));
        expect(await screen.findByTestId('audit-export-bearer-token-stored')).toHaveTextContent('No bearer token is stored.');
        expect(screen.queryByLabelText('Remove the stored bearer token')).toBeNull();
        expect(screen.queryByTestId('audit-export-headers-stored')).toBeNull();
        fireEvent.click(screen.getByText('Save Changes'));
        await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        expect(sentConfig()).not.toHaveProperty('bearer_token');
    });
});


describe('AuditExportForm: a column the allow-list names that does not exist', () => {
    it('shows the refusal of the Control Plane as it was written, and stays open', async () => {
        const refusal =
            "field_allowlist: Value error, field_allowlist names column(s) that do not exist: ['llm_model']. Columns: ['chain', 'id', 'timestamp']";
        mockUpdate.mockReset().mockRejectedValue(new Error(refusal));
        const onSuccess = vi.fn();
        render(<AuditExportForm destination={storedS3Destination()} isEdit onCancel={() => {}} onSuccess={onSuccess} />);
        fireEvent.click(screen.getByText('Backend'));
        fireEvent.change(await screen.findByLabelText('Field Allowlist (optional)'), { target: { value: 'id, llm_model' } });
        expect(screen.getByTestId('audit-export-allowlist-help')).toHaveTextContent(
            'Every name must be a column that exists: a list that names one that does not is refused, and the refusal lists the columns.',
        );
        fireEvent.click(screen.getByText('Save Changes'));
        expect(await screen.findByText(refusal)).toBeInTheDocument();
        expect((mockUpdate.mock.calls[0] as [string, { field_allowlist: string[] }])[1].field_allowlist).toEqual(['id', 'llm_model']);
        expect(onSuccess).not.toHaveBeenCalled();
        mockUpdate.mockReset().mockResolvedValue({});
    });
});


describe('AuditExportForm — field allowlist serialisation', () => {
    it('sends a parsed string array, not the comma-separated text', async () => {
        render(<AuditExportForm onCancel={() => {}} onSuccess={() => {}} />);

        // Fill the bare minimum to pass validation
        fireEvent.change(screen.getByPlaceholderText(/Splunk evidence vault/i), {
            target: { value: 'Test' },
        });
        fireEvent.click(screen.getByText('Backend'));
        // The bucket placeholder is the unique identifier for the S3 bucket field
        const bucketInput = screen.getByPlaceholderText('acme-audit-vault');
        fireEvent.change(bucketInput, { target: { value: 'my-bucket' } });
        const accessKeyInput = screen.getByPlaceholderText(/AKIAIOSFODNN7EXAMPLE/i);
        fireEvent.change(accessKeyInput, { target: { value: 'AKIA-TEST' } });
        const secretInput = screen.getByLabelText('Secret Access Key');
        fireEvent.change(secretInput, { target: { value: 'SECRET' } });

        // Field allowlist — comma-separated text
        const allowlistInput = screen.getByPlaceholderText(/Empty = ship every column/i);
        fireEvent.change(allowlistInput, {
            target: { value: 'id, timestamp,  proxy_type ,, ' },
        });

        fireEvent.click(screen.getByText('Create Destination'));
        await waitFor(() => {
            expect(mockCreate).toHaveBeenCalled();
        });
        const [payload] = mockCreate.mock.calls[0];
        // Must be a string[] — empty tokens trimmed.
        expect(payload.field_allowlist).toEqual(['id', 'timestamp', 'proxy_type']);
    });
});
