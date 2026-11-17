/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * SkillSourcesTab — git-sourced skill repositories admin UI.
 *
 * Operators paste a GitHub URL; the Rust scheduler in gateway-core
 * walks it on a 1h cadence and discovers SKILL.md files. This tab
 * lets the admin register sources, edit them, watch sync status, and
 * trigger immediate refreshes.
 *
 * Polling: while any source has status === 'syncing' the table
 * re-fetches every 3 s until everyone is idle/failed. Sync is fast
 * (one GitHub tree call + N blob fetches) so this is fine.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Plus,
    RefreshCw,
    Pencil,
    Trash2,
    AlertTriangle,
    CheckCircle2,
    Clock,
    GitBranch,
} from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import type {
    SkillSourceResponse,
    SkillSourceCreateRequest,
    SkillSourceUpdateRequest,
    SkillSyncStatus,
} from '../../lib/agentSkillTypes';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

interface Props {
    /** Optional callback when a sync completes — parent can refresh
     *  the skills list to show newly-ingested rows. */
    onSyncComplete?: () => void;
}

/** Narrow `unknown` from a `catch` to a printable string. ESLint
 *  rejects `catch (e: any)` under `no-explicit-any`. */
const errMsg = (e: unknown): string =>
    e instanceof Error ? e.message : String(e);

const SYNC_POLL_INTERVAL_MS = 3000;
const STATUS_LABEL: Record<SkillSyncStatus, string> = {
    idle: 'Idle',
    syncing: 'Syncing…',
    failed: 'Failed',
};
const STATUS_CLASSES: Record<SkillSyncStatus, string> = {
    idle: 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-200 dark:border-emerald-900',
    syncing: 'bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/40 dark:text-sky-200 dark:border-sky-900',
    failed: 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/40 dark:text-rose-200 dark:border-rose-900',
};


