/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * <SavedViewMenu /> — Datadog-style "Saved views" dropdown for the
 * proxy log + admin audit log search UIs.
 *
 * Behaviour:
 *   * Button shows the current view name (or "Saved views" idle).
 *   * Dropdown groups views into "Mine" (owner == current admin)
 *     and "Shared" (is_shared, anyone in tenant).
 *   * Click a view → calls `onApply(view.filters)` so the parent
 *     can splat the envelope into its local state, then closes.
 *   * "Save current as new view..." → opens the inline create
 *     dialog; the parent provides `currentFilters` (a memoised
 *     snapshot of the live envelope).
 *   * Each view has a kebab menu: Rename, Toggle shared, Delete.
 *     Mutations are owner-only on the server, the UI hides those
 *     options for views the operator doesn't own.
 *
 * The component owns the dropdown UI + the dialogs; the parent owns
 * the filter state and re-fetches the table after `onApply`.
 *
 * Why no inline "apply this view from URL on mount": deep-linking
 * to a saved view would require a viewId in the URL, which collides
 * with `useUrlFilterState`. If we ever want it, the right model is
 * a separate `view=slv-…` query param the parent reads on mount,
 * loads the view, and applies. That's a future increment.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bookmark, ChevronDown, Globe, Lock, MoreVertical, Plus, Trash2, Edit2, Share2, X } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import type { SavedLogView, SavedLogViewScope } from '../../lib/types';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

export interface SavedViewMenuProps {
    /** Which viewer this menu belongs to. */
    scope: SavedLogViewScope;
    /**
     * The filter envelope the parent would POST to /search RIGHT NOW.
     * Captured at click time when the operator saves a new view. Pass
     * a memoised snapshot so we don't capture a stale render.
     */
    currentFilters: Record<string, any>;
    /**
     * Hand back a stored envelope. The parent is expected to splat it
     * into its local filter state (multi-select arrays, time range,
     * `q`, etc.) and re-fetch the table.
     */
    onApply: (filters: Record<string, any>) => void;
    /** Current admin's ID — used to split Mine vs Shared. */
    currentAdminId: string | null;
}

