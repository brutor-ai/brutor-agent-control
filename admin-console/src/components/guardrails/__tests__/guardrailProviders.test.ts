/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A guardrail policy's provider entries and banned lists, as the form
 * sends them and as it explains the Control Plane's refusals.
 *
 * The Control Plane never returns a provider's secrets: an entry says
 * which are stored (`config_secrets_stored`). A save that leaves a secret
 * out keeps it, one that sends `null` removes it. The bodies of the
 * refusals below are the Control Plane's own.
 */

import { describe, expect, it } from 'vitest';

import {
    bannedEntriesFromText,
    bannedEntriesText,
    bedrockEntryProblem,
    guardrailSaveRefusalText,
    secretStored,
} from '../guardrailProviders';
import { thrownByClient } from '../../../test/serverAnswers';

const COMPLETE = { guardrail_id: 'gr-1', region: 'eu-west-1', aws_access_key_id: 'AKIAUNITTEST' };
const STORED = { aws_secret_access_key: true, aws_session_token: false };

describe('secretStored', () => {
    it('is what the Control Plane says of each secret of the entry', () => {
        expect(secretStored({ config_secrets_stored: STORED }, 'aws_secret_access_key')).toBe(true);
        expect(secretStored({ config_secrets_stored: STORED }, 'aws_session_token')).toBe(false);
        expect(secretStored({}, 'aws_secret_access_key')).toBe(false);
        expect(secretStored({ config_secrets_stored: null }, 'aws_secret_access_key')).toBe(false);
    });
});

describe('bedrockEntryProblem', () => {
    const entry = (config: Record<string, unknown>, stored: Record<string, boolean> = {}, enabled = true) => ({
        provider: 'bedrock',
        enabled,
        config,
        config_secrets_stored: stored,
    });

    it('asks for nothing when the secret access key is stored and not typed again', () => {
        expect(bedrockEntryProblem(entry(COMPLETE, STORED))).toBeNull();
    });

    it('asks for nothing when the secret access key is typed', () => {
        expect(bedrockEntryProblem(entry({ ...COMPLETE, aws_secret_access_key: 'unit-secret' }))).toBeNull();
    });

    it('asks for the secret access key when none is stored and none is typed, or the stored one is being removed', () => {
        expect(bedrockEntryProblem(entry(COMPLETE))).toBe(
            'Amazon Bedrock guardrail: AWS secret access key is required. The gateway calls a Bedrock guardrail with the credential entered here and uses no other.',
        );
        expect(bedrockEntryProblem(entry({ ...COMPLETE, aws_secret_access_key: null }, STORED))).toMatch(/AWS secret access key is required/);
    });

    it('names every part that is missing', () => {
        expect(bedrockEntryProblem(entry({}))).toBe(
            'Amazon Bedrock guardrail: Bedrock guardrail ID, AWS region, AWS access key ID and AWS secret access key are required. The gateway calls a Bedrock guardrail with the credential entered here and uses no other.',
        );
    });

    it('refuses a region that is not a region name', () => {
        expect(bedrockEntryProblem(entry({ ...COMPLETE, region: 'x.attacker.example/' }, STORED))).toBe(
            'Amazon Bedrock guardrail: AWS region must be a region name such as eu-west-1.',
        );
    });

    it('asks nothing of an entry that is switched off, or of another provider', () => {
        expect(bedrockEntryProblem(entry({}, {}, false))).toBeNull();
        expect(bedrockEntryProblem({ provider: 'lakera', enabled: true, config: {} })).toBeNull();
    });
});