export const SkillSourcesTab: React.FC<Props> = ({ onSyncComplete }) => {
    const { gatewayClient } = useGateway();
    const [sources, setSources] = useState<SkillSourceResponse[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [editing, setEditing] = useState<SkillSourceResponse | null>(null);
    const [creating, setCreating] = useState(false);

    const fetchSources = useCallback(async () => {
        try {
            const resp = await gatewayClient.listSkillSources();
            setSources(resp.sources);
            setError(null);
            return resp.sources;
        } catch (e) {
            setError(errMsg(e) || 'Failed to list skill sources');
            return null;
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    useEffect(() => {
        runBackground(fetchSources(), 'sources');
    }, [fetchSources]);

    // Poll while any source is mid-sync. Stops as soon as everyone
    // settles back to idle/failed so we're not hammering the DB at rest.
    const anySyncing = useMemo(
        () => sources.some((s) => s.status === 'syncing'),
        [sources],
    );
    useEffect(() => {
        if (!anySyncing) return;
        const id = window.setInterval(() => {
            runBackground(fetchSources().then((fresh) => {
                // If a sync just settled, let the parent refresh too. This reads the
                // fresh list: the `sources` this closure holds always contains the
                // syncing row that started the poll, so the callback never fired.
                if (onSyncComplete && fresh && !fresh.some((s) => s.status === 'syncing')) {
                    onSyncComplete();
                }
            }), 'sources');
        }, SYNC_POLL_INTERVAL_MS);
        return () => window.clearInterval(id);
    }, [anySyncing, fetchSources, onSyncComplete]);

    const handleRefresh = async (id: string) => {
        try {
            await gatewayClient.refreshSkillSource(id);
            await fetchSources();
        } catch (e) {
            setError(errMsg(e) || 'Refresh failed');
        }
    };

    const handleDelete = async (s: SkillSourceResponse) => {
        if (
            !window.confirm(
                `Delete skill source "${s.name}"?\n\n` +
                    `This removes the registration only — skills already ` +
                    `discovered from this repo stay in your catalog (marked ` +
                    `as Manual). The ${s.discovered_skill_count} discovered ` +
                    `skill(s) become independent of the source.`,
            )
        ) {
            return;
        }
        try {
            await gatewayClient.deleteSkillSource(s.id);
            await fetchSources();
        } catch (e) {
            setError(errMsg(e) || 'Delete failed');
        }
    };

    if (loading) {
        return <div className="p-4 text-muted">Loading skill sources…</div>;
    }

    return (
        <div className="p-4 space-y-4">
            <div className="flex items-start justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default flex items-center gap-2">
                        <GitBranch className="w-5 h-5" />
                        Skill Sources
                    </h2>
                    <p className="text-sm text-muted mt-1 max-w-2xl">
                        Register a GitHub repository of <code>SKILL.md</code>{' '}
                        files. The proxy walks each source on its configured
                        cadence and ingests discovered skills into your catalog
                        (as drafts by default — flip{' '}
                        <strong>Auto-publish</strong> to have them land directly
                        in PUBLISHED state).
                    </p>
                </div>
                <button
                    onClick={() => setCreating(true)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-primary text-white rounded-md hover:bg-primary-600"
                >
                    <Plus className="w-4 h-4" />
                    Register source
                </button>
            </div>

            {error && (
                <div className="px-3 py-2 text-sm rounded border bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/40 dark:text-rose-200 dark:border-rose-900">
                    {error}
                </div>
            )}

            {sources.length === 0 ? (
                <div className="border border-dashed border-default rounded-md p-8 text-center text-muted">
                    No skill sources registered yet. Click{' '}
                    <strong>Register source</strong> above to add a GitHub repo.
                </div>
            ) : (
                <div className="overflow-x-auto border border-default rounded-md">
                    <table className="min-w-full text-sm">
                        <thead className={`${TABLE_THEAD} text-left`}>
                            <tr>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Name</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Repository</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Status</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Skills</th>
                                <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Last synced</th>
                                <th className={`px-3 py-2 w-32 text-right ${TABLE_TH_TYPE}`}>
                                    Actions
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-default">
                            {sources.map((s) => (
                                <tr key={s.id} className="hover:bg-surface-alt/40">
                                    <td className="px-3 py-2 align-top">
                                        <div className="font-medium text-default">
                                            {s.name}
                                        </div>
                                        {!s.enabled && (
                                            <div className="text-xs text-muted mt-0.5">
                                                Disabled — paused by operator
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 align-top">
                                        <a
                                            href={s.repo_url}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="text-primary hover:underline break-all"
                                        >
                                            {s.repo_url.replace(
                                                /^https:\/\/github\.com\//,
                                                '',
                                            )}
                                        </a>
                                        <div className="text-xs text-muted">
                                            @ {s.git_ref}
                                            {s.subpath ? ` · ${s.subpath}` : ''}
                                        </div>
                                    </td>
                                    <td className="px-3 py-2 align-top">
                                        <span
                                            className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded-md border ${
                                                STATUS_CLASSES[s.status]
                                            }`}
                                        >
                                            {s.status === 'idle' && (
                                                <CheckCircle2 className="w-3 h-3" />
                                            )}
                                            {s.status === 'syncing' && (
                                                <RefreshCw className="w-3 h-3 animate-spin" />
                                            )}
                                            {s.status === 'failed' && (
                                                <AlertTriangle className="w-3 h-3" />
                                            )}
                                            {STATUS_LABEL[s.status]}
                                        </span>
                                        {s.sync_error && (
                                            <div
                                                className="text-xs text-rose-700 dark:text-rose-300 mt-1 max-w-md truncate"
                                                title={s.sync_error}
                                            >
                                                {s.sync_error}
                                            </div>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 align-top tabular-nums">
                                        {s.discovered_skill_count}
                                    </td>
                                    <td className="px-3 py-2 align-top">
                                        {s.last_synced_at ? (
                                            <span
                                                className="text-xs text-muted flex items-center gap-1"
                                                title={s.last_synced_at}
                                            >
                                                <Clock className="w-3 h-3" />
                                                {new Date(
                                                    s.last_synced_at,
                                                ).toLocaleString()}
                                            </span>
                                        ) : (
                                            <span className="text-xs text-muted">
                                                never
                                            </span>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 align-top">
                                        <div className="flex items-center justify-end gap-1">
                                            <button
                                                onClick={asyncHandler(() => handleRefresh(s.id), 'click')}
                                                disabled={s.status === 'syncing'}
                                                className="p-1 text-muted hover:text-default disabled:opacity-30"
                                                title="Trigger immediate sync"
                                            >
                                                <RefreshCw className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={() => setEditing(s)}
                                                className="p-1 text-muted hover:text-default"
                                                title="Edit"
                                            >
                                                <Pencil className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => handleDelete(s), 'click')}
                                                className="p-1 text-muted hover:text-rose-600"
                                                title="Delete"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {(creating || editing) && (
                <SkillSourceEditModal
                    source={editing}
                    onClose={() => {
                        setCreating(false);
                        setEditing(null);
                    }}
                    onSaved={() => {
                        setCreating(false);
                        setEditing(null);
                        runBackground(fetchSources(), 'sources');
                    }}
                />
            )}
        </div>
    );
};


// ----------------------------------------------------------------------
// Edit modal
// ----------------------------------------------------------------------

interface ModalProps {
    /** null = create mode; non-null = edit mode (repo_url/git_ref locked) */
    source: SkillSourceResponse | null;
    onClose: () => void;
    onSaved: () => void;
}

const SkillSourceEditModal: React.FC<ModalProps> = ({
    source,
    onClose,
    onSaved,
}) => {
    const { gatewayClient } = useGateway();
    const isEdit = source !== null;
    const [name, setName] = useState(source?.name ?? '');
    const [repoUrl, setRepoUrl] = useState(source?.repo_url ?? '');
    const [gitRef, setGitRef] = useState(source?.git_ref ?? 'main');
    const [subpath, setSubpath] = useState(source?.subpath ?? '');
    const [enabled, setEnabled] = useState(source?.enabled ?? true);
    const [autoPublish, setAutoPublish] = useState(source?.auto_publish ?? false);
    const [intervalSecs, setIntervalSecs] = useState(
        source?.sync_interval_seconds ?? 3600,
    );
    const [saving, setSaving] = useState(false);
    const [err, setErr] = useState<string | null>(null);

    const handleSave = async () => {
        setSaving(true);
        setErr(null);
        try {
            if (isEdit && source) {
                const body: SkillSourceUpdateRequest = {
                    name,
                    subpath: subpath || null,
                    enabled,
                    sync_interval_seconds: intervalSecs,
                    auto_publish: autoPublish,
                };
                await gatewayClient.updateSkillSource(source.id, body);
            } else {
                const body: SkillSourceCreateRequest = {
                    name,
                    repo_url: repoUrl,
                    git_ref: gitRef,
                    subpath: subpath || null,
                    enabled,
                    sync_interval_seconds: intervalSecs,
                    auto_publish: autoPublish,
                };
                await gatewayClient.createSkillSource(body);
            }
            onSaved();
        } catch (e) {
            setErr(errMsg(e) || 'Save failed');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
            <div className="bg-surface text-default rounded-lg shadow-xl max-w-lg w-full mx-4 p-6 space-y-4">
                <h3 className="text-lg font-semibold">
                    {isEdit ? 'Edit skill source' : 'Register skill source'}
                </h3>

                {err && (
                    <div className="text-sm px-3 py-2 rounded bg-rose-50 text-rose-700 border border-rose-200 dark:bg-rose-950/40 dark:text-rose-200 dark:border-rose-900">
                        {err}
                    </div>
                )}

                <div className="space-y-3">
                    <div>
                        <label className="block text-sm font-medium mb-1">
                            Name
                        </label>
                        <input
                            type="text"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            className="w-full px-2 py-1.5 text-sm border border-default rounded bg-input"
                            placeholder="Anthropic example skills"
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium mb-1">
                            GitHub URL
                        </label>
                        <input
                            type="text"
                            value={repoUrl}
                            onChange={(e) => setRepoUrl(e.target.value)}
                            disabled={isEdit}
                            className="w-full px-2 py-1.5 text-sm border border-default rounded bg-input disabled:opacity-60"
                            placeholder="https://github.com/anthropics/skill-examples"
                        />
                        {isEdit && (
                            <p className="text-xs text-muted mt-0.5">
                                Repo URL is immutable after create. Delete &amp; re-register to change.
                            </p>
                        )}
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Ref (branch / tag)
                            </label>
                            <input
                                type="text"
                                value={gitRef}
                                onChange={(e) => setGitRef(e.target.value)}
                                disabled={isEdit}
                                className="w-full px-2 py-1.5 text-sm border border-default rounded bg-input disabled:opacity-60"
                                placeholder="main"
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Subpath (optional)
                            </label>
                            <input
                                type="text"
                                value={subpath ?? ''}
                                onChange={(e) => setSubpath(e.target.value)}
                                className="w-full px-2 py-1.5 text-sm border border-default rounded bg-input"
                                placeholder="skills/"
                            />
                        </div>
                    </div>
                    <div>
                        <label className="block text-sm font-medium mb-1">
                            Sync interval (seconds)
                        </label>
                        <input
                            type="number"
                            value={intervalSecs}
                            min={300}
                            max={86400}
                            onChange={(e) =>
                                setIntervalSecs(Number(e.target.value) || 3600)
                            }
                            className="w-full px-2 py-1.5 text-sm border border-default rounded bg-input"
                        />
                        <p className="text-xs text-muted mt-0.5">
                            Min 300 (5 min) · Max 86400 (24 h) · Default 3600 (1 h)
                        </p>
                    </div>
                    <div className="space-y-2">
                        <label className="flex items-center gap-2 text-sm">
                            <input
                                type="checkbox"
                                checked={enabled}
                                onChange={(e) => setEnabled(e.target.checked)}
                            />
                            Enabled (scheduler will sync this source)
                        </label>
                        <label className="flex items-center gap-2 text-sm">
                            <input
                                type="checkbox"
                                checked={autoPublish}
                                onChange={(e) => setAutoPublish(e.target.checked)}
                            />
                            Auto-publish discovered skills (otherwise land as DRAFT)
                        </label>
                    </div>
                </div>

                <div className="flex justify-end gap-2 pt-2">
                    <button
                        onClick={onClose}
                        disabled={saving}
                        className="px-3 py-1.5 text-sm border border-default rounded hover:bg-surface-alt"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={asyncHandler(handleSave, 'click')}
                        disabled={saving || !name || !repoUrl}
                        className="px-3 py-1.5 text-sm font-medium bg-primary text-white rounded hover:bg-primary-600 disabled:opacity-50"
                    >
                        {saving ? 'Saving…' : isEdit ? 'Save' : 'Register'}
                    </button>
                </div>
            </div>
        </div>
    );
};
