/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Provider create/edit form.
 *
 * The two payloads differ on purpose and that is what is pinned. The update
 * endpoint is `exclude_unset`, so a blanked field must go as null to be cleared,
 * while create leaves it out. Also pinned: a save error from the Audit Info tab
 * is brought into view, because the banner lives on the General tab and the
 * button otherwise appears to do nothing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const client = {
    createProvider: vi.fn(),
    updateProvider: vi.fn(),
    uploadProviderIcon: vi.fn(),
    deleteProviderIcon: vi.fn(),
};
const gateway = { gatewayClient: client };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../config/ProviderIcon', () => ({ default: () => <span>icon</span> }));
vi.mock('../../../common/AuditInfoTab', () => ({
    default: ({ onVersionChange, isNew }: { onVersionChange: (v: string) => void; isNew: boolean }) => (
        <div>
            audit tab {isNew ? 'new' : 'existing'}
            <button type="button" onClick={() => onVersionChange('2.0')}>
                bump version
            </button>
        </div>
    ),
}));

import LLProviderForm from '../LLProviderForm';
import type { LlmProviderResponse } from '../../../../lib/llmProviderTypes';

const onClose = vi.fn();
const onSaved = vi.fn();

function existing(overrides: Record<string, unknown> = {}): LlmProviderResponse {
    return {
        id: 'p1',
        key: 'acme',
        display_name: 'Acme',
        version: '3.0',
        category: 'chat',
        default_auth_type: 'api_key',
        default_api_url: 'https://api.acme.test',
        developer_url: 'https://dev.acme.test',
        api_key_url: 'https://keys.acme.test',
        pricing_url: 'https://price.acme.test',
        model_catalog_url: 'https://docs.acme.test',
        models_api_url: 'https://api.acme.test/models',
        additional_drop_params: ['a', 'b'],
        region: 'eu-west-1',
        notes: 'old notes',
        enabled: true,
        has_icon: false,
        ...overrides,
    } as unknown as LlmProviderResponse;
}

function renderCreate() {
    return render(<LLProviderForm provider={null} onClose={onClose} onSaved={onSaved} />);
}

function renderEdit(provider: LlmProviderResponse = existing()) {
    return render(<LLProviderForm provider={provider} onClose={onClose} onSaved={onSaved} />);
}

function field(labelText: string): HTMLInputElement {
    const label = Array.from(document.querySelectorAll('label')).find(l =>
        (l.textContent ?? '').trim().startsWith(labelText)
    );
    if (!label) throw new Error(`No label starting with "${labelText}"`);
    const control = label.parentElement?.querySelector('input,select,textarea');
    if (!control) throw new Error(`No control for "${labelText}"`);
    return control as HTMLInputElement;
}

function setField(labelText: string, value: string) {
    fireEvent.change(field(labelText), { target: { value } });
}

function click(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}

const URL_AND_TEXT_LABELS = [
    'Default API URL',
    'Developer site',
    'API key page',
    'Pricing',
    'Model catalog',
    'Models API endpoint',
    'Additional drop params',
    'Region',
    'Notes',
];