describe('the entries of a banned list on one surface', () => {
    it('shows a string as it is and an object by its word or pattern', () => {
        expect(bannedEntriesText(['alpha', { word: 'beta', action: 'warn' }, { pattern: 'g.mma' }], 'word')).toBe('alpha\nbeta');
        expect(bannedEntriesText([{ pattern: 'g.mma', action: 'block' }, 'x+'], 'pattern')).toBe('g.mma\nx+');
        expect(bannedEntriesText(undefined, 'word')).toBe('');
    });

    it('keeps an entry that has its own action while its line is unchanged, and makes a new line a string', () => {
        const before = ['alpha', { word: 'beta', action: 'warn' }];
        expect(bannedEntriesFromText('alpha\nbeta\n  gamma  \n\n', before, 'word')).toEqual(['alpha', { word: 'beta', action: 'warn' }, 'gamma']);
        // The line was changed: it is a new entry, with the action of the check.
        expect(bannedEntriesFromText('alpha\nbetas', before, 'word')).toEqual(['alpha', 'betas']);
    });

    it('never produces an entry the gateway cannot read', () => {
        for (const entry of bannedEntriesFromText('a\nb', [{ word: 'a', action: 'warn' }, 7, null, { other: 1 }], 'word')) {
            expect(typeof entry === 'string' || typeof (entry as { word?: unknown }).word === 'string').toBe(true);
        }
    });
});

