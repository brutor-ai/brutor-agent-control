/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Whether the gateway serves a model, as the console can tell.
 *
 * The gateway calls a model only when all of these hold: the model is
 * enabled, it is not retired (it has no retire date, or that date is still
 * ahead, by the UTC day: from its retire date on a model takes no call),
 * and its provider is enabled. A routing group's virtual model stands for
 * no provider of its own: its members are judged one by one. A model that
 * is not served is answered exactly as a disabled one is.
 *
 * A disabled model already says so wherever it is shown. This module is
 * about the two reasons a model that reads "enabled" takes no call.
 */

export type NotServedReason = { why: 'retired'; retireDate: string } | { why: 'provider_disabled'; provider: string };

interface ModelRow {
    retire_date?: string | null;
    provider?: string | null;
    mode?: string | null;
}

interface ProviderRow {
    key: string;
    enabled?: boolean | null;
}

/** The UTC day of `now`, as `YYYY-MM-DD`. */
const utcDay = (now: Date): string => now.toISOString().slice(0, 10);

/**
 * Why the gateway does not serve `model`, beyond its own switch; empty when
 * it does, as far as `providers` tells. A provider that is not in the list
 * is not judged: the list may not have been read yet.
 */
export function notServedReasons(model: ModelRow, providers: readonly ProviderRow[], now: Date = new Date()): NotServedReason[] {
    const reasons: NotServedReason[] = [];
    const retireDate = (model.retire_date ?? '').slice(0, 10);
    // Two `YYYY-MM-DD` dates compare as text.
    if (/^\d{4}-\d{2}-\d{2}$/.test(retireDate) && retireDate <= utcDay(now)) {
        reasons.push({ why: 'retired', retireDate });
    }
    if (model.mode !== 'routing' && model.provider) {
        const provider = providers.find((row) => row.key === model.provider);
        if (provider && provider.enabled === false) reasons.push({ why: 'provider_disabled', provider: model.provider });
    }
    return reasons;
}

/** The reasons in plain words; '' for none. */
export function notServedText(reasons: readonly NotServedReason[]): string {
    if (reasons.length === 0) return '';
    const retired = reasons.find((reason) => reason.why === 'retired');
    const provider = reasons.find((reason) => reason.why === 'provider_disabled');
    const start = 'The gateway does not serve this model:';
    if (retired?.why === 'retired') {
        const also = provider?.why === 'provider_disabled' ? ` Its provider (${provider.provider}) is also disabled.` : '';
        return `The gateway does not serve this model. Retire date: ${retired.retireDate}. From its retire date on, a model takes no call.${also}`;
    }
    if (provider?.why === 'provider_disabled') {
        return `${start} its provider (${provider.provider}) is disabled. A model of a disabled provider takes no call.`;
    }
    return '';
}
