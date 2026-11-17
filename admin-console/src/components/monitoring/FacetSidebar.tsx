/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * <FacetSidebar /> — Datadog-style left rail of facets.
 *
 * Each section is a collapsible field. Open one and you see the
 * top-N values + counts; click a value to add it to that field's
 * filter (or remove it if already selected). Collapse state
 * persists per-page in localStorage so the operator's preferred
 * layout survives refreshes.
 *
 * Companions in this directory:
 *   - <FacetMultiSelect />  for inline filter inputs in the toolbar
 *   - <ActiveFilterChips /> shown above the table
 *   - <TimeRangePicker />   feeds the scope every facet observes
 *
 * The sidebar and the multi-select share the same `loadFacets`
 * callback the parent already wired up — no duplicate data path.
 * That's the only "Datadog had this bug for years" trap to avoid:
 * sidebar values and dropdown values must come from the SAME
 * source so they can never disagree.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, X } from 'lucide-react';
import type { FacetValue } from './FacetMultiSelect';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

export interface FacetField {
    /** Backend column name. */
    field: string;
    /** Human label shown in the section header. */
    label: string;
}

export interface FacetSidebarProps {
    /** Field list. Order is the render order. */
    fields: FacetField[];
    /** Current selection per field. */
    selected: Record<string, string[]>;
    /** Patch a single field's selection. The parent re-fetches the table. */
    onChange: (field: string, next: string[]) => void;
    /** Same loader the FacetMultiSelect uses — top-N values + counts. */
    loadFacets: (field: string) => Promise<FacetValue[]>;
    /**
     * Storage key for the per-section collapse state. Different
     * pages should pass different keys ('proxy_logs', 'audit_logs')
     * so the two viewers' preferences don't overwrite each other.
     */
    storageKey: string;
    /** Optional per-value display map (e.g. server UUID → server name). */
    labels?: Record<string, string>;
    /**
     * Re-fetch trigger. The parent passes a value (e.g. the filter
     * envelope hash) that bumps whenever ANY filter changes. Each
     * EXPANDED section then re-loads, so a value the operator just
     * clicked elsewhere is reflected in the sidebar counts.
     */
    refetchKey: string | number;
    /**
     * Optional explicit width in px. When provided, the parent owns the
     * width (e.g. a drag-to-resize splitter) and the default `w-60` is
     * dropped. Omit to keep the fixed 240px rail.
     */
    width?: number;
}

function valueToKey(v: FacetValue['value']): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return String(v);
}

/**
 * Read the collapse map from localStorage. Missing key → empty map
 * → all sections start COLLAPSED on first visit; we don't pre-open
 * everything because the network cost of N parallel facet fetches
 * on first paint is meaningful on a slow tenant.
 */
function readCollapseMap(key: string): Record<string, boolean> {
    try {
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : {};
    } catch {
        return {};
    }
}

function writeCollapseMap(key: string, map: Record<string, boolean>): void {
    try {
        localStorage.setItem(key, JSON.stringify(map));
    } catch {
        /* localStorage disabled / quota — silent fail is fine */
    }
}

