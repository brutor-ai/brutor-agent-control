/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the LLM models tab test share: the gateway client and
 * health-check doubles, the stubs for the tab's children, and the starting
 * state of every case. The page's own cases are Community tests
 * (`components/llm/config/__tests__/LLModelTab.test.tsx`); the cases that need
 * a real Enterprise item on the page are under `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes what they return from here,
 *
 *     vi.mock('../LLModelForm', async () => (await stubs()).formModule());
 *
 * This module does not import the tab: a mock factory that awaits a module
 * which is itself waiting for the mocked module would never settle.
 */

import { useState } from 'react';
import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

export const toastMock = Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    loading: vi.fn(() => 'toast-id'),
});

export const client = {
    listLLMs: vi.fn(),
    listLLMCatalog: vi.fn(),
    createLLM: vi.fn(),
    updateLLM: vi.fn(),
    deleteLLM: vi.fn(),
    copyLLM: vi.fn(),
    testLLMConnection: vi.fn(),
    importLLMCatalogModel: vi.fn(),
};
const gateway = { gatewayClient: client, llmProxyUrl: 'http://proxy.test' };

export const health = {
    isEnabled: true,
    isPaused: false,
    intervalSeconds: 60,
    lastCheckTime: null as Date | null,
    isChecking: false,
    runHealthCheck: vi.fn(),
    healthCheckConfig: null as { ai_settings?: { interval_seconds?: number } } | null,
    refreshConfig: vi.fn(),
};

/** What the page last asked of the health check hook. */
export const healthOptions: { paused?: boolean } = {};

/**
 * What the stubbed form submits (`payload`); each test sets it before
 * pressing save. `refused` is what the last refused save handed the form.
 */
export const formStub: { payload: Record<string, unknown>; refused?: unknown } = { payload: {} };

export const toastModule = () => ({ default: toastMock });

export const useGatewayModule = () => ({ useGateway: () => gateway });

export const useAIModelHealthCheckModule = () => ({
    useAIModelHealthCheck: (options: { paused?: boolean }) => {
        Object.assign(healthOptions, options);
        return health;
    },
});

interface ItemProps {
    model: { name: string };
    onEdit: () => void;
    onDelete: () => void;
    onCopy: () => void;
    onTestConnection: () => void;
    onToggleEnabled: (enabled: boolean) => void;
    isFocused?: boolean;
}

function itemStub(kind: string, { model, onEdit, onDelete, onCopy, onTestConnection, onToggleEnabled, isFocused }: ItemProps) {
    return (
        <div data-testid="model-item" data-kind={kind}>
            <span data-testid="item-name">{model.name}</span>
            {isFocused && <span>focused {model.name}</span>}
            <button onClick={onEdit}>edit {model.name}</button>
            <button onClick={onDelete}>delete {model.name}</button>
            <button onClick={onCopy}>copy {model.name}</button>
            <button onClick={onTestConnection}>test {model.name}</button>
            <button onClick={() => onToggleEnabled(false)}>disable {model.name}</button>
            <button onClick={() => onToggleEnabled(true)}>enable {model.name}</button>
        </div>
    );
}

/** `LLModelCard` (`card`) and `LLModelListItem` (`row`). */
export const itemModule = (kind: 'card' | 'row') => ({ default: (p: ItemProps) => itemStub(kind, p) });

/** `LLModelForm`. */
export function formModule() {
    function FormStub({
        model,
        onSave,
        onClose,
    }: {
        model: { name?: string } | null;
        onSave: (data: Record<string, unknown>) => Promise<void>;
        onClose: () => void;
    }) {
        const [saveError, setSaveError] = useState<string | null>(null);
        return (
            <div>
                form for {model?.name ?? 'a new model'}
                {saveError && <p>form error: {saveError}</p>}
                <button
                    onClick={() => {
                        onSave(formStub.payload).catch((e: unknown) => {
                            formStub.refused = e;
                            setSaveError(e instanceof Error ? e.message : 'unknown');
                        });
                    }}
                >
                    form save
                </button>
                <button onClick={onClose}>form close</button>
            </div>
        );
    }
    return { default: FormStub };
}

/** `LLMCatalogTree`. */
export const catalogTreeModule = () => ({
    default: ({
        entries,
        configuredKeys,
        onImport,
        loading,
    }: {
        entries: Array<{ id: string; name: string }>;
        configuredKeys: Set<string>;
        onImport: (e: { id: string; name: string }) => Promise<void>;
        loading: boolean;
    }) => (
        <div>
            {loading && <p>catalog loading</p>}
            <p>configured: {Array.from(configuredKeys).join(',')}</p>
            {entries.slice(0, 1).map(entry => (
                <button key={entry.id} onClick={() => { onImport(entry).catch(() => {}); }}>
                    tree import {entry.id}
                </button>
            ))}
        </div>
    ),
});

/** `LLMSearchModal`. */
export const searchModalModule = () => ({
    default: ({
        onClose,
        onImportModel,
    }: {
        onClose: () => void;
        onImportModel: (e: { id: string }) => Promise<void>;
    }) => (
        <div>
            search modal
            <button onClick={() => { onImportModel({ id: 'cat-9' }).catch(() => {}); }}>search import</button>
            <button onClick={onClose}>search close</button>
        </div>
    ),
});

/** `HealthCheckHelpModal` (`health help close`). */
export const closeOnlyModalModule = (label: string) => ({
    default: ({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) =>
        isOpen ? <button onClick={onClose}>{label}</button> : null,
});

/** `LLMProxyEndpointModal`. */
export const proxyModalModule = () => ({
    default: ({ isOpen, proxyUrl, onClose }: { isOpen: boolean; proxyUrl: string; onClose: () => void }) =>
        isOpen ? <button onClick={onClose}>proxy modal {proxyUrl}</button> : null,
});

/** `GenericDeleteConfirmationModal`. */
export const deleteModalModule = () => ({
    default: ({
        isOpen,
        itemName,
        onConfirm,
        onClose,
        errorMessage,
    }: {
        isOpen: boolean;
        itemName: string;
        onConfirm: () => void;
        onClose: () => void;
        errorMessage?: string | null;
    }) =>
        isOpen ? (
            <div>
                delete modal for {itemName}
                {errorMessage && <p>{errorMessage}</p>}
                <button onClick={onConfirm}>confirm delete</button>
                <button onClick={onClose}>cancel delete</button>
            </div>
        ) : null,
});

export function llm(id: string, overrides: Record<string, unknown> = {}) {
    return {
        id,
        name: id.toUpperCase(),
        provider: 'openai',
        model_name: `${id}-name`,
        enabled: true,
        performance_metrics: { total_requests: 0, total_tokens_consumed: 0 },
        ...overrides,
    };
}

/** The models the gateway lists; none when called with no argument. */
export function setModels(...models: Array<ReturnType<typeof llm>>) {
    client.listLLMs.mockResolvedValue({ models: Object.fromEntries(models.map(m => [m.id, m])) });
}

/** "Add New Model" appears twice on an empty tab: in the action bar and the empty state. */
export function clickAddNew() {
    fireEvent.click(screen.getAllByRole('button', { name: 'Add New Model' })[0]);
}

export function click(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}

/** How every case starts: no model, a healthy probe, health checks enabled and idle. For a `beforeEach`. */
export function resetLLModelTab(): void {
    vi.clearAllMocks();
    localStorage.clear();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'log').mockImplementation(() => {});
    for (const fn of Object.values(client)) fn.mockReset();
    toastMock.loading.mockReturnValue('toast-id');
    setModels();
    client.testLLMConnection.mockResolvedValue({ status: 'healthy', response_time_ms: 12.3 });
    Object.assign(health, {
        isEnabled: true,
        isPaused: false,
        lastCheckTime: null,
        isChecking: false,
        healthCheckConfig: null,
    });
    health.runHealthCheck.mockResolvedValue(undefined);
    health.refreshConfig.mockResolvedValue(undefined);
    formStub.payload = {};
    formStub.refused = undefined;
}
