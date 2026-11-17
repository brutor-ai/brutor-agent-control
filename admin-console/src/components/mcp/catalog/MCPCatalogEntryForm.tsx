/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import {
    X,
    Plus,
    Trash2,
    Save,
    AlertCircle
} from 'lucide-react';
import {
    MCPCatalogEntry,
    CatalogEntryCreateRequest,
    CatalogEntryUpdateRequest,
    CatalogEnvVar,
    CatalogToolPreview,
    CatalogRemoteConfig,
    CatalogContainerizedConfig,
    CatalogHeader
} from '../../../lib/mcpCatalogTypes';
import { errorMessage } from '../../../lib/narrow';
import { asyncHandler } from '../../../lib/asyncSafe';

interface MCPCatalogEntryFormProps {
    isOpen: boolean;
    onClose: () => void;
    onSave: () => void | Promise<void>;
    entry?: MCPCatalogEntry;
}

const emptyEnvVar = (): CatalogEnvVar => ({
    key: '',
    name: '',
    required: false,
    sensitive: false,
    description: '',
});

const emptyToolPreview = (): CatalogToolPreview => ({
    name: '',
    description: '',
    params: {},
});

const emptyHeader = (): CatalogHeader => ({
    key: '',
    name: '',
    required: false,
    sensitive: false,
});

