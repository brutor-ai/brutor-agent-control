/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * <FacetMultiSelect /> — Datadog-style multi-select filter input.
 *
 * What it solves:
 *
 *   Audit-log UIs that ship free-text filter inputs are notorious for
 *   "I typed alice but got nothing back" — because the input matches
 *   the underlying ID column (a UUID), not the display name the
 *   operator just saw on a row. This component fixes that root cause
 *   by populating its dropdown from the SAME data the table is about
 *   to show: every value in the dropdown is a real, frequency-ranked
 *   value from `proxy_logs` scoped to the active filter envelope.
 *
 * Behaviour:
 *
 *   - Open → call `loadFacets(field)` → render the top-N values with
 *     row counts. No values for this scope → render a "no matching
 *     values" hint instead of an empty dropdown.
 *   - Operator types → debounced `loadSuggestions(field, prefix)` →
 *     replace the list with prefix matches.
 *   - Multi-select: checkbox per row, "Clear" button to wipe the
 *     selection without closing the dropdown.
 *   - Close: clicking outside, hitting Escape, or pressing the
 *     trigger button again. Selection is committed on close (i.e.
 *     the parent only re-fetches the table once per dropdown
 *     interaction, not once per checkbox tick).
 *
 * Why it's a shared component:
 *
 *   Both the Proxy Logs and Admin Audit Logs viewers use the same
 *   shape — the only differences are which API client method to call
 *   (proxy vs audit) and the field allow-list. Both are passed in
 *   as props so the component itself has zero coupling to either
 *   surface.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, Search, X } from 'lucide-react';
import { errorMessage } from '../../lib/narrow';

export interface FacetValue {
    value: string | number | boolean | null;
    count: number;
}

export interface FacetMultiSelectProps {
    /** Display label shown above the trigger. */
    label: string;
    /** Backend column name (e.g. "server_id"). Sent verbatim to /facets and /suggest. */
    field: string;
    /** Operator's current selection — strings on the wire even if the column is numeric/bool. */
    selected: string[];
    /** Called when the selection changes (after dropdown closes). */
    onChange: (next: string[]) => void;
    /** Optional placeholder shown when nothing is selected. */
    placeholder?: string;
    /**
     * Fetch the top-N values for THIS field. The implementation is
     * the API-client method (different for proxy logs vs audit
     * logs) — passed in so this component stays surface-agnostic.
     */
    loadFacets: (field: string) => Promise<FacetValue[]>;
    /** Same idea but for prefix typeahead. Receives the substring the operator typed. */
    loadSuggestions: (field: string, prefix: string) => Promise<FacetValue[]>;
    /**
     * Optional: a display map so internal IDs render as friendly
     * labels in the dropdown (e.g. server UUID → server name). Not
     * required — the raw value is still a real value the operator can
     * pick, where a free-text box asks for a UUID they don't have.
     */
    labels?: Record<string, string>;
}

/**
 * Stringify a wire value so the parent's `selected[]` is a uniform
 * `string[]`. Booleans land as 'true'/'false' to match the URL-state
 * encoding the rest of the filter panel uses.
 */
function valueToKey(v: FacetValue['value']): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    return String(v);
}

