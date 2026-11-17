/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A guardrail policy's provider entries and banned lists: what the form
 * (`GuardrailForm.tsx`) checks and sends, and the Control Plane's refusals
 * of a save in plain words.
 *
 * **Secrets.** A provider entry has two kinds: its `api_key`, and for
 * Amazon Bedrock the secret access key and the session token in `config`.
 * No response carries any of them. An entry says which secrets of `config`
 * are stored (`config_secrets_stored`). A save that leaves a secret out
 * keeps the stored one, and one that sends `null` removes it, so the form
 * holds only what was typed.
 *
 * **Banned patterns.** The gateway compiles them with a pattern language
 * that has no look-around, no backreferences and no Unicode classes. The
 * Control Plane refuses a pattern the gateway could not compile.
 */

import { getArray, getNumber, getRecord, getString, isRecord } from '../../lib/narrow';
import { isAwsRegionName } from '../llm/config/providerConnection';

/** The secrets of a Bedrock entry's `config`. */
export const BEDROCK_CONFIG_SECRETS = ['aws_secret_access_key', 'aws_session_token'] as const;

/** Whether the Control Plane holds a value for `field` of the entry's `config`. */
export function secretStored(entry: { config_secrets_stored?: Record<string, boolean> | null }, field: string): boolean {
    return entry.config_secrets_stored?.[field] === true;
}

/** The keys of a Bedrock guardrail entry's `config` the gateway cannot do without, with their labels. */
const BEDROCK_ENTRY_REQUIRED: ReadonlyArray<{ field: string; label: string }> = [
    { field: 'guardrail_id', label: 'Bedrock guardrail ID' },
    { field: 'region', label: 'AWS region' },
    { field: 'aws_access_key_id', label: 'AWS access key ID' },
    { field: 'aws_secret_access_key', label: 'AWS secret access key' },
];
const BEDROCK_LABELS: Readonly<Record<string, string>> = Object.fromEntries(BEDROCK_ENTRY_REQUIRED.map(({ field, label }) => [field, label]));

const listed = (names: readonly string[]): string =>
    names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;

/**
 * Why an enabled Amazon Bedrock guardrail entry cannot be saved; null for
 * one that can, for a disabled one and for an entry of another provider.
 * The gateway calls a Bedrock guardrail with the entry's own region and AWS
 * credential and reads no other: an entry without them cannot be built,
 * which on a fail-closed policy blocks the policy's calls. A secret counts
 * as there when it is typed, or stored and not being removed.
 */
export function bedrockEntryProblem(entry: {
    provider?: string;
    enabled?: boolean;
    config?: Record<string, unknown> | null;
    config_secrets_stored?: Record<string, boolean> | null;
}): string | null {
    if (entry.provider !== 'bedrock' || !entry.enabled) return null;
    const config = entry.config ?? {};
    const missing = BEDROCK_ENTRY_REQUIRED.filter(({ field }) => {
        const value = config[field];
        if (typeof value === 'string' && value.trim() !== '') return false;
        return !(value === undefined && secretStored(entry, field));
    }).map(({ label }) => label);
    if (missing.length > 0) {
        return `Amazon Bedrock guardrail: ${listed(missing)} ${missing.length === 1 ? 'is' : 'are'} required. The gateway calls a Bedrock guardrail with the credential entered here and uses no other.`;
    }
    if (!isAwsRegionName(String(config.region).trim())) {
        return 'Amazon Bedrock guardrail: AWS region must be a region name such as eu-west-1.';
    }
    return null;
}

// --- The entries of a banned list on one surface ------------------------------

/**
 * An entry of a per-surface banned list, as the gateway reads it: a string,
 * or an object with the word or pattern and an action of its own.
 */
export type BannedEntry = string | Record<string, unknown>;
type BannedKey = 'word' | 'pattern';

const entryText = (entry: unknown, key: BannedKey): string | undefined =>
    typeof entry === 'string' ? entry : getString(entry, key);

/** The entries as lines of text: each by its word or pattern. An entry of another shape is left out. */
export function bannedEntriesText(entries: unknown, key: BannedKey): string {
    if (!Array.isArray(entries)) return '';
    return (entries as unknown[]).flatMap((entry) => entryText(entry, key) ?? []).join('\n');
}

/**
 * The entries for the lines of `text`. A line that is the word or pattern
 * of an entry of `before` that has an action of its own stays that entry;
 * any other line is a string, which takes the action of the check.
 */
export function bannedEntriesFromText(text: string, before: unknown, key: BannedKey): BannedEntry[] {
    const withAction = new Map<string, Record<string, unknown>>();
    for (const entry of Array.isArray(before) ? (before as unknown[]) : []) {
        const value = getString(entry, key);
        if (isRecord(entry) && value !== undefined) withAction.set(value, entry);
    }
    return text
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => withAction.get(line) ?? line);
}

// --- What the Control Plane refuses when a policy is saved ---------------------

const NOTHING_SAVED = 'Nothing was saved.';

const CHECK_NAMES: Readonly<Record<string, string>> = {
    pii: 'PII',
    prompt_injection: 'Prompt injection',
    jailbreak: 'Jailbreak',
    toxic_content: 'Toxic content',
};
const SELECTED_PROVIDERS: Readonly<Record<string, { name: string; entry: string; needs: string }>> = {
    lakera_guard: { name: 'Lakera Guard', entry: 'Lakera', needs: 'an API key' },
    openai_moderation: { name: 'OpenAI Moderation', entry: 'OpenAI Moderation', needs: 'an API key' },
    presidio: { name: 'Presidio', entry: 'Presidio', needs: 'an endpoint URL' },
};