const MCPCatalogEntryForm: React.FC<MCPCatalogEntryFormProps> = ({ isOpen, onClose, onSave, entry }) => {
    const isEditMode = !!entry;

    // Basic fields
    const [name, setName] = useState('');
    const [slug, setSlug] = useState('');
    // Once the operator types a slug, the name stops driving it.
    const [slugTouched, setSlugTouched] = useState(false);
    const [shortDescription, setShortDescription] = useState('');
    const [description, setDescription] = useState('');
    const [iconUrl, setIconUrl] = useState('');
    const [repoUrl, setRepoUrl] = useState('');
    const [runtime, setRuntime] = useState<'remote' | 'containerized'>('remote');
    const [authPattern, setAuthPattern] = useState<'oauth' | 'api_key' | 'header' | 'none'>('none');
    const [categoriesInput, setCategoriesInput] = useState('');
    const [enabled, setEnabled] = useState(true);

    // Remote config
    const [remoteFixedURL, setRemoteFixedURL] = useState('');
    const [remoteHostname, setRemoteHostname] = useState('');
    const [remoteHeaders, setRemoteHeaders] = useState<CatalogHeader[]>([]);

    // Containerized config
    const [containerImage, setContainerImage] = useState('');
    const [containerPort, setContainerPort] = useState(8080);
    const [containerPath, setContainerPath] = useState('/mcp');
    const [containerArgs, setContainerArgs] = useState('');

    // Env vars
    const [envVars, setEnvVars] = useState<CatalogEnvVar[]>([]);

    // Tool previews
    const [toolPreviews, setToolPreviews] = useState<CatalogToolPreview[]>([]);

    // State
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Populate form for edit mode
    useEffect(() => {
        setSlugTouched(false);
        if (entry) {
            setName(entry.name);
            setSlug(entry.slug);
            setShortDescription(entry.short_description || '');
            setDescription(entry.description || '');
            setIconUrl(entry.icon_url || '');
            setRepoUrl(entry.repo_url || '');
            setRuntime(entry.runtime);
            setAuthPattern(entry.auth_pattern);
            setCategoriesInput((entry.categories || []).join(', '));
            setEnabled(entry.enabled);
            setEnvVars(entry.env_vars || []);
            setToolPreviews(entry.tool_preview || []);

            if (entry.remote_config) {
                setRemoteFixedURL(entry.remote_config.fixedURL || '');
                setRemoteHostname(entry.remote_config.hostname || '');
                setRemoteHeaders(entry.remote_config.headers || []);
            }
            if (entry.containerized_config) {
                setContainerImage(entry.containerized_config.image);
                setContainerPort(entry.containerized_config.port);
                setContainerPath(entry.containerized_config.path);
                setContainerArgs((entry.containerized_config.args || []).join(' '));
            }
        } else {
            // Reset for create mode
            setName('');
            setSlug('');
            setShortDescription('');
            setDescription('');
            setIconUrl('');
            setRepoUrl('');
            setRuntime('remote');
            setAuthPattern('none');
            setCategoriesInput('');
            setEnabled(true);
            setRemoteFixedURL('');
            setRemoteHostname('');
            setRemoteHeaders([]);
            setContainerImage('');
            setContainerPort(8080);
            setContainerPath('/mcp');
            setContainerArgs('');
            setEnvVars([]);
            setToolPreviews([]);
        }
    }, [entry]);

    // Auto-generate slug from name
    useEffect(() => {
        if (!isEditMode && !slugTouched) {
            setSlug(name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, ''));
        }
    }, [name, isEditMode, slugTouched]);

    const buildPayload = (): CatalogEntryCreateRequest | CatalogEntryUpdateRequest => {
        const categories = categoriesInput
            .split(',')
            .map((c) => c.trim())
            .filter(Boolean);

        const remoteConfig: CatalogRemoteConfig | undefined =
            runtime === 'remote'
                ? {
                    fixedURL: remoteFixedURL || undefined,
                    hostname: remoteHostname || undefined,
                    headers: remoteHeaders.length > 0 ? remoteHeaders : undefined,
                }
                : undefined;

        const containerizedConfig: CatalogContainerizedConfig | undefined =
            runtime === 'containerized'
                ? {
                    image: containerImage,
                    port: containerPort,
                    path: containerPath,
                    args: containerArgs ? containerArgs.split(/\s+/).filter(Boolean) : undefined,
                }
                : undefined;

        return {
            name,
            slug,
            short_description: shortDescription || undefined,
            description: description || undefined,
            icon_url: iconUrl || undefined,
            repo_url: repoUrl || undefined,
            runtime,
            remote_config: remoteConfig,
            containerized_config: containerizedConfig,
            env_vars: envVars.length > 0 ? envVars : undefined,
            tool_preview: toolPreviews.length > 0 ? toolPreviews : undefined,
            categories: categories.length > 0 ? categories : undefined,
            auth_pattern: authPattern,
            enabled,
        };
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!name.trim() || !slug.trim()) {
            setError('Name and slug are required.');
            return;
        }
        setSaving(true);
        setError(null);
        try {
            // The parent (MCPCatalogTab) handles the actual API call
            // We pass back the payload via a custom event or callback
            // For simplicity, store payload on the form element
            const payload = buildPayload();
            const form = e.target as HTMLFormElement;
            (form as any).__payload = payload;
            await onSave();
        } catch (err) {
            setError(errorMessage(err, 'Failed to save catalog entry.'));
        } finally {
            setSaving(false);
        }
    };

    // Env var helpers
    const addEnvVar = () => setEnvVars([...envVars, emptyEnvVar()]);
    const removeEnvVar = (idx: number) => setEnvVars(envVars.filter((_, i) => i !== idx));
    const updateEnvVar = (idx: number, field: keyof CatalogEnvVar, value: any) => {
        setEnvVars(envVars.map((ev, i) => (i === idx ? { ...ev, [field]: value } : ev)));
    };

    // Tool preview helpers
    const addToolPreview = () => setToolPreviews([...toolPreviews, emptyToolPreview()]);
    const removeToolPreview = (idx: number) => setToolPreviews(toolPreviews.filter((_, i) => i !== idx));
    const updateToolPreview = (idx: number, field: keyof CatalogToolPreview, value: any) => {
        setToolPreviews(toolPreviews.map((tp, i) => (i === idx ? { ...tp, [field]: value } : tp)));
    };

    // Header helpers
    const addHeader = () => setRemoteHeaders([...remoteHeaders, emptyHeader()]);
    const removeHeader = (idx: number) => setRemoteHeaders(remoteHeaders.filter((_, i) => i !== idx));
    const updateHeader = (idx: number, field: keyof CatalogHeader, value: any) => {
        setRemoteHeaders(remoteHeaders.map((h, i) => (i === idx ? { ...h, [field]: value } : h)));
    };

    // Expose payload for parent to read
    const getPayload = buildPayload;
    // Store on window for parent access (simple approach)
    useEffect(() => {
        (window as any).__mcpCatalogFormPayload = getPayload;
        return () => { delete (window as any).__mcpCatalogFormPayload; };
    });

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-lg shadow-xl w-full max-w-3xl max-h-[90vh] flex flex-col">
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-strong">
                    <h2 className="text-lg font-semibold text-default">
                        {isEditMode ? 'Edit Catalog Entry' : 'Add Catalog Entry'}
                    </h2>
                    <button
                        onClick={onClose}
                        className="p-2 hover:bg-surface-alt rounded-md transition-colors"
                    >
                        <X className="w-5 h-5 text-secondary" />
                    </button>
                </div>

                {/* Form */}
                <div className="scroll-region p-6">
                    {error && (
                        <div className="flex items-center space-x-2 p-3 mb-4 bg-red-50 border border-red-200 rounded-md">
                            <AlertCircle className="w-4 h-4 text-red-600 flex-shrink-0" />
                            <span className="text-sm text-red-700">{error}</span>
                        </div>
                    )}

                    <div className="space-y-5">
                        {/* Name & Slug */}
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Name *</label>
                                <input
                                    type="text"
                                    value={name}
                                    onChange={(e) => setName(e.target.value)}
                                    placeholder="My MCP Server"
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Slug *</label>
                                <input
                                    type="text"
                                    value={slug}
                                    onChange={(e) => { setSlug(e.target.value); setSlugTouched(e.target.value !== ''); }}
                                    placeholder="my-mcp-server"
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm font-mono"
                                />
                            </div>
                        </div>

                        {/* Short Description */}
                        <div>
                            <label className="block text-sm font-medium text-default mb-1">Short Description</label>
                            <input
                                type="text"
                                value={shortDescription}
                                onChange={(e) => setShortDescription(e.target.value)}
                                placeholder="Brief one-line description"
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                            />
                        </div>

                        {/* Description */}
                        <div>
                            <label className="block text-sm font-medium text-default mb-1">Description</label>
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                placeholder="Full description of this MCP server..."
                                rows={4}
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                            />
                        </div>

                        {/* Icon URL & Repo URL */}
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Icon URL</label>
                                <input
                                    type="url"
                                    value={iconUrl}
                                    onChange={(e) => setIconUrl(e.target.value)}
                                    placeholder="https://example.com/icon.png"
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Repo URL</label>
                                <input
                                    type="url"
                                    value={repoUrl}
                                    onChange={(e) => setRepoUrl(e.target.value)}
                                    placeholder="https://github.com/org/repo"
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                />
                            </div>
                        </div>

                        {/* Runtime & Auth */}
                        <div className="grid grid-cols-2 gap-4">
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Runtime *</label>
                                <div className="flex items-center space-x-4 mt-2">
                                    <label className="flex items-center space-x-2 cursor-pointer">
                                        <input
                                            type="radio"
                                            name="runtime"
                                            value="remote"
                                            checked={runtime === 'remote'}
                                            onChange={() => setRuntime('remote')}
                                            className="text-primary-500 focus:ring-primary"
                                        />
                                        <span className="text-sm text-default">Remote</span>
                                    </label>
                                    <label className="flex items-center space-x-2 cursor-pointer">
                                        <input
                                            type="radio"
                                            name="runtime"
                                            value="containerized"
                                            checked={runtime === 'containerized'}
                                            onChange={() => setRuntime('containerized')}
                                            className="text-primary-500 focus:ring-primary"
                                        />
                                        <span className="text-sm text-default">Containerized</span>
                                    </label>
                                </div>
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-default mb-1">Auth Pattern</label>
                                <select
                                    value={authPattern}
                                    onChange={(e) => setAuthPattern(e.target.value as any)}
                                    className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                >
                                    <option value="none">None</option>
                                    <option value="oauth">OAuth</option>
                                    <option value="api_key">API Key</option>
                                    <option value="header">Header</option>
                                </select>
                            </div>
                        </div>

                        {/* Remote Config (conditional) */}
                        {runtime === 'remote' && (
                            <div className="border border-strong rounded-md p-4">
                                <h3 className="text-sm font-semibold text-default mb-3">Remote Configuration</h3>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-xs font-medium text-secondary mb-1">Fixed URL</label>
                                        <input
                                            type="url"
                                            value={remoteFixedURL}
                                            onChange={(e) => setRemoteFixedURL(e.target.value)}
                                            placeholder="https://mcp.example.com/sse"
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-xs font-medium text-secondary mb-1">Hostname</label>
                                        <input
                                            type="text"
                                            value={remoteHostname}
                                            onChange={(e) => setRemoteHostname(e.target.value)}
                                            placeholder="mcp.example.com"
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                        />
                                    </div>

                                    {/* Headers */}
                                    <div>
                                        <div className="flex items-center justify-between mb-2">
                                            <label className="text-xs font-medium text-secondary">Headers</label>
                                            <button
                                                type="button"
                                                onClick={addHeader}
                                                className="flex items-center space-x-1 text-xs text-primary-600 hover:text-primary-700"
                                            >
                                                <Plus className="w-3 h-3" />
                                                <span>Add Header</span>
                                            </button>
                                        </div>
                                        {remoteHeaders.map((header, idx) => (
                                            <div key={idx} className="flex items-center space-x-2 mb-2">
                                                <input
                                                    type="text"
                                                    value={header.key}
                                                    onChange={(e) => updateHeader(idx, 'key', e.target.value)}
                                                    placeholder="Header key"
                                                    className="flex-1 px-2 py-1.5 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary min-h-0 min-w-0"
                                                />
                                                <input
                                                    type="text"
                                                    value={header.name || ''}
                                                    onChange={(e) => updateHeader(idx, 'name', e.target.value)}
                                                    placeholder="Display name"
                                                    className="flex-1 px-2 py-1.5 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary min-h-0 min-w-0"
                                                />
                                                <label className="flex items-center space-x-1 text-xs">
                                                    <input
                                                        type="checkbox"
                                                        checked={header.required}
                                                        onChange={(e) => updateHeader(idx, 'required', e.target.checked)}
                                                    />
                                                    <span>Req</span>
                                                </label>
                                                <label className="flex items-center space-x-1 text-xs">
                                                    <input
                                                        type="checkbox"
                                                        checked={header.sensitive}
                                                        onChange={(e) => updateHeader(idx, 'sensitive', e.target.checked)}
                                                    />
                                                    <span>Sens</span>
                                                </label>
                                                <button
                                                    type="button"
                                                    onClick={() => removeHeader(idx)}
                                                    className="p-1 text-red-500 hover:text-red-700"
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* Containerized Config (conditional) */}
                        {runtime === 'containerized' && (
                            <div className="border border-strong rounded-md p-4">
                                <h3 className="text-sm font-semibold text-default mb-3">Container Configuration</h3>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-xs font-medium text-secondary mb-1">Image *</label>
                                        <input
                                            type="text"
                                            value={containerImage}
                                            onChange={(e) => setContainerImage(e.target.value)}
                                            placeholder="mcp/fetch:latest"
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm font-mono"
                                        />
                                    </div>
                                    <div className="grid grid-cols-2 gap-3">
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">Port</label>
                                            <input
                                                type="number"
                                                value={containerPort}
                                                onChange={(e) => setContainerPort(parseInt(e.target.value) || 8080)}
                                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                                            />
                                        </div>
                                        <div>
                                            <label className="block text-xs font-medium text-secondary mb-1">Path</label>
                                            <input
                                                type="text"
                                                value={containerPath}
                                                onChange={(e) => setContainerPath(e.target.value)}
                                                placeholder="/mcp"
                                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm font-mono"
                                            />
                                        </div>
                                    </div>
                                    <div>
                                        <label className="block text-xs font-medium text-secondary mb-1">Args (space-separated)</label>
                                        <input
                                            type="text"
                                            value={containerArgs}
                                            onChange={(e) => setContainerArgs(e.target.value)}
                                            placeholder="--verbose --port 8080"
                                            className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm font-mono"
                                        />
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* Categories */}
                        <div>
                            <label className="block text-sm font-medium text-default mb-1">Categories</label>
                            <input
                                type="text"
                                value={categoriesInput}
                                onChange={(e) => setCategoriesInput(e.target.value)}
                                placeholder="search, productivity, dev-tools (comma-separated)"
                                className="w-full px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary text-sm"
                            />
                            <p className="text-xs text-muted mt-1">Separate multiple categories with commas</p>
                        </div>

                        {/* Enabled */}
                        <div>
                            <label className="flex items-center space-x-2 cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={enabled}
                                    onChange={(e) => setEnabled(e.target.checked)}
                                    className="rounded border-strong"
                                />
                                <span className="text-sm text-default">Enabled</span>
                            </label>
                        </div>

                        {/* Environment Variables */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label className="text-sm font-medium text-default">Environment Variables</label>
                                <button
                                    type="button"
                                    onClick={addEnvVar}
                                    className="flex items-center space-x-1 text-xs text-primary-600 hover:text-primary-700"
                                >
                                    <Plus className="w-3 h-3" />
                                    <span>Add Variable</span>
                                </button>
                            </div>
                            {envVars.length === 0 && (
                                <p className="text-xs text-muted">No environment variables defined.</p>
                            )}
                            {envVars.map((ev, idx) => (
                                <div key={idx} className="flex items-start space-x-2 mb-2 p-2 bg-surface-alt rounded border border-strong">
                                    <div className="flex-1 space-y-1 min-h-0 min-w-0">
                                        <div className="grid grid-cols-2 gap-2">
                                            <input
                                                type="text"
                                                value={ev.key}
                                                onChange={(e) => updateEnvVar(idx, 'key', e.target.value)}
                                                placeholder="ENV_KEY"
                                                className="px-2 py-1.5 border border-strong rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary"
                                            />
                                            <input
                                                type="text"
                                                value={ev.name}
                                                onChange={(e) => updateEnvVar(idx, 'name', e.target.value)}
                                                placeholder="Display name"
                                                className="px-2 py-1.5 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                                            />
                                        </div>
                                        <input
                                            type="text"
                                            value={ev.description}
                                            onChange={(e) => updateEnvVar(idx, 'description', e.target.value)}
                                            placeholder="Description"
                                            className="w-full px-2 py-1.5 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                                        />
                                        <div className="flex items-center space-x-4">
                                            <label className="flex items-center space-x-1 text-xs">
                                                <input
                                                    type="checkbox"
                                                    checked={ev.required}
                                                    onChange={(e) => updateEnvVar(idx, 'required', e.target.checked)}
                                                />
                                                <span>Required</span>
                                            </label>
                                            <label className="flex items-center space-x-1 text-xs">
                                                <input
                                                    type="checkbox"
                                                    checked={ev.sensitive}
                                                    onChange={(e) => updateEnvVar(idx, 'sensitive', e.target.checked)}
                                                />
                                                <span>Sensitive</span>
                                            </label>
                                        </div>
                                    </div>
                                    <button
                                        type="button"
                                        onClick={() => removeEnvVar(idx)}
                                        className="p-1 text-red-500 hover:text-red-700 mt-1"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>

                        {/* Tool Previews */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <label className="text-sm font-medium text-default">Tool Previews</label>
                                <button
                                    type="button"
                                    onClick={addToolPreview}
                                    className="flex items-center space-x-1 text-xs text-primary-600 hover:text-primary-700"
                                >
                                    <Plus className="w-3 h-3" />
                                    <span>Add Tool</span>
                                </button>
                            </div>
                            {toolPreviews.length === 0 && (
                                <p className="text-xs text-muted">No tool previews defined.</p>
                            )}
                            {toolPreviews.map((tp, idx) => (
                                <div key={idx} className="flex items-start space-x-2 mb-2">
                                    <input
                                        type="text"
                                        value={tp.name}
                                        onChange={(e) => updateToolPreview(idx, 'name', e.target.value)}
                                        placeholder="tool_name"
                                        className="flex-1 px-2 py-1.5 border border-strong rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary min-h-0 min-w-0"
                                    />
                                    <input
                                        type="text"
                                        value={tp.description}
                                        onChange={(e) => updateToolPreview(idx, 'description', e.target.value)}
                                        placeholder="Tool description"
                                        className="flex-[2] px-2 py-1.5 border border-strong rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                                    />
                                    <button
                                        type="button"
                                        onClick={() => removeToolPreview(idx)}
                                        className="p-1 text-red-500 hover:text-red-700"
                                    >
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                {/* Footer */}
                <div className="flex items-center justify-end space-x-3 px-6 py-4 border-t border-strong">
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="px-4 py-2 text-sm text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt transition-colors disabled:opacity-50"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSubmit, 'click')}
                        disabled={saving || !name.trim() || !slug.trim()}
                        className="px-4 py-2 text-sm bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        {saving ? (
                            <>
                                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white" />
                                <span>Saving...</span>
                            </>
                        ) : (
                            <>
                                <Save className="w-4 h-4" />
                                <span>{isEditMode ? 'Update Entry' : 'Create Entry'}</span>
                            </>
                        )}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default MCPCatalogEntryForm;
