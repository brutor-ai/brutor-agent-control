/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Open or edit an incident. The frameworks in scope
 * decide which deadline rules apply — a rule applies when the
 * classification names it — so the form lists the enabled frameworks and
 * the classifications the catalogs know as suggestions, while leaving the
 * classification free text. Deadlines are recomputed on every change and
 * shown, never enforced.
 */

import { useState } from 'react';
import { Loader2, Save } from 'lucide-react';

import { INCIDENT_CLASSIFICATION_SUGGESTIONS, INCIDENT_SEVERITIES, type ComplianceFramework, type ComplianceIncident } from '../../lib/complianceEvidenceTypes';
import { BUTTON_PRIMARY, BUTTON_SECONDARY, INPUT_CLASS } from './complianceFormat';
import { incidentValuesFrom, validateIncident, type IncidentFormValues } from './complianceForms';

interface Props {
    initial: ComplianceIncident | null;
    frameworks: ComplianceFramework[];
    systems: { id: string; name: string }[];
    saving: boolean;
    error: string;
    onSubmit: (values: IncidentFormValues) => void;
    onCancel: () => void;
}

export default function IncidentForm({ initial, frameworks, systems, saving, error, onSubmit, onCancel }: Props) {
    const [v, setV] = useState<IncidentFormValues>(() => incidentValuesFrom(initial));
    const [localError, setLocalError] = useState('');
    const update = (patch: Partial<IncidentFormValues>) => setV((p) => ({ ...p, ...patch }));
    const enabledFrameworks = frameworks.filter((f) => f.enabled || v.frameworks.includes(f.id));

    const submit = (e: React.FormEvent) => {
        e.preventDefault();
        const problem = validateIncident(v);
        setLocalError(problem ?? '');
        if (problem) return;
        onSubmit(v);
    };

    return (
        <form onSubmit={submit} className="space-y-3 text-xs" data-testid="incident-form">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <label className="block md:col-span-2">
                    <span className="text-muted uppercase tracking-wide">Title</span>
                    <input type="text" value={v.title} onChange={(e) => update({ title: e.target.value })} className={`${INPUT_CLASS} block w-full mt-0.5`} data-testid="incident-title" />
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Classification</span>
                    <input
                        type="text"
                        list="incident-classifications"
                        value={v.classification}
                        onChange={(e) => update({ classification: e.target.value })}
                        className={`${INPUT_CLASS} block w-full mt-0.5 font-mono`}
                        placeholder="serious_incident"
                        data-testid="incident-classification"
                    />
                    <datalist id="incident-classifications">
                        {INCIDENT_CLASSIFICATION_SUGGESTIONS.map((c) => (
                            <option key={c} value={c} />
                        ))}
                    </datalist>
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Severity</span>
                    <select value={v.severity} onChange={(e) => update({ severity: e.target.value })} className={`${INPUT_CLASS} block w-full mt-0.5`} data-testid="incident-severity">
                        {INCIDENT_SEVERITIES.map((s) => (
                            <option key={s} value={s}>
                                {s}
                            </option>
                        ))}
                    </select>
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Became aware at</span>
                    <input
                        type="datetime-local"
                        value={v.becameAwareAt}
                        onChange={(e) => update({ becameAwareAt: e.target.value })}
                        className={`${INPUT_CLASS} block w-full mt-0.5`}
                        data-testid="incident-became-aware"
                    />
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Classified as major/significant at (optional)</span>
                    <input
                        type="datetime-local"
                        value={v.classifiedAt}
                        onChange={(e) => update({ classifiedAt: e.target.value })}
                        className={`${INPUT_CLASS} block w-full mt-0.5`}
                        data-testid="incident-classified-at"
                        title="DORA: the initial notification is due 4 h after classification as major, and no later than 24 h after awareness."
                    />
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">AI System</span>
                    <select value={v.aiSystemId} onChange={(e) => update({ aiSystemId: e.target.value })} className={`${INPUT_CLASS} block w-full mt-0.5`} data-testid="incident-system">
                        <option value="">none / estate-wide</option>
                        {systems.map((s) => (
                            <option key={s.id} value={s.id}>
                                {s.name}
                            </option>
                        ))}
                    </select>
                </label>
                <fieldset className="md:col-span-2">
                    <legend className="text-muted uppercase tracking-wide">Frameworks in scope</legend>
                    <div className="flex flex-wrap gap-3 mt-1" data-testid="incident-frameworks">
                        {enabledFrameworks.length === 0 && <span className="text-amber-800">No framework is enabled for this tenant — enable one under Frameworks.</span>}
                        {enabledFrameworks.map((f) => (
                            <label key={f.id} className="inline-flex items-center gap-1.5">
                                <input
                                    type="checkbox"
                                    checked={v.frameworks.includes(f.id)}
                                    onChange={(e) =>
                                        update({ frameworks: e.target.checked ? [...v.frameworks, f.id] : v.frameworks.filter((x) => x !== f.id) })
                                    }
                                    data-testid={`incident-framework-${f.id}`}
                                />
                                {f.title}
                            </label>
                        ))}
                    </div>
                </fieldset>
                <label className="block md:col-span-2">
                    <span className="text-muted uppercase tracking-wide">Description</span>
                    <textarea value={v.description} onChange={(e) => update({ description: e.target.value })} className={`${INPUT_CLASS} block w-full mt-0.5 h-20`} data-testid="incident-description" />
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Linked run ids</span>
                    <textarea value={v.linkedRunIds} onChange={(e) => update({ linkedRunIds: e.target.value })} className={`${INPUT_CLASS} block w-full mt-0.5 h-14 font-mono`} placeholder="one per line" data-testid="incident-runs" />
                </label>
                <label className="block">
                    <span className="text-muted uppercase tracking-wide">Linked inbox item ids</span>
                    <textarea
                        value={v.linkedInboxItemIds}
                        onChange={(e) => update({ linkedInboxItemIds: e.target.value })}
                        className={`${INPUT_CLASS} block w-full mt-0.5 h-14 font-mono`}
                        placeholder="one per line"
                        data-testid="incident-inbox-items"
                    />
                </label>
            </div>
            {(localError || error) && (
                <p className="text-red-800 bg-red-50 border border-red-300 rounded p-2" data-testid="incident-form-error" role="alert">
                    {localError || error}
                </p>
            )}
            <div className="flex items-center gap-2">
                <button type="submit" className={BUTTON_PRIMARY} disabled={saving} data-testid="incident-submit">
                    {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                    {initial ? 'Save changes' : 'Open incident'}
                </button>
                <button type="button" className={BUTTON_SECONDARY} onClick={onCancel} disabled={saving} data-testid="incident-cancel">
                    Cancel
                </button>
            </div>
        </form>
    );
}
