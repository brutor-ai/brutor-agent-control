/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The ESLint rule on what `console.warn` and `console.error` print
 * (`eslint.config.js`, the block named `console/failure-kind`; the rule is
 * `scripts/eslint/console-failure-kind.js`): a sentence, and the kind of a
 * failure (`errorKind`, `errorStatus`). A caught value is never printed
 * whole, nor its message, nor an object.
 *
 * The rule is run here as the configuration defines it, on text, and the
 * configuration has it switched on, as an error, for production source.
 */

import path from 'node:path';

import { ESLint, type Linter } from 'eslint';
import tseslint from 'typescript-eslint';
import { describe, expect, it } from 'vitest';

import config, { CONSOLE_FAILURE_KIND } from '../../../eslint.config.js';
import { REPO_ROOT } from '../../test/sourceRoots';

const blocks = config as Linter.Config[];
const block = blocks.filter((candidate) => candidate.name === CONSOLE_FAILURE_KIND);

/** The block alone, with the TypeScript parser: the rule reads scopes, not types. */
const eslint = new ESLint({
    cwd: REPO_ROOT,
    overrideConfigFile: true,
    overrideConfig: [{ files: ['**/*.{ts,tsx}'], languageOptions: { parser: tseslint.parser } }, ...block],
});

async function refused(text: string, file = 'src/components/x.ts'): Promise<string[]> {
    const [result] = await eslint.lintText(text, { filePath: path.join(REPO_ROOT, file) });
    return result.messages.map((message) => `${message.line}: ${message.ruleId} ${message.messageId} (${message.severity === 2 ? 'error' : 'warning'})`);
}

const RULE = 'brutor-console/failure-kind';

describe('the lint rule on what console.warn and console.error print', () => {
    it('is one block of the configuration, for production source', () => {
        expect(block).toHaveLength(1);
        expect(block[0].files).toEqual(['src/**/*.{ts,tsx}']);
        expect(block[0].ignores).toEqual(['**/*.{test,spec}.{ts,tsx}', '**/__tests__/**', 'src/test/**']);
        expect(block[0].rules).toEqual({ [RULE]: 'error' });
    });

    it('refuses a caught value printed whole, from a catch clause or a rejection handler', async () => {
        const text = [
            'try { work(); } catch (error) {',
            "    console.error('Failed to load:', error);",
            '}',
            'try { work(); } catch (err) {',
            "    console.warn('Failed to refresh:', err);",
            '}',
            'promise.catch((e) => {',
            "    console.error('Failed:', e);",
            '});',
            'promise.then(done, function (reason) {',
            "    console.error('Failed:', reason);",
            '});',
        ].join('\n');
        expect(await refused(text)).toEqual([
            `2: ${RULE} caught (error)`,
            `5: ${RULE} caught (error)`,
            `8: ${RULE} caught (error)`,
            `11: ${RULE} caught (error)`,
        ]);
    });

    it('refuses a part or a rendering of a caught value: its message, its stack, a template, a string', async () => {
        const text = [
            'try { work(); } catch (error) {',
            "    console.error('Failed:', error.message);",
            '    console.error(`Failed: ${error}`);',
            "    console.error('Failed:', String(error));",
            "    console.error('Failed:', errorMessage(error, 'Failed'));",
            "    console.error('Failed:', (error as Error).stack);",
            "    console.error('Failed:', { error, context });",
            '}',
        ].join('\n');
        expect(await refused(text)).toEqual([2, 3, 4, 5, 6, 7].map((line) => `${line}: ${RULE} caught (error)`));
    });

    it('refuses any value that is not a sentence or the kind of a failure: an identifier, an object, a field', async () => {
        const text = [
            'function report(error: unknown, context: { operation: string }) {',
            "    console.error('Gateway API Error:', error);",
            "    console.error('Gateway API Error:', { error, context });",
            "    console.error('Refused:', response.data);",
            "    console.warn('Answer:', [first, second]);",
            '    console.error(error);',
            '}',
        ].join('\n');
        expect(await refused(text)).toEqual([2, 3, 4, 5, 6].map((line) => `${line}: ${RULE} value (error)`));
    });

    it('takes a sentence and the kind of the failure', async () => {
        const text = [
            'try { work(); } catch (error) {',
            "    console.error('Failed to load:', errorKind(error));",
            '    console.error(`Health check failed for server ${serverId}:`, errorKind(error));',
            "    console.warn('Refused with status', errorStatus(error));",
            "    console.error('Authentication required to list the tenants.');",
            '    console.error(`Refused (${errorKind(error)})`);',
            '    showToast(error);',
            '    throw error;',
            '}',
            'promise.catch((e: unknown) => console.error(`[async:${context}]`, errorKind(e)));',
        ].join('\n');
        expect(await refused(text)).toEqual([]);
    });

    it('holds a name that shadows nothing: a parameter of an ordinary callback named like a caught value is a value', async () => {
        const text = [
            'try { work(); } catch (error) {',
            '    items.forEach((error) => {',
            '        console.error(`Item ${error.id} failed`);',
            '    });',
            '}',
        ].join('\n');
        expect(await refused(text)).toEqual([]);
    });

    it('says nothing of the other writers, which no-console refuses, or of a test', async () => {
        expect(await refused("try { work(); } catch (error) { console.log('x', error); }")).toEqual([]);
        expect(await refused("try { work(); } catch (error) { console.error('x', error); }", 'src/components/__tests__/x.test.ts')).toEqual([]);
        expect(await refused("try { work(); } catch (error) { console.error('x', error); }", 'src/test/x.ts')).toEqual([]);
    });

    it('is on, as an error, for production source in the configuration as a whole, and nothing after it switches it off', async () => {
        const whole = new ESLint({ cwd: REPO_ROOT });
        for (const file of ['src/index.tsx', 'src/lib/asyncSafe.ts', 'src/lib/gatewayApi/base.ts', 'src/hooks/useAuth.tsx']) {
            const rules = ((await whole.calculateConfigForFile(path.join(REPO_ROOT, file))) as { rules: Record<string, unknown[]> }).rules;
            expect({ file, rule: rules[RULE]?.[0] }).toEqual({ file, rule: 2 });
        }
        const test = ((await whole.calculateConfigForFile(path.join(REPO_ROOT, 'src/lib/__tests__/asyncSafe.test.ts'))) as { rules: Record<string, unknown[]> }).rules;
        expect(test[RULE]?.[0] ?? 0).toBe(0);
    });
});
