/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Audit Info tab for a resource group.
 *
 * A thin wrapper around the shared panel, because a group's tabs each own
 * their own save rather than lifting form state to the parent — `onUpdate`
 * here is a refresh signal, not a setter.
 *
 * Version is saved explicitly rather than on every keystroke: it is a
 * PATCH against a governed resource, and firing one per character would
 * write a row to the audit log for each letter typed.
 */

import { useEffect, useState } from 'react';
import { Save } from 'lucide-react';
import toast from 'react-hot-toast';

import AuditInfoTab from '../../common/AuditInfoTab';
import { useGateway } from '../../../hooks/useGateway';
import { ResourceGroup } from '../../../lib/groupTypes';

interface GroupAuditTabProps {
    group: ResourceGroup;
    onUpdate: () => void;
    /** Rendered inside the General tab's sub-tab area (which already
     *  pads) rather than as a top-level tab. */
    embedded?: boolean;
}

export function GroupAuditTab({ group, onUpdate, embedded = false }: GroupAuditTabProps) {
    const { updateResourceGroup } = useGateway();
    const [version, setVersion] = useState(group.version ?? '1.0');
    const [saving, setSaving] = useState(false);

    // Re-sync when the panel switches to another group, or the field would
    // keep showing the previous one's version.
    useEffect(() => {
        setVersion(group.version ?? '1.0');
    }, [group.id, group.version]);

    const dirty = version !== (group.version ?? '1.0');

    const save = async () => {
        try {
            setSaving(true);
            await updateResourceGroup(group.id, { version });
            toast.success('Version updated');
            onUpdate();
        } catch (err: unknown) {
            toast.error(err instanceof Error ? err.message : 'Could not save the version');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className={embedded ? 'space-y-4' : 'p-6 space-y-4'} data-testid="group-audit-tab">
            <AuditInfoTab
                meta={{
                    version,
                    created_by: group.created_by,
                    created_at: group.created_at,
                    updated_by: group.updated_by,
                    updated_at: group.updated_at,
                }}
                onVersionChange={setVersion}
                disabled={saving}
            />

            <button
                type="button"
                disabled={!dirty || saving}
                onClick={() => void save()}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
                data-testid="group-audit-save"
            >
                <Save className="w-4 h-4" />
                {saving ? 'Saving…' : 'Save version'}
            </button>
        </div>
    );
}

export default GroupAuditTab;