/** The issues of a `guardrail_provider_incomplete` answer, in the words of the page. */
function incompleteProviderLines(issues: readonly unknown[]): string[] {
    const lines: string[] = [];
    const bedrockMissing: string[] = [];
    for (const issue of issues) {
        if (typeof issue !== 'string') continue;
        const bedrock = /Bedrock provider requires config\.(\w+)/.exec(issue);
        if (bedrock) {
            bedrockMissing.push(BEDROCK_LABELS[bedrock[1]] ?? bedrock[1].replace(/_/g, ' '));
            continue;
        }
        if (issue.includes('config.region is not an AWS region name')) {
            lines.push('Amazon Bedrock guardrail provider: AWS region must be a region name such as eu-west-1.');
            continue;
        }
        const selected = /^(\w+)_provider='(\w+)' requires/.exec(issue);
        const provider = selected && SELECTED_PROVIDERS[selected[2]];
        if (selected && provider) {
            const check = CHECK_NAMES[selected[1]] ?? selected[1].replace(/_/g, ' ');
            lines.push(
                `${check} is set to use ${provider.name}, which needs an enabled ${provider.entry} provider with ${provider.needs} (Providers tab). Add it, or choose the built-in detector.`,
            );
            continue;
        }
        if (issue.includes('Custom Webhook provider requires a non-empty')) {
            lines.push('Custom webhook provider: an endpoint URL is required while the provider is enabled.');
            continue;
        }
        lines.push(issue);
    }
    if (bedrockMissing.length > 0) {
        lines.unshift(
            `Amazon Bedrock guardrail provider: ${listed(bedrockMissing)} ${bedrockMissing.length === 1 ? 'is' : 'are'} required while the provider is enabled. Enter ${bedrockMissing.length === 1 ? 'it' : 'them'} (Providers tab), or switch the provider off.`,
        );
    }
    return lines;
}

/** `banned_words_per_surface['chat_input'].words` → " of the override for chat input"; '' for the list of the check. */
function overrideOf(message: string): string {
    const surface = /_per_surface\[['"]([\w-]+)['"]\]/.exec(message)?.[1];
    return surface ? ` of the override for ${surface.replace(/[_-]/g, ' ')}` : '';
}

const UNSUPPORTED = "which the gateway's patterns do not support. Rewrite the pattern without it.";

/** One field error of a validation answer about a banned list, in plain words; undefined for any other. */
function bannedListLine(message: string): string | undefined {
    const pattern = /invalid regex pattern at index (\d+): (['"])(.*)\2 — (.*)$/is.exec(message);
    if (pattern) {
        // Python wrote the pattern as a string literal: its backslashes are doubled.
        const typed = pattern[3].replace(/\\\\/g, '\\');
        const subject = `Banned pattern ${Number(pattern[1]) + 1}${overrideOf(message)}, ${typed}, cannot be used:`;
        // The reason alone: without who could not compile it, and without the
        // sentence about the pattern language that ends every such refusal.
        const reason = pattern[4]
            .replace(/\. Banned patterns are compiled.*$/s, '')
            .replace(/^the (gateway|control plane) cannot compile it[^:]*: /, '');
        if (reason.startsWith('look-around')) return `${subject} it has look-around ((?=…), (?!…), (?<=…) or (?<!…)), ${UNSUPPORTED}`;
        if (reason.startsWith('backreferences')) return `${subject} it has a backreference such as \\1, ${UNSUPPORTED}`;
        if (reason.startsWith('Unicode character classes')) return `${subject} it has a Unicode class such as \\p{L}, ${UNSUPPORTED}`;
        return `${subject} ${reason}.`;
    }
    const list = /(words|patterns): the (action of the )?entry at index (\d+) must be/.exec(message);
    if (list) {
        const noun = list[1] === 'words' ? 'word' : 'pattern';
        const subject = `Banned ${noun} ${Number(list[3]) + 1}${overrideOf(message)}`;
        return list[2]
            ? `${subject} has an action of its own that is not Block or Warn.`
            : `${subject} is not a ${noun} the gateway can read. Each entry is one ${noun === 'word' ? 'word or phrase' : 'pattern'} on its own line.`;
    }
    return undefined;
}

/**
 * The Control Plane's refusal of a guardrail policy, in plain words;
 * `undefined` for any other failure. `error` is what the API client threw.
 *
 *   400 `guardrail_provider_incomplete`  a check or an enabled provider lacks what it needs (`issues`)
 *   422 a field error of a banned list   a pattern the gateway cannot compile, an entry it cannot read
 *   503 `... cannot be stored`           no `ENCRYPTION_KEY` on the Control Plane
 */
export function guardrailSaveRefusalText(error: unknown): string | undefined {
    const body = getRecord(error, 'details');
    if (!body) return undefined;
    const said = getString(body, 'detail');
    if (getNumber(error, 'status') === 503 && said?.startsWith("A guardrail provider's credential cannot be stored")) {
        return `A guardrail provider's credential cannot be stored: this Control Plane has no encryption key (ENCRYPTION_KEY), and a credential is never stored unencrypted. ${NOTHING_SAVED} Ask whoever runs the installation to set ENCRYPTION_KEY, then save again.`;
    }
    const detail = getRecord(body, 'detail');
    if (getString(detail, 'error') === 'guardrail_provider_incomplete') {
        const lines = incompleteProviderLines(getArray(detail, 'issues') ?? []);
        return [NOTHING_SAVED, ...(lines.length > 0 ? lines : ['A check or a provider that is enabled lacks its configuration.'])].join('\n');
    }
    const fieldErrors = getArray(body, 'detail');
    if (fieldErrors) {
        const lines = fieldErrors.flatMap((item) => bannedListLine(getString(item, 'msg') ?? '') ?? []);
        if (lines.length > 0) return [NOTHING_SAVED, ...lines].join('\n');
    }
    return undefined;
}
