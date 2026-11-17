/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The one question the catalog import asks: the API URL of an entry the
 * control plane marks `requires_api_url`.
 *
 * Such an entry is a template (an Azure resource, a Databricks workspace, a
 * self-hosted server): the gateway knows no address for it, and the control
 * plane refuses the import without one (422). The dialog asks before
 * anything is sent, sends the address with the import, and shows a refusal
 * next to the field.
 */

import React, { useState } from 'react';
import { Link2, Loader2, Plus } from 'lucide-react';

import { asyncHandler } from '../../../lib/asyncSafe';
import { errorMessage } from '../../../lib/narrow';
import type { LLMCatalogEntry } from '../../../lib/llmTypes';
import { apiUrlRefusal, modelSaveRefusalText } from './providerConnection';

interface LLMCatalogApiUrlDialogProps {
    entry: LLMCatalogEntry;
    /** What the control plane said when it refused the entry sent without an address, if that is why the dialog is open. */
    refusal?: string;
    /** Imports the entry with this address. Rejects with what the API client threw. */
    onImport: (apiUrl: string) => Promise<void>;
    onCancel: () => void;
}

const LLMCatalogApiUrlDialog: React.FC<LLMCatalogApiUrlDialogProps> = ({ entry, refusal, onImport, onCancel }) => {
    const [apiUrl, setApiUrl] = useState('');
    const [error, setError] = useState<string | undefined>(refusal);
    const [adding, setAdding] = useState(false);
    const name = entry.display_name || entry.model_name || entry.id;

    const submit = async (event: React.FormEvent) => {
        event.preventDefault();
        const typed = apiUrl.trim();
        if (!typed) {
            setError('API URL is required.');
            return;
        }
        setAdding(true);
        setError(undefined);
        try {
            await onImport(typed);
        } catch (caught) {
            setError(apiUrlRefusal(caught) ?? `Failed to add model: ${modelSaveRefusalText(caught) ?? errorMessage(caught, 'unknown error')}`);
        } finally {
            setAdding(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[60] p-4">
            <form
                onSubmit={asyncHandler(submit, 'add model')}
                noValidate
                className="bg-surface rounded-lg shadow-xl w-full max-w-lg"
            >
                <div className="flex items-center space-x-3 px-5 py-4 border-b border-default">
                    <Link2 className="w-5 h-5 text-primary-600 flex-shrink-0" />
                    <h3 className="text-lg font-semibold text-default">{name} needs an API URL</h3>
                </div>

                <div className="px-5 py-4 space-y-3">
                    <p className="text-sm text-secondary">
                        The model is added with the address of your own endpoint: the gateway knows no address for
                        a {entry.mode} model of provider &quot;{entry.provider_key}&quot;, and would refuse every request for
                        it without one.
                    </p>
                    <div>
                        <label htmlFor="catalogImportApiUrl" className="block text-sm font-medium text-secondary mb-2">
                            API URL *
                        </label>
                        <input
                            id="catalogImportApiUrl"
                            type="url"
                            value={apiUrl}
                            onChange={(e) => {
                                setApiUrl(e.target.value);
                                setError(undefined);
                            }}
                            required
                            autoFocus
                            aria-invalid={error !== undefined}
                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 font-mono text-sm"
                            placeholder="https://your-endpoint.example/v1"
                        />
                        {error && (
                            <p role="alert" className="mt-1 text-sm text-red-600 whitespace-pre-wrap">
                                {error}
                            </p>
                        )}
                    </div>
                </div>

                <div className="flex items-center justify-end space-x-3 px-5 py-3 border-t border-default bg-surface-alt rounded-b-lg">
                    <button
                        type="button"
                        onClick={onCancel}
                        disabled={adding}
                        className="px-4 py-2 text-sm font-medium text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        disabled={adding}
                        className="px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 flex items-center space-x-1.5"
                    >
                        {adding ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                        <span>Add model</span>
                    </button>
                </div>
            </form>
        </div>
    );
};

export default LLMCatalogApiUrlDialog;
