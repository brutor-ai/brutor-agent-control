/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Form state ⇄ wire bodies for the console's two write forms — the
 * incident register and the AI-interaction notice
 * config — kept out of the component files so each of those exports
 * only its component, and so the mapping is unit-testable on its own.
 */

import type {
    ComplianceIncident,
    IncidentCreateRequest,
    IncidentUpdateRequest,
    NoticeConfig,
    NoticeConfigPutRequest,
} from '../../lib/complianceEvidenceTypes';
import { parseIdList } from './complianceFormat';

// ─── Incidents ───────────────────────────────────────────────────────────────

export interface IncidentFormValues {
    title: string;
    description: string;
    classification: string;
    severity: string;
    becameAwareAt: string; // datetime-local
    /** When the incident was classified as major / significant
     *  (datetime-local, optional): DORA's initial notification runs 4 h
     *  from it, at most 24 h from awareness. */
    classifiedAt: string;
    frameworks: string[];
    aiSystemId: string;
    linkedRunIds: string;
    linkedInboxItemIds: string;
}

/** `2026-09-19T10:30` for a datetime-local input, from an ISO instant. */
export function toLocalInput(iso: string | null | undefined): string {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function incidentValuesFrom(incident: ComplianceIncident | null): IncidentFormValues {
    return {
        title: incident?.title ?? '',
        description: incident?.description ?? '',
        classification: incident?.classification ?? '',
        severity: incident?.severity ?? 'medium',
        becameAwareAt: toLocalInput(incident?.became_aware_at) || toLocalInput(new Date().toISOString()),
        classifiedAt: toLocalInput(incident?.classified_at),
        frameworks: incident?.frameworks ?? [],
        aiSystemId: incident?.ai_system_id ?? '',
        linkedRunIds: (incident?.linked_run_ids ?? []).join('\n'),
        linkedInboxItemIds: (incident?.linked_inbox_item_ids ?? []).join('\n'),
    };
}

export function validateIncident(v: IncidentFormValues): string | null {
    if (!v.title.trim()) return 'A title is required.';
    if (!v.classification.trim()) return 'A classification is required (it selects the deadline rules).';
    if (!v.becameAwareAt) return 'When did you become aware of it?';
    if (Number.isNaN(new Date(v.becameAwareAt).getTime())) return 'The awareness time is not a valid date.';
    if (v.classifiedAt) {
        const classified = new Date(v.classifiedAt).getTime();
        if (Number.isNaN(classified)) return 'The classification time is not a valid date.';
        if (classified < new Date(v.becameAwareAt).getTime()) return 'The incident cannot be classified before you became aware of it.';
    }
    if (v.frameworks.length === 0) return 'Choose at least one framework in scope.';
    return null;
}

export function toIncidentCreateBody(v: IncidentFormValues): IncidentCreateRequest {
    return {
        title: v.title.trim(),
        description: v.description,
        classification: v.classification.trim(),
        severity: v.severity,
        became_aware_at: new Date(v.becameAwareAt).toISOString(),
        classified_at: v.classifiedAt ? new Date(v.classifiedAt).toISOString() : null,
        frameworks: v.frameworks,
        ai_system_id: v.aiSystemId || null,
        linked_run_ids: parseIdList(v.linkedRunIds),
        linked_inbox_item_ids: parseIdList(v.linkedInboxItemIds),
    };
}

export function toIncidentUpdateBody(v: IncidentFormValues): IncidentUpdateRequest {
    const body = toIncidentCreateBody(v);
    return {
        title: body.title,
        description: body.description,
        classification: body.classification,
        severity: body.severity,
        became_aware_at: body.became_aware_at,
        classified_at: body.classified_at,
        frameworks: body.frameworks,
        ai_system_id: body.ai_system_id,
        linked_run_ids: body.linked_run_ids,
        linked_inbox_item_ids: body.linked_inbox_item_ids,
    };
}

// ─── Notice config ───────────────────────────────────────────────────────────

export interface NoticeForm {
    enabled: boolean;
    surface: string;
    texts: Record<string, string>;
    exemptionAsserted: boolean;
    exemptionBasis: string;
    exemptionRationale: string;
}

export function noticeFormFrom(config: NoticeConfig | null, effective: NoticeConfig | null): NoticeForm {
    const base = config ?? effective;
    return {
        enabled: base?.enabled ?? true,
        surface: base?.surface ?? 'both',
        texts: { ...(config?.texts ?? {}) },
        exemptionAsserted: config?.exemption?.asserted ?? false,
        exemptionBasis: config?.exemption?.basis ?? '',
        exemptionRationale: config?.exemption?.rationale ?? '',
    };
}

/** The PUT body: locales with empty text are dropped; an exemption travels
 *  only when asserted. */
export function toNoticePutBody(form: NoticeForm): NoticeConfigPutRequest {
    const texts: Record<string, string> = {};
    for (const [locale, text] of Object.entries(form.texts)) {
        if (text.trim()) texts[locale] = text;
    }
    return {
        enabled: form.enabled,
        surface: form.surface,
        texts,
        exemption: form.exemptionAsserted
            ? { asserted: true, basis: form.exemptionBasis || null, rationale: form.exemptionRationale.trim() || null }
            : null,
    };
}