describe('LLProviderForm', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        for (const fn of Object.values(client)) fn.mockReset();
        client.createProvider.mockResolvedValue({ display_name: 'Server Name' });
        client.updateProvider.mockResolvedValue({});
        onSaved.mockResolvedValue(undefined);
    });
    afterEach(cleanup);

    describe('create', () => {
        it('leaves blank fields out and reports the name the server returned', async () => {
            renderCreate();
            expect(screen.getByRole('heading', { name: 'New Provider' })).toBeTruthy();
            setField('Slug', 'acme');
            setField('Display name', '  Acme  ');
            click('Create provider');

            await waitFor(() => expect(onSaved).toHaveBeenCalledWith('Created provider Server Name'));
            expect(client.createProvider).toHaveBeenCalledWith({
                version: '1.0',
                key: 'acme',
                display_name: 'Acme',
                category: 'chat',
                default_auth_type: 'api_key',
                default_api_url: undefined,
                developer_url: undefined,
                api_key_url: undefined,
                pricing_url: undefined,
                model_catalog_url: undefined,
                models_api_url: undefined,
                additional_drop_params: undefined,
                region: undefined,
                notes: undefined,
                enabled: true,
            });
        });

        it('sends every field the admin filled, trimmed, with drop params as a list', async () => {
            renderCreate();
            setField('Slug', 'ACME_1');
            expect(field('Slug').value).toBe('acme_1');
            setField('Display name', 'Acme');
            setField('Category', 'image');
            setField('Default auth type', 'aws_iam');
            setField('Default API URL', ' https://a.test ');
            setField('Developer site', ' https://b.test ');
            setField('API key page', ' https://c.test ');
            setField('Pricing', ' https://d.test ');
            setField('Model catalog', ' https://e.test ');
            setField('Models API endpoint', ' https://f.test ');
            setField('Additional drop params', ' logit_bias, , stream_options ,');
            setField('Region', ' eu-west-1 ');
            setField('Notes', ' internal ');
            fireEvent.click(screen.getByRole('checkbox'));
            fireEvent.click(screen.getByRole('tab', { name: 'Audit Info' }));
            expect(screen.getByText('audit tab new')).toBeTruthy();
            click('bump version');
            click('Create provider');

            await waitFor(() => expect(client.createProvider).toHaveBeenCalledTimes(1));
            expect(client.createProvider).toHaveBeenCalledWith({
                version: '2.0',
                key: 'acme_1',
                display_name: 'Acme',
                category: 'image',
                default_auth_type: 'aws_iam',
                default_api_url: 'https://a.test',
                developer_url: 'https://b.test',
                api_key_url: 'https://c.test',
                pricing_url: 'https://d.test',
                model_catalog_url: 'https://e.test',
                models_api_url: 'https://f.test',
                additional_drop_params: ['logit_bias', 'stream_options'],
                region: 'eu-west-1',
                notes: 'internal',
                enabled: false,
            });
        });

        it.each([
            ['', 'Acme', 'Slug is required.'],
            ['-bad', 'Acme', 'Slug must be lowercase alphanumeric with optional `-` or `_`, max 64 chars.'],
            ['a'.repeat(65), 'Acme', 'Slug must be lowercase alphanumeric with optional `-` or `_`, max 64 chars.'],
            ['acme', '   ', 'Display name is required.'],
        ])('refuses slug "%s", name "%s"', async (slug, name, message) => {
            renderCreate();
            setField('Slug', slug);
            setField('Display name', name);
            click('Create provider');
            expect(await screen.findByText(message)).toBeTruthy();
            expect(client.createProvider).not.toHaveBeenCalled();
            expect(onSaved).not.toHaveBeenCalled();
        });

        it('offers no icon upload until the provider exists', () => {
            renderCreate();
            expect(screen.queryByRole('button', { name: 'Upload' })).toBeNull();
        });

        it('shows a server error, stays open, and recovers the button', async () => {
            client.createProvider.mockRejectedValue(new Error('Slug already exists'));
            renderCreate();
            setField('Slug', 'acme');
            setField('Display name', 'Acme');
            click('Create provider');
            expect(await screen.findByText('Slug already exists')).toBeTruthy();
            expect(onSaved).not.toHaveBeenCalled();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Create provider' }).disabled).toBe(false);
        });

        it('brings a validation error into view when saving from the Audit tab', async () => {
            renderCreate();
            fireEvent.click(screen.getByRole('tab', { name: 'Audit Info' }));
            click('Create provider');
            const error = await screen.findByText('Display name is required.');
            expect(error.closest('.hidden')).toBeNull();
            expect(screen.queryByText('audit tab new')).toBeNull();
        });

        it('brings a server error into view when saving from the Audit tab', async () => {
            client.createProvider.mockRejectedValue(new Error('Nope'));
            renderCreate();
            setField('Slug', 'acme');
            setField('Display name', 'Acme');
            fireEvent.click(screen.getByRole('tab', { name: 'Audit Info' }));
            click('Create provider');
            const error = await screen.findByText('Nope');
            expect(error.closest('.hidden')).toBeNull();
        });
    });

    describe('edit', () => {
        it('populates from the provider, with the slug locked', () => {
            renderEdit();
            expect(screen.getByRole('heading', { name: 'Edit Provider — Acme' })).toBeTruthy();
            expect(field('Slug').disabled).toBe(true);
            expect(field('Slug').value).toBe('acme');
            expect(field('Additional drop params').value).toBe('a, b');
            expect(field('Region').value).toBe('eu-west-1');
        });

        it('sends the update, without the slug', async () => {
            renderEdit();
            setField('Display name', 'Acme Two');
            setField('Region', 'us-east-1');
            click('Save changes');

            await waitFor(() => expect(onSaved).toHaveBeenCalledWith('Updated provider Acme Two'));
            expect(client.createProvider).not.toHaveBeenCalled();
            expect(client.updateProvider).toHaveBeenCalledWith('p1', {
                version: '3.0',
                display_name: 'Acme Two',
                category: 'chat',
                default_auth_type: 'api_key',
                default_api_url: 'https://api.acme.test',
                developer_url: 'https://dev.acme.test',
                api_key_url: 'https://keys.acme.test',
                pricing_url: 'https://price.acme.test',
                model_catalog_url: 'https://docs.acme.test',
                models_api_url: 'https://api.acme.test/models',
                additional_drop_params: ['a', 'b'],
                region: 'us-east-1',
                notes: 'old notes',
                enabled: true,
            });
        });

        it('sends null for what was blanked, so the server really clears it', async () => {
            renderEdit();
            for (const label of URL_AND_TEXT_LABELS) setField(label, '  ');
            setField('Category', '');
            setField('Default auth type', '');
            click('Save changes');
            await waitFor(() => expect(client.updateProvider).toHaveBeenCalledTimes(1));
            expect(client.updateProvider.mock.calls[0][1]).toMatchObject({
                category: null,
                default_auth_type: null,
                default_api_url: null,
                developer_url: null,
                api_key_url: null,
                pricing_url: null,
                model_catalog_url: null,
                models_api_url: null,
                additional_drop_params: null,
                region: null,
                notes: null,
            });
        });

        it('tolerates a sparse provider', () => {
            renderEdit(
                existing({
                    version: undefined,
                    category: undefined,
                    default_auth_type: undefined,
                    additional_drop_params: undefined,
                    region: undefined,
                })
            );
            expect(field('Category').value).toBe('');
            expect(field('Additional drop params').value).toBe('');
            expect(field('Region').value).toBe('');
        });

        it('reloads when handed a different provider', () => {
            const { rerender } = renderEdit();
            setField('Display name', 'unsaved');
            rerender(
                <LLProviderForm
                    provider={existing({ id: 'p2', display_name: 'Other' })}
                    onClose={onClose}
                    onSaved={onSaved}
                />
            );
            expect(field('Display name').value).toBe('Other');
        });

        it('tells the audit tab this is not new', () => {
            renderEdit();
            fireEvent.click(screen.getByRole('tab', { name: 'Audit Info' }));
            expect(screen.getByText('audit tab existing')).toBeTruthy();
        });

        it('shows a server error, which can be dismissed', async () => {
            client.updateProvider.mockRejectedValue(new Error('Bad URL'));
            renderEdit();
            click('Save changes');
            const error = await screen.findByText('Bad URL');
            expect(onSaved).not.toHaveBeenCalled();

            const dismiss = error.parentElement?.querySelector('button');
            if (!dismiss) throw new Error('dismiss button not found');
            fireEvent.click(dismiss);
            expect(screen.queryByText('Bad URL')).toBeNull();
        });
    });

    describe('icon', () => {
        function pickFile(file: File) {
            const input = document.querySelector('input[type="file"]');
            if (!input) throw new Error('file input not found');
            fireEvent.change(input, { target: { files: [file] } });
        }
        const file = new File(['x'], 'logo.png', { type: 'image/png' });

        it('uploads straight away and shows the result', async () => {
            client.uploadProviderIcon.mockResolvedValue(
                existing({ has_icon: true, icon_mime_type: 'image/png', icon_size: 2048 })
            );
            renderEdit();
            expect(screen.getByText(/No icon uploaded/)).toBeTruthy();
            pickFile(file);

            expect(await screen.findByText(/Current icon: image\/png/)).toBeTruthy();
            expect(screen.getByText(/2 KB/)).toBeTruthy();
            expect(client.uploadProviderIcon).toHaveBeenCalledWith('p1', file);
            expect(screen.getByRole('button', { name: 'Replace' })).toBeTruthy();
        });

        it('reports a failed upload', async () => {
            client.uploadProviderIcon.mockRejectedValue(new Error('too large'));
            renderEdit();
            pickFile(file);
            expect(await screen.findByText('Icon upload failed: too large')).toBeTruthy();
            expect(screen.getByRole('button', { name: 'Upload' })).toBeTruthy();
        });

        it('removes the icon', async () => {
            client.deleteProviderIcon.mockResolvedValue({});
            renderEdit(existing({ has_icon: true, icon_mime_type: 'image/png' }));
            fireEvent.click(screen.getByTitle('Remove icon'));
            expect(await screen.findByText(/No icon uploaded/)).toBeTruthy();
            expect(client.deleteProviderIcon).toHaveBeenCalledWith('p1');
            expect(screen.queryByTitle('Remove icon')).toBeNull();
        });

        it('keeps the icon and reports a failed removal', async () => {
            client.deleteProviderIcon.mockRejectedValue(new Error('denied'));
            renderEdit(existing({ has_icon: true, icon_mime_type: 'image/png' }));
            fireEvent.click(screen.getByTitle('Remove icon'));
            expect(await screen.findByText('Icon delete failed: denied')).toBeTruthy();
            expect(screen.getByTitle('Remove icon')).toBeTruthy();
        });
    });

    it('shows saving state and blocks a second submit', async () => {
        let finish: (value: unknown) => void = () => {};
        client.updateProvider.mockReturnValue(new Promise(resolve => { finish = resolve; }));
        renderEdit();
        click('Save changes');
        expect((await screen.findByRole<HTMLButtonElement>('button', { name: 'Saving…' })).disabled).toBe(true);
        expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Cancel' }).disabled).toBe(true);
        finish({});
        await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    });

    it('cancels without saving', () => {
        renderCreate();
        click('Cancel');
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(client.createProvider).not.toHaveBeenCalled();
    });
});