describe('guardrailSaveRefusalText', () => {
    const incomplete = (issues: string[]) => ({
        detail: { error: 'guardrail_provider_incomplete', message: 'Selected external providers are missing configuration.', issues },
    });

    it('says what an enabled Bedrock provider lacks, by the names of the fields on the page', async () => {
        const thrown = await thrownByClient(
            400,
            incomplete([
                'An enabled Bedrock provider requires config.region — the AWS region of the guardrail.',
                "An enabled Bedrock provider requires config.aws_secret_access_key — the guardrail's own AWS credential; the gateway signs with no other.",
            ]),
        );
        expect(guardrailSaveRefusalText(thrown)).toBe(
            'Nothing was saved.\nAmazon Bedrock guardrail provider: AWS region and AWS secret access key are required while the provider is enabled. Enter them (Providers tab), or switch the provider off.',
        );
    });

    it('says a check needs its provider, with the provider and what it needs', async () => {
        const thrown = await thrownByClient(
            400,
            incomplete([
                "prompt_injection_provider='lakera_guard' requires an enabled Lakera entry in providers[] with a non-empty api_key.",
                "toxic_content_provider='openai_moderation' requires an enabled OpenAI Moderation entry in providers[] with a non-empty api_key. Switch toxic_content_provider to 'built_in' for the wordlist fallback.",
                "pii_provider='presidio' requires an enabled Presidio entry in providers[] with a non-empty endpoint (the Analyzer URL). Leave pii_provider unset to use the built-in regex detector.",
                'An enabled Custom Webhook provider requires a non-empty endpoint — the URL the gateway POSTs content to.',
                "An enabled Bedrock provider's config.region is not an AWS region name (lower-case letters, digits and '-', such as eu-west-1).",
            ]),
        );
        expect(guardrailSaveRefusalText(thrown)?.split('\n')).toEqual([
            'Nothing was saved.',
            'Prompt injection is set to use Lakera Guard, which needs an enabled Lakera provider with an API key (Providers tab). Add it, or choose the built-in detector.',
            'Toxic content is set to use OpenAI Moderation, which needs an enabled OpenAI Moderation provider with an API key (Providers tab). Add it, or choose the built-in detector.',
            'PII is set to use Presidio, which needs an enabled Presidio provider with an endpoint URL (Providers tab). Add it, or choose the built-in detector.',
            'Custom webhook provider: an endpoint URL is required while the provider is enabled.',
            'Amazon Bedrock guardrail provider: AWS region must be a region name such as eu-west-1.',
        ]);
    });

    it('shows an issue it does not know as it came, never the code', async () => {
        const text = guardrailSaveRefusalText(await thrownByClient(400, incomplete(['Something new is missing.'])));
        expect(text).toBe('Nothing was saved.\nSomething new is missing.');
        expect(text).not.toContain('guardrail_provider_incomplete');
    });

    const validation = (loc: string[], msg: string) => ({ detail: [{ type: 'value_error', loc: ['body', ...loc], msg: `Value error, ${msg}` }] });
    const SYNTAX = 'Banned patterns are compiled with the Rust regex-lite syntax, which has no look-around, backreferences or Unicode classes';

    it('says a banned pattern uses look-around, which the gateway does not have', async () => {
        const thrown = await thrownByClient(
            422,
            validation(
                ['builtin', 'banned_patterns'],
                `Invalid regex pattern at index 2: '(?=secret)x' — the gateway cannot compile it: look-around ((?=..), (?!..), (?<=..), (?<!..)) is not supported. ${SYNTAX}`,
            ),
        );
        expect(guardrailSaveRefusalText(thrown)).toBe(
            'Nothing was saved.\nBanned pattern 3, (?=secret)x, cannot be used: it has look-around ((?=…), (?!…), (?<=…) or (?<!…)), which the gateway\'s patterns do not support. Rewrite the pattern without it.',
        );
    });

    it('says a banned pattern uses a backreference or a Unicode class, and shows the pattern as it was typed', async () => {
        // Python writes the pattern with its backslashes doubled.
        const back = await thrownByClient(
            422,
            validation(['builtin', 'banned_patterns'], `Invalid regex pattern at index 0: '(a)\\\\1' — the gateway cannot compile it: backreferences (and octal escapes) are not supported. ${SYNTAX}`),
        );
        expect(guardrailSaveRefusalText(back)).toBe(
            "Nothing was saved.\nBanned pattern 1, (a)\\1, cannot be used: it has a backreference such as \\1, which the gateway's patterns do not support. Rewrite the pattern without it.",
        );
        const unicode = await thrownByClient(
            422,
            validation(['builtin', 'banned_patterns'], `Invalid regex pattern at index 0: '\\\\p{L}+' — the gateway cannot compile it: Unicode character classes (\\p{..}) are not supported. ${SYNTAX}`),
        );
        expect(guardrailSaveRefusalText(unicode)).toMatch(/it has a Unicode class such as \\p\{L\}, which the gateway's patterns do not support/);
    });

    it('gives the reason of any other pattern the gateway cannot compile, and names the surface of an override', async () => {
        const thrown = await thrownByClient(
            422,
            validation(
                ['builtin', 'banned_patterns_per_surface'],
                `banned_patterns_per_surface['chat_input'].patterns: invalid regex pattern at index 0: '(a' — the gateway cannot compile it: found open group without closing ')'. ${SYNTAX}`,
            ),
        );
        expect(guardrailSaveRefusalText(thrown)).toBe(
            "Nothing was saved.\nBanned pattern 1 of the override for chat input, (a, cannot be used: found open group without closing ')'.",
        );
    });

    it('says an entry of a banned list on a surface has a shape the gateway cannot read', async () => {
        const shape = await thrownByClient(
            422,
            validation(['builtin', 'banned_words_per_surface'], "banned_words_per_surface['mcp_request'].words: the entry at index 1 must be a string or an object with a 'word' string"),
        );
        expect(guardrailSaveRefusalText(shape)).toBe(
            'Nothing was saved.\nBanned word 2 of the override for mcp request is not a word the gateway can read. Each entry is one word or phrase on its own line.',
        );
        const action = await thrownByClient(
            422,
            validation(['builtin', 'banned_words_per_surface'], "banned_words_per_surface['mcp_request'].words: the action of the entry at index 0 must be one of: block, warn"),
        );
        expect(guardrailSaveRefusalText(action)).toBe(
            'Nothing was saved.\nBanned word 1 of the override for mcp request has an action of its own that is not Block or Warn.',
        );
    });

    it('says a credential cannot be stored without an encryption key', async () => {
        const thrown = await thrownByClient(503, {
            detail:
                "A guardrail provider's credential cannot be stored: this control plane has no encryption service (ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved.",
        });
        expect(guardrailSaveRefusalText(thrown)).toBe(
            "A guardrail provider's credential cannot be stored: this Control Plane has no encryption key (ENCRYPTION_KEY), and a credential is never stored unencrypted. Nothing was saved. Ask whoever runs the installation to set ENCRYPTION_KEY, then save again.",
        );
    });

    it('is undefined for any other failure', async () => {
        expect(guardrailSaveRefusalText(await thrownByClient(400, { detail: 'Name already in use' }))).toBeUndefined();
        expect(guardrailSaveRefusalText(await thrownByClient(422, validation(['name'], 'Field required')))).toBeUndefined();
        expect(guardrailSaveRefusalText(new Error('network'))).toBeUndefined();
    });
});