const FacetSidebar: React.FC<FacetSidebarProps> = ({
    fields,
    selected,
    onChange,
    loadFacets,
    storageKey,
    labels,
    refetchKey,
    width,
}) => {
    // open[field] = true means "open" (the storage helpers are named
    // for collapse; the stored flag means open). Default = all closed.
    const [open, setOpen] = useState<Record<string, boolean>>(() => readCollapseMap(storageKey));
    const [values, setValues] = useState<Record<string, FacetValue[]>>({});
    const [loading, setLoading] = useState<Record<string, boolean>>({});
    // The values of a field, by the name of the field.
    const [readFailure, reads] = useReadFailures();

    // Persist on every change.
    useEffect(() => {
        writeCollapseMap(storageKey, open);
    }, [open, storageKey]);

    // The number of the request made last for each field. An answer is
    // shown only when it is the answer to that request, so the section shows
    // the values or the failure of what was asked for last.
    const latestRequest = useRef<Record<string, number>>({});

    /**
     * Ask for the values of one field: when its section opens, when a filter
     * moves while it is open, and from the Retry of its notice. Values take
     * the failure notice away; a failure takes the values away.
     */
    const loadField = async (field: string) => {
        const request = (latestRequest.current[field] ?? 0) + 1;
        latestRequest.current[field] = request;
        const isLatest = () => latestRequest.current[field] === request;
        setLoading(s => ({ ...s, [field]: true }));
        try {
            const rows = await loadFacets(field);
            if (!isLatest()) return;
            setValues(s => ({ ...s, [field]: rows }));
            reads.loaded(field);
        } catch (err) {
            if (!isLatest()) return;
            setValues(s => ({ ...s, [field]: [] }));
            reads.lost(field, 'Failed to load the values', err);
        } finally {
            if (isLatest()) setLoading(s => ({ ...s, [field]: false }));
        }
    };

    /**
     * Ask again for every open section when the sidebar appears and whenever
     * the refetchKey changes (a sibling filter moved). Closed sections are
     * skipped: their values are asked for when they open.
     */
    useEffect(() => {
        for (const f of fields) {
            if (open[f.field]) runBackground(loadField(f.field), 'facet values');
        }
        // Not on `open`: a section that opens asks for its own values in `toggle`.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [refetchKey]);

    const toggle = async (field: string) => {
        const isOpen = !!open[field];
        setOpen(s => ({ ...s, [field]: !isOpen }));
        if (!isOpen) await loadField(field);
    };

    const toggleValue = (field: string, key: string) => {
        const current = selected[field] ?? [];
        const next = current.includes(key)
            ? current.filter(v => v !== key)
            : [...current, key];
        onChange(field, next);
    };

    // Total active filter count — shown in the header so the
    // operator sees at a glance how scoped the view is.
    const activeCount = useMemo(
        () => Object.values(selected).reduce((sum, arr) => sum + (arr?.length ?? 0), 0),
        [selected],
    );

    return (
        <aside
            className={`${width == null ? 'w-60' : ''} border-r border-default bg-surface-alt overflow-y-auto flex-shrink-0`}
            style={width == null ? undefined : { width }}
        >
            <div className="px-3 py-2 border-b border-default flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wide text-secondary">
                    Facets
                </span>
                <span className="text-[11px] text-muted">
                    {activeCount > 0 ? `${activeCount} active` : 'none active'}
                </span>
            </div>
            <ul>
                {fields.map(f => {
                    const sel = selected[f.field] ?? [];
                    const rows = values[f.field] ?? [];
                    const isOpen = !!open[f.field];
                    const isLoading = !!loading[f.field];
                    return (
                        <li key={f.field} className="border-b border-default last:border-b-0">
                            <button
                                type="button"
                                onClick={asyncHandler(() => toggle(f.field), 'click')}
                                className="w-full flex items-center justify-between px-3 py-2 hover:bg-surface text-xs font-medium text-default"
                                aria-expanded={isOpen}
                            >
                                <span className="flex items-center min-w-0">
                                    {isOpen ? (
                                        <ChevronDown className="w-3 h-3 mr-1 flex-shrink-0" />
                                    ) : (
                                        <ChevronRight className="w-3 h-3 mr-1 flex-shrink-0" />
                                    )}
                                    <span className="truncate">{f.label}</span>
                                </span>
                                {sel.length > 0 && (
                                    <span className="ml-2 inline-flex items-center px-1.5 rounded-full bg-primary-100 text-primary-700 text-[10px] font-semibold">
                                        {sel.length}
                                    </span>
                                )}
                            </button>
                            {isOpen && (
                                <div className="px-3 pb-2 space-y-0.5">
                                    {isLoading && (
                                        <div className="text-[11px] text-muted italic py-1">Loading…</div>
                                    )}
                                    {!isLoading && (
                                        <ReadFailureNotice
                                            compact
                                            failure={readFailure(f.field)}
                                            onRetry={asyncHandler(() => loadField(f.field), 'facet values', 'background')}
                                        />
                                    )}
                                    {!isLoading && rows.length === 0 && !readFailure(f.field) && (
                                        <div className="text-[11px] text-muted italic py-1">No values in this scope.</div>
                                    )}
                                    {!isLoading && rows.map(row => {
                                        const k = valueToKey(row.value);
                                        const isSelected = sel.includes(k);
                                        const display = labels?.[k] ?? k;
                                        return (
                                            <button
                                                key={k || '(empty)'}
                                                type="button"
                                                onClick={() => toggleValue(f.field, k)}
                                                className={`w-full flex items-center justify-between px-1.5 py-0.5 rounded text-[11px] hover:bg-surface ${
                                                    isSelected ? 'bg-primary-50 text-primary-700' : 'text-default'
                                                }`}
                                                title={display}
                                            >
                                                <span className="flex items-center min-w-0">
                                                    {isSelected && <X className="w-2.5 h-2.5 mr-1 flex-shrink-0" />}
                                                    <span className="truncate">
                                                        {display === '' ? <em className="text-muted">(empty)</em> : display}
                                                    </span>
                                                </span>
                                                <span className="ml-2 text-[10px] text-muted flex-shrink-0">
                                                    {row.count.toLocaleString()}
                                                </span>
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </li>
                    );
                })}
            </ul>
        </aside>
    );
};

export default FacetSidebar;
