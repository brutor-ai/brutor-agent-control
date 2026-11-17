/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The **Audit Info** tab — the same panel on every AI Resource form.
 *
 * It sits immediately after General / Basic Info on all nine resource
 * types, so "who made this, when, who last touched it, and what version is
 * it?" has the same answer in the same place everywhere. An operator who
 * learns it once on a model should not have to hunt for it on a routing
 * group.
 *
 * **Version is editable; authorship is not**, and the two are visually
 * distinct for that reason. A field the system owns should not look like
 * one you can type into — discovering the difference by clicking is a
 * small betrayal every time.
 *
 * The read-only guarantee is structural rather than cosmetic: `created_by`
 * and `updated_by` are absent from every request model on the API, so
 * there is nothing this component could submit even if it tried.
 */

import { Info } from 'lucide-react';

import ResourceMetaFields, { ResourceMeta } from './ResourceMetaFields';

export interface AuditInfoTabProps {
    meta: ResourceMeta;
    /** Omit to render read-only (a detail view rather than a form). */
    onVersionChange?: (version: string) => void;
    /**
     * Set for resources that have no version of their own. A server config
     * takes its version from the MCP server it configures; carrying a
     * second one here would invite the two to disagree, and the one on the
     * config would be the wrong answer.
     */
    versionHidden?: boolean;
    /** Names where the version comes from when this resource has none. */
    versionDerivedFrom?: string;
    disabled?: boolean;
    /** Shown when the resource does not exist yet. */
    isNew?: boolean;
    /**
     * Suppress the panel's own heading, for forms whose section chrome
     * already prints the tab name above the content. Two identical
     * headings stacked on each other read as a rendering bug.
     */
    headingHidden?: boolean;
}

export function AuditInfoTab({
    meta,
    onVersionChange,
    versionHidden = false,
    versionDerivedFrom,
    disabled = false,
    isNew = false,
    headingHidden = false,
}: AuditInfoTabProps) {
    return (
        <div className="space-y-4" data-testid="audit-info-tab">
            {!headingHidden && (
                <div>
                    <h3 className="text-base font-semibold text-default">Audit Info</h3>
                    <p className="text-sm text-muted mt-1">
                        {versionHidden
                            ? 'Who created and last changed this resource.'
                            : 'Version, and who created and last changed this resource.'}
                    </p>
                </div>
            )}

            {isNew && (
                <div
                    className="flex items-start gap-2 p-3 border border-default rounded-lg bg-surface-subtle"
                    data-testid="audit-info-new"
                >
                    <Info className="w-4 h-4 text-muted shrink-0 mt-0.5" />
                    <p className="text-sm text-muted">
                        Authorship is recorded when you save. It is set from your
                        signed-in account and cannot be edited afterwards.
                    </p>
                </div>
            )}

            <ResourceMetaFields
                meta={meta}
                onVersionChange={onVersionChange}
                versionHidden={versionHidden}
                versionDerivedFrom={versionDerivedFrom}
                disabled={disabled}
            />
        </div>
    );
}

export default AuditInfoTab;