const FacetMultiSelect: React.FC<FacetMultiSelectProps> = ({
    label,
    field,
    selected,
    onChange,
    placeholder,
    loadFacets,
    loadSuggestions,
    labels,
}) => {
    const [open, setOpen] = useState(false);
    const [values, setValues] = useState<FacetValue[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [typed, setTyped] = useState('');
    // Working selection — operator can tick/untick freely without
    // triggering a parent refetch on every change. Committed on close.
    const [working, setWorking] = useState<Set<string>>(new Set(selected));

    const ref = useRef<HTMLDivElement>(null);
    const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Re-sync working set whenever the parent's `selected` changes
    // (URL state hydrate / chip × removal / saved view recall).
    useEffect(() => {
        setWorking(new Set(selected));
    }, [selected]);

    // Lazy-load facet values when the dropdown opens. Re-fetch on
    // every open so the counts reflect any filter changes the
    // operator made elsewhere in the meantime.
    const fetchInitial = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const rows = await loadFacets(field);
            setValues(rows);
        } catch (e) {
            setError(errorMessage(e, 'Failed to load values'));
        } finally {
            setLoading(false);
        }
    }, [field, loadFacets]);

    const fetchSuggest = useCallback(
        async (prefix: string) => {
            setLoading(true);
            setError(null);
            try {
                const rows = await loadSuggestions(field, prefix);
                setValues(rows);
            } catch (e) {
                setError(errorMessage(e, 'Failed to load suggestions'));
            } finally {
                setLoading(false);
            }
        },
        [field, loadSuggestions],
    );

    useEffect(() => {
        if (!open) return;
        if (!typed) {
            void fetchInitial();
            return;
        }
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
            void fetchSuggest(typed);
        }, 200);
        return () => {
            if (debounceRef.current) clearTimeout(debounceRef.current);
        };
    }, [open, typed, fetchInitial, fetchSuggest]);

    // Close on outside click / Escape.
    useEffect(() => {
        if (!open) return;
        const onClickOutside = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) {
                commitAndClose();
            }
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                commitAndClose();
            }
        };
        document.addEventListener('mousedown', onClickOutside);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('mousedown', onClickOutside);
            document.removeEventListener('keydown', onKey);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open, working]);

    const commitAndClose = () => {
        const next = Array.from(working);
        const prev = selected;
        // Skip the parent re-render if the selection didn't actually
        // change (purely opening + closing the dropdown shouldn't
        // refetch the table).
        const changed =
            next.length !== prev.length ||
            next.some((v) => !prev.includes(v));
        if (changed) onChange(next);
        setOpen(false);
        setTyped('');
    };

    const toggle = (key: string) => {
        setWorking((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
        });
    };

    const clear = () => {
        setWorking(new Set());
    };

    const display = useMemo(() => {
        if (selected.length === 0) return placeholder ?? `Any ${label.toLowerCase()}`;
        if (selected.length === 1) {
            return labels?.[selected[0]] ?? selected[0];
        }
        return `${labels?.[selected[0]] ?? selected[0]} +${selected.length - 1} more`;
    }, [selected, placeholder, label, labels]);

    return (
        <div className="relative" ref={ref}>
            <label className="block text-xs font-medium text-secondary mb-1">{label}</label>
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className={`w-full flex items-center justify-between px-2 py-1 text-xs border border-strong rounded bg-surface hover:bg-surface-alt transition-colors ${
                    selected.length > 0 ? 'text-default' : 'text-muted'
                }`}
                aria-expanded={open}
            >
                <span className="truncate">{display}</span>
                <ChevronDown className={`w-3.5 h-3.5 flex-shrink-0 ml-1 transition-transform ${open ? 'rotate-180' : ''}`} />
            </button>
            {open && (
                <div className="absolute z-50 mt-1 w-full min-w-[220px] max-h-[320px] overflow-hidden bg-surface border border-strong rounded-md shadow-lg flex flex-col">
                    {/* Search / typeahead input. Pre-populates with the
                        operator's last typed prefix when re-opening, so
                        switching between facets doesn't lose context. */}
                    <div className="flex items-center border-b border-default px-2 py-1.5">
                        <Search className="w-3 h-3 text-muted flex-shrink-0" />
                        <input
                            type="text"
                            autoFocus
                            value={typed}
                            onChange={(e) => setTyped(e.target.value)}
                            placeholder={`Search ${label.toLowerCase()}...`}
                            className="flex-1 ml-1.5 text-xs bg-transparent border-none focus:outline-none min-h-0 min-w-0"
                        />
                        {working.size > 0 && (
                            <button
                                type="button"
                                onClick={clear}
                                className="text-[11px] text-muted hover:text-default"
                                title="Clear selection"
                            >
                                Clear ({working.size})
                            </button>
                        )}
                    </div>
                    <div className="scroll-region">
                        {loading && (
                            <div className="px-2 py-3 text-xs text-muted italic">Loading values…</div>
                        )}
                        {!loading && error && (
                            <div className="px-2 py-3 text-xs text-red-600">{error}</div>
                        )}
                        {!loading && !error && values.length === 0 && (
                            <div className="px-2 py-3 text-xs text-muted italic">
                                {typed
                                    ? `No values match "${typed}".`
                                    : `No values in this scope. Try widening the time range.`}
                            </div>
                        )}
                        {!loading && !error && values.map((row) => {
                            const key = valueToKey(row.value);
                            const checked = working.has(key);
                            const display = labels?.[key] ?? key;
                            return (
                                <label
                                    key={key}
                                    className={`flex items-center justify-between px-2 py-1 text-xs cursor-pointer hover:bg-surface-alt ${
                                        checked ? 'bg-primary-50' : ''
                                    }`}
                                >
                                    <span className="flex items-center min-w-0">
                                        <span className={`flex items-center justify-center w-3.5 h-3.5 rounded border ${checked ? 'bg-primary-600 border-primary-600 text-white' : 'border-strong bg-surface'}`}>
                                            {checked && <Check className="w-3 h-3" />}
                                        </span>
                                        <span className="ml-2 truncate" title={display}>
                                            {display === '' ? <em className="text-muted">(empty)</em> : display}
                                        </span>
                                    </span>
                                    <span className="ml-2 text-[11px] text-muted flex-shrink-0">{row.count.toLocaleString()}</span>
                                    <input
                                        type="checkbox"
                                        checked={checked}
                                        onChange={() => toggle(key)}
                                        className="sr-only"
                                    />
                                </label>
                            );
                        })}
                    </div>
                    <div className="border-t border-default px-2 py-1.5 flex items-center justify-between">
                        <span className="text-[11px] text-muted">
                            {working.size > 0
                                ? `${working.size} selected`
                                : `Showing top ${values.length}`}
                        </span>
                        <button
                            type="button"
                            onClick={commitAndClose}
                            className="text-[11px] font-medium text-primary-700 hover:text-primary-800"
                        >
                            Apply
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

export default FacetMultiSelect;

/**
 * Active filter chips — a horizontal row of removable pills, one per
 * filter value. Empty filters render nothing; the row collapses
 * cleanly so the toolbar doesn't shift around.
 */
export interface ActiveFilterChipsProps {
    /** Active filters, keyed by field name. */
    filters: Record<string, string[]>;
    /** Human label per field — defaults to the raw field name. */
    fieldLabels?: Record<string, string>;
    /** Optional per-value display map. */
    valueLabels?: Record<string, string>;
    /** Called when the operator clicks × on a chip. */
    onRemove: (field: string, value: string) => void;
    /** Called when the operator clicks "Clear all". */
    onClearAll: () => void;
}

export const ActiveFilterChips: React.FC<ActiveFilterChipsProps> = ({
    filters,
    fieldLabels,
    valueLabels,
    onRemove,
    onClearAll,
}) => {
    const flat = useMemo(() => {
        const out: Array<{ field: string; value: string }> = [];
        for (const [field, values] of Object.entries(filters)) {
            for (const v of values) {
                if (v) out.push({ field, value: v });
            }
        }
        return out;
    }, [filters]);

    if (flat.length === 0) return null;

    return (
        <div className="flex items-center flex-wrap gap-1 px-3 py-2 border-b border-default bg-surface-alt">
            <span className="text-[11px] text-muted font-medium mr-1">Filters:</span>
            {flat.map(({ field, value }) => {
                const fLabel = fieldLabels?.[field] ?? field;
                const vLabel = valueLabels?.[value] ?? value;
                return (
                    <span
                        key={`${field}:${value}`}
                        className="inline-flex items-center px-2 py-0.5 text-[11px] font-medium rounded-full bg-primary-100 text-primary-700"
                    >
                        <span className="font-semibold mr-1">{fLabel}:</span>
                        <span className="truncate max-w-[200px]" title={vLabel}>{vLabel}</span>
                        <button
                            type="button"
                            onClick={() => onRemove(field, value)}
                            className="ml-1 inline-flex items-center justify-center w-3.5 h-3.5 rounded-full hover:bg-primary-200"
                            aria-label={`Remove ${fLabel} = ${vLabel}`}
                        >
                            <X className="w-2.5 h-2.5" />
                        </button>
                    </span>
                );
            })}
            <button
                type="button"
                onClick={onClearAll}
                className="ml-1 text-[11px] text-muted hover:text-default underline"
            >
                Clear all
            </button>
        </div>
    );
};