const SavedViewMenu: React.FC<SavedViewMenuProps> = ({
    scope,
    currentFilters,
    onApply,
    currentAdminId,
}) => {
    const { gatewayClient } = useGateway();
    const [open, setOpen] = useState(false);
    const [views, setViews] = useState<SavedLogView[]>([]);
    const [loading, setLoading] = useState(false);
    const [readFailure, reads] = useReadFailures<'views'>();
    const [activeViewId, setActiveViewId] = useState<string | null>(null);
    const [saveDialog, setSaveDialog] = useState<{ name: string; shared: boolean } | null>(null);
    const [renameDialog, setRenameDialog] = useState<{ view: SavedLogView; name: string } | null>(null);
    const [kebabOpenFor, setKebabOpenFor] = useState<string | null>(null);
    const wrapperRef = useRef<HTMLDivElement | null>(null);

    // Close on outside-click. Standard dropdown UX — Datadog's saved-view
    // menu does the same; the operator expects clicking outside to close.
    useEffect(() => {
        if (!open && !kebabOpenFor) return;
        const handler = (e: MouseEvent) => {
            if (!wrapperRef.current) return;
            if (!wrapperRef.current.contains(e.target as Node)) {
                setOpen(false);
                setKebabOpenFor(null);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open, kebabOpenFor]);

    const reload = useCallback(async () => {
        if (!gatewayClient) return;
        setLoading(true);
        try {
            const res = await gatewayClient.listSavedLogViews(scope);
            setViews(res.items || []);
            reads.loaded('views');
        } catch (err) {
            reads.failed('views', 'Failed to load the saved views', err);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, scope, reads]);

    // Initial load + reload whenever the scope changes (e.g. someone
    // ever embeds this in a viewer that swaps scopes — currently only
    // one scope per mount but cheap to support).
    useEffect(() => {
        runBackground(reload(), 'reload');
    }, [reload]);

    const { mine, shared } = useMemo(() => {
        const m: SavedLogView[] = [];
        const s: SavedLogView[] = [];
        for (const v of views) {
            if (currentAdminId && v.owner_admin_id === currentAdminId) {
                m.push(v);
            } else if (v.is_shared) {
                s.push(v);
            }
        }
        return { mine: m, shared: s };
    }, [views, currentAdminId]);

    const handleApply = useCallback((v: SavedLogView) => {
        setActiveViewId(v.id);
        onApply(v.filters);
        setOpen(false);
    }, [onApply]);

    const handleCreate = useCallback(async () => {
        if (!gatewayClient || !saveDialog) return;
        const name = saveDialog.name.trim();
        if (!name) return;
        try {
            const created = await gatewayClient.createSavedLogView({
                scope,
                name,
                filters: currentFilters,
                is_shared: saveDialog.shared,
            });
            setSaveDialog(null);
            setActiveViewId(created.id);
            await reload();
        } catch (err) {
            // Pass through the server error message — name collisions
            // come back as 409 with a clear detail string.
            window.alert(errorMessage(err, 'Failed to save view'));
        }
    }, [gatewayClient, saveDialog, scope, currentFilters, reload]);

    const handleRename = useCallback(async () => {
        if (!gatewayClient || !renameDialog) return;
        const name = renameDialog.name.trim();
        if (!name || name === renameDialog.view.name) {
            setRenameDialog(null);
            return;
        }
        try {
            await gatewayClient.updateSavedLogView(renameDialog.view.id, { name });
            setRenameDialog(null);
            await reload();
        } catch (err) {
            window.alert(errorMessage(err, 'Failed to rename view'));
        }
    }, [gatewayClient, renameDialog, reload]);

    const handleToggleShared = useCallback(async (v: SavedLogView) => {
        if (!gatewayClient) return;
        try {
            await gatewayClient.updateSavedLogView(v.id, { is_shared: !v.is_shared });
            setKebabOpenFor(null);
            await reload();
        } catch (err) {
            window.alert(errorMessage(err, 'Failed to update sharing'));
        }
    }, [gatewayClient, reload]);

    const handleDelete = useCallback(async (v: SavedLogView) => {
        if (!gatewayClient) return;
        if (!window.confirm(`Delete saved view "${v.name}"?`)) return;
        try {
            await gatewayClient.deleteSavedLogView(v.id);
            setKebabOpenFor(null);
            if (activeViewId === v.id) setActiveViewId(null);
            await reload();
        } catch (err) {
            window.alert(errorMessage(err, 'Failed to delete view'));
        }
    }, [gatewayClient, activeViewId, reload]);

    const activeView = useMemo(
        () => views.find(v => v.id === activeViewId) ?? null,
        [views, activeViewId],
    );

    const isOwner = (v: SavedLogView) =>
        currentAdminId !== null && v.owner_admin_id === currentAdminId;

    const renderItem = (v: SavedLogView) => (
        <li key={v.id} className="group flex items-center justify-between hover:bg-surface-alt">
            <button
                type="button"
                onClick={() => handleApply(v)}
                className={`flex-1 text-left px-3 py-1.5 text-xs flex items-center min-w-0 min-h-0 ${
                    activeViewId === v.id ? 'text-primary font-medium' : 'text-default'
                }`}
                title={v.description ?? v.name}
            >
                {v.is_shared
                    ? <Globe className="w-3 h-3 mr-1.5 flex-shrink-0 text-muted" />
                    : <Lock className="w-3 h-3 mr-1.5 flex-shrink-0 text-muted" />}
                <span className="truncate">{v.name}</span>
            </button>
            <div className="relative">
                <button
                    type="button"
                    onClick={() => setKebabOpenFor(kebabOpenFor === v.id ? null : v.id)}
                    className="p-1 text-muted hover:text-default opacity-0 group-hover:opacity-100"
                    aria-label="View actions"
                >
                    <MoreVertical className="w-3 h-3" />
                </button>
                {kebabOpenFor === v.id && (
                    <div className="absolute right-0 top-full mt-1 z-30 bg-surface border border-default rounded shadow-lg w-44 py-1">
                        {isOwner(v) && (
                            <>
                                <button
                                    type="button"
                                    onClick={() => { setRenameDialog({ view: v, name: v.name }); setKebabOpenFor(null); }}
                                    className="w-full flex items-center px-3 py-1.5 text-xs text-default hover:bg-surface-alt"
                                >
                                    <Edit2 className="w-3 h-3 mr-2" /> Rename
                                </button>
                                <button
                                    type="button"
                                    onClick={asyncHandler(() => handleToggleShared(v), 'click')}
                                    className="w-full flex items-center px-3 py-1.5 text-xs text-default hover:bg-surface-alt"
                                >
                                    <Share2 className="w-3 h-3 mr-2" />
                                    {v.is_shared ? 'Unshare' : 'Share with tenant'}
                                </button>
                                <button
                                    type="button"
                                    onClick={asyncHandler(() => handleDelete(v), 'click')}
                                    className="w-full flex items-center px-3 py-1.5 text-xs text-red-600 hover:bg-red-50"
                                >
                                    <Trash2 className="w-3 h-3 mr-2" /> Delete
                                </button>
                            </>
                        )}
                        {!isOwner(v) && (
                            <div className="px-3 py-1.5 text-[11px] text-muted italic">
                                Read-only (owned by another admin)
                            </div>
                        )}
                    </div>
                )}
            </div>
        </li>
    );

    return (
        <div ref={wrapperRef} className="relative">
            <button
                type="button"
                onClick={() => setOpen(o => !o)}
                className="flex items-center gap-1.5 px-2.5 py-1 text-xs border border-strong rounded bg-surface hover:bg-surface-alt"
                title="Saved views"
            >
                <Bookmark className="w-3.5 h-3.5" />
                <span className="truncate max-w-[160px]">
                    {activeView ? activeView.name : 'Saved views'}
                </span>
                <ChevronDown className="w-3 h-3" />
            </button>

            {open && (
                <div className="absolute left-0 top-full mt-1 z-20 bg-surface border border-default rounded shadow-lg w-72 py-1 max-h-96 overflow-y-auto">
                    <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-muted font-semibold border-b border-default">
                        Mine
                    </div>
                    {loading && (
                        <div className="px-3 py-2 text-xs text-muted italic">Loading…</div>
                    )}
                    {!loading && <ReadFailureNotice compact className="px-3 py-2" failure={readFailure('views')} onRetry={() => runBackground(reload(), 'reload')} />}
                    {!loading && mine.length === 0 && !readFailure('views') && (
                        <div className="px-3 py-2 text-xs text-muted italic">No saved views yet.</div>
                    )}
                    <ul>{mine.map(renderItem)}</ul>

                    <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-muted font-semibold border-y border-default mt-1">
                        Shared
                    </div>
                    {!loading && shared.length === 0 && !readFailure('views') && (
                        <div className="px-3 py-2 text-xs text-muted italic">None shared in tenant.</div>
                    )}
                    <ul>{shared.map(renderItem)}</ul>

                    <div className="border-t border-default mt-1">
                        <button
                            type="button"
                            onClick={() => { setOpen(false); setSaveDialog({ name: '', shared: false }); }}
                            className="w-full flex items-center px-3 py-2 text-xs text-primary hover:bg-surface-alt"
                        >
                            <Plus className="w-3 h-3 mr-2" />
                            Save current as new view…
                        </button>
                        {activeView && isOwner(activeView) && (
                            <button
                                type="button"
                                onClick={asyncHandler(async () => {
                                    if (!gatewayClient) return;
                                    try {
                                        await gatewayClient.updateSavedLogView(activeView.id, {
                                            filters: currentFilters,
                                        });
                                        setOpen(false);
                                        await reload();
                                    } catch (err) {
                                        window.alert(errorMessage(err, 'Failed to update view'));
                                    }
                                }, 'click')}
                                className="w-full flex items-center px-3 py-2 text-xs text-default hover:bg-surface-alt border-t border-default"
                            >
                                <Edit2 className="w-3 h-3 mr-2" />
                                Update "{activeView.name}" with current filters
                            </button>
                        )}
                    </div>
                </div>
            )}

            {/* Save dialog. Inline rather than a portal — the menu
                already owns its container so the dialog tracks with
                the button rather than floating at the document root. */}
            {saveDialog && (
                <div className="absolute left-0 top-full mt-1 z-30 bg-surface border border-default rounded shadow-lg w-72 p-3">
                    <div className="flex items-center justify-between mb-2">
                        <span className="text-xs font-semibold">Save current filters as view</span>
                        <button onClick={() => setSaveDialog(null)} className="text-muted hover:text-default">
                            <X className="w-3.5 h-3.5" />
                        </button>
                    </div>
                    <input
                        type="text"
                        autoFocus
                        value={saveDialog.name}
                        onChange={(e) => setSaveDialog({ ...saveDialog, name: e.target.value })}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') runBackground(handleCreate(), 'create');
                            if (e.key === 'Escape') setSaveDialog(null);
                        }}
                        placeholder="View name"
                        className="w-full px-2 py-1 text-xs border border-strong rounded mb-2"
                    />
                    <label className="flex items-center gap-1.5 text-xs text-default mb-2">
                        <input
                            type="checkbox"
                            checked={saveDialog.shared}
                            onChange={(e) => setSaveDialog({ ...saveDialog, shared: e.target.checked })}
                        />
                        Share with everyone on this tenant
                    </label>
                    <div className="flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setSaveDialog(null)}
                            className="px-2 py-1 text-xs text-secondary hover:text-default"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={asyncHandler(handleCreate, 'click')}
                            disabled={!saveDialog.name.trim()}
                            className="px-3 py-1 text-xs rounded bg-primary text-white disabled:opacity-50"
                        >
                            Save
                        </button>
                    </div>
                </div>
            )}

            {renameDialog && (
                <div className="absolute left-0 top-full mt-1 z-30 bg-surface border border-default rounded shadow-lg w-72 p-3">
                    <div className="flex items-center justify-between mb-2">
                        <span className="text-xs font-semibold">Rename view</span>
                        <button onClick={() => setRenameDialog(null)} className="text-muted hover:text-default">
                            <X className="w-3.5 h-3.5" />
                        </button>
                    </div>
                    <input
                        type="text"
                        autoFocus
                        value={renameDialog.name}
                        onChange={(e) => setRenameDialog({ ...renameDialog, name: e.target.value })}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') runBackground(handleRename(), 'rename');
                            if (e.key === 'Escape') setRenameDialog(null);
                        }}
                        className="w-full px-2 py-1 text-xs border border-strong rounded mb-2"
                    />
                    <div className="flex justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setRenameDialog(null)}
                            className="px-2 py-1 text-xs text-secondary hover:text-default"
                        >
                            Cancel
                        </button>
                        <button
                            type="button"
                            onClick={asyncHandler(handleRename, 'click')}
                            disabled={!renameDialog.name.trim()}
                            className="px-3 py-1 text-xs rounded bg-primary text-white disabled:opacity-50"
                        >
                            Rename
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

export default SavedViewMenu;
