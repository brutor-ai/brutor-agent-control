/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The AI Resource identity block — version + authorship.
 *
 * Rendered on every AI Resource's General tab, next to id, name and
 * description, so the same four questions have the same answer in the
 * same place on every resource: *who made this, when, who last touched
 * it, and what version is it?*
 *
 * **Version is editable. Authorship is not.** The two are visually
 * distinct for that reason — a field the system owns should not look like
 * one the operator can type into, because discovering the difference by
 * clicking is a small betrayal every time.
 *
 * Authorship is stamped server-side from the authenticated admin and is
 * absent from every request model, so there is nothing this component
 * could send even if it tried.
 */

export interface ResourceMeta {
    /** Operator-authored label. Absent on resources whose version is
     *  derived from something else (a server config takes its MCP
     *  server's version). */
    version?: string | null;
    created_by?: string | null;
    created_at?: string | null;
    updated_by?: string | null;
    updated_at?: string | null;
}

interface ResourceMetaFieldsProps {
    meta: ResourceMeta;
    /** Omit to render read-only (a detail view rather than a form). */
    onVersionChange?: (version: string) => void;
    /** Set when the resource has no version of its own. */
    versionHidden?: boolean;
    /** Explains where the version comes from when it is not editable. */
    versionDerivedFrom?: string;
    disabled?: boolean;
}

function formatWhen(iso?: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

function formatWho(who?: string | null): string {
    // A null means no author is recorded for the row, and saying so is
    // honest. Rows the platform creates itself are stamped `system` at
    // write time, so filling a null in with "system" here would guess —
    // and guess wrong on exactly the rows where the answer matters.
    return who || 'unknown';
}

export function ResourceMetaFields({
    meta,
    onVersionChange,
    versionHidden = false,
    versionDerivedFrom,
    disabled = false,
}: ResourceMetaFieldsProps) {
    const editable = Boolean(onVersionChange) && !disabled;

    return (
        <div className="space-y-4" data-testid="resource-meta-fields">
            {!versionHidden && (
                <div>
                    <label
                        htmlFor="resource-version"
                        className="block text-sm font-medium text-secondary mb-1"
                    >
                        Version
                    </label>
                    {editable ? (
                        <input
                            id="resource-version"
                            type="text"
                            maxLength={50}
                            value={meta.version ?? ''}
                            onChange={(e) => onVersionChange?.(e.target.value)}
                            placeholder="1.0"
                            className="w-full px-3 py-2 border border-strong rounded-lg bg-surface text-sm focus:ring-2 focus:ring-primary-500"
                            data-testid="resource-version-input"
                        />
                    ) : (
                        <div className="text-sm text-default" data-testid="resource-version-readonly">
                            {meta.version || '—'}
                        </div>
                    )}
                    <p className="text-xs text-muted mt-1">
                        {versionDerivedFrom
                            ? `Determined by the ${versionDerivedFrom}.`
                            : 'Your label for this resource. Change history is in the audit log.'}
                    </p>
                </div>
            )}

            {versionHidden && versionDerivedFrom && (
                <p className="text-xs text-muted" data-testid="resource-version-derived">
                    Version is determined by the {versionDerivedFrom}, not set here.
                </p>
            )}

            {/* Authorship. Read-only by construction — the API does not
                accept these fields on any request. */}
            <div
                className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-3 border-t border-default"
                data-testid="resource-authorship"
            >
                <MetaFact label="Created by" value={formatWho(meta.created_by)} />
                <MetaFact label="Created" value={formatWhen(meta.created_at)} />
                <MetaFact label="Last modified by" value={formatWho(meta.updated_by)} />
                <MetaFact label="Last modified" value={formatWhen(meta.updated_at)} />
            </div>
        </div>
    );
}

function MetaFact({ label, value }: { label: string; value: string }) {
    return (
        <div>
            <div className="text-xs text-muted uppercase tracking-wider">{label}</div>
            {/* Not an input: these are system-set, and making them look
                editable would invite an operator to try. */}
            <div className="text-sm text-default mt-0.5">{value}</div>
        </div>
    );
}

export default ResourceMetaFields;
