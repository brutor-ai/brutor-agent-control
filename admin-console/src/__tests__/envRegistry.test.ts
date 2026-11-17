/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The environment variables of the Admin Console are defined once and read
 * in one place.
 *
 * `src/envRegistry.json` defines each variable (name, type, default,
 * description, example, edition). `src/env.ts`
 * is the only module that reads the application's configuration: at build
 * time from Vite, at run time from `window._env_`, which the container's
 * entrypoint writes. `scripts/tool-env.mjs` is the only module that reads
 * the environment of the build tools and tests. The table in the README
 * and `.env.example` are written from the registry, so a variable that is
 * renamed, added or dropped cannot be documented under another name.
 *
 * The Community package holds this to the Community tree. The Enterprise
 * package runs it again over both.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import {
    BOOLEAN_SPELLINGS,
    GENERATED_DOCUMENTS,
    generatedDocument,
} from '../../scripts/gen-env-docs.mjs';
import { parseBoolean, readRegistry, toolEnv, toolFlag } from '../../scripts/tool-env.mjs';
import { packageOf, textFiles } from '../test/privateReferences';
import { REPO_ROOT, enterpriseUnderTest } from '../test/sourceRoots';

const inScope = (file: string): boolean => enterpriseUnderTest() || packageOf(file) === 'community';
const editions = (): Array<'community' | 'enterprise'> => (enterpriseUnderTest() ? ['community', 'enterprise'] : ['community']);
const read = (file: string): string => readFileSync(path.join(REPO_ROOT, file), 'utf8');

describe('the registry of environment variables', () => {
    it.each(editions())('defines every %s variable completely, once', (edition) => {
        const registry = readRegistry(edition);
        expect(registry, `the ${edition} registry is there`).not.toBeNull();
        const names = new Set<string>();
        for (const variable of registry?.variables ?? []) {
            for (const field of ['name', 'type', 'edition', 'description', 'accepted', 'example', 'read_by'] as const) {
                expect(variable[field], `${variable.name}: ${field}`).toEqual(expect.any(String));
                expect(String(variable[field]).length, `${variable.name}: ${field}`).toBeGreaterThan(0);
            }
            expect(['application', 'tool']).toContain(variable.read_by);
            expect(['both', 'community', 'enterprise']).toContain(variable.edition);
            expect(['url', 'boolean', 'path', 'string']).toContain(variable.type);
            for (const name of [variable.name, variable.build_name]) {
                if (name === null) continue;
                expect(names.has(name), `${name} is defined once`).toBe(false);
                names.add(name);
            }
        }
        // A switch says so in the one spelling every variable accepts.
        const switches = (registry?.variables ?? []).filter((variable) => variable.type === 'boolean');
        expect(switches.map((variable) => variable.accepted)).toEqual(switches.map(() => `one of ${BOOLEAN_SPELLINGS.join(', ')}`));
    });

    it.each(editions())('the %s registry defines the variables that are read, and nothing about names that are not', (edition) => {
        // No backwards compatibility: no old name is listed, refused or read.
        const registry = readRegistry(edition) as unknown as Record<string, unknown> & { variables: Record<string, unknown>[] };
        expect(Object.keys(registry).sort()).toEqual(['$comment', 'component', 'schema_version', 'variables']);
        for (const variable of registry.variables) {
            expect({ name: variable.name, fields: Object.keys(variable).sort() }).toEqual({
                name: variable.name,
                fields: ['accepted', 'build_name', 'default', 'description', 'edition', 'example', 'name', 'read_by', 'type'],
            });
        }
    });

    it('names the application\'s variables BRUTOR_* in a container and VITE_* in a build from source', () => {
        const application = (readRegistry('community')?.variables ?? []).filter((variable) => variable.read_by === 'application');
        expect(application.map((variable) => variable.name)).toEqual([
            'BRUTOR_CONTROL_PLANE_PUBLIC_URL',
            'BRUTOR_GATEWAY_PUBLIC_URL',
            'BRUTOR_DEBUG_ENABLED',
        ]);
        for (const variable of application) expect(variable.name).toMatch(/^BRUTOR_[A-Z_]+$/);
        // The build-time name is the run-time name with the Vite prefix.
        const built = application.filter((variable) => variable.build_name !== null);
        expect(built.map((variable) => variable.build_name)).toEqual(built.map((variable) => variable.name.replace(/^BRUTOR_/, 'VITE_')));
    });

    it('keeps the Enterprise-only variable out of the Community registry', () => {
        const community = (readRegistry('community')?.variables ?? []).map((variable) => variable.name);
        expect(community).not.toContain('BRUTOR_OBFUSCATE_FULL_ENABLED');
        expect((readRegistry('community')?.variables ?? []).every((variable) => variable.edition !== 'enterprise')).toBe(true);
    });
});

describe('what is written from the registry', () => {
    it.each(GENERATED_DOCUMENTS.filter((document) => inScope(document.file)).map((document) => [document.file, document] as const))(
        '%s is what the registry gives',
        (file, document) => {
            expect(read(file), `${file} is out of date: node scripts/gen-env-docs.mjs writes it again`).toBe(generatedDocument(document, read(file)));
        },
    );

    it('covers the README, the example file and the Enterprise README', () => {
        expect(GENERATED_DOCUMENTS.map((document) => document.file)).toEqual(['README.md', '.env.example', 'enterprise/README.md']);
    });

    it('the README lists every variable', () => {
        const readme = read('README.md');
        for (const variable of readRegistry('community')?.variables ?? []) expect(readme).toContain(`\`${variable.name}\``);
    });

    it('the example file sets the build-time names to their defaults', () => {
        const example = read('.env.example');
        expect(example).toContain('\nVITE_CONTROL_PLANE_PUBLIC_URL=http://localhost:5050\n');
        expect(example).toContain('\nVITE_GATEWAY_PUBLIC_URL=http://localhost:8100\n');
    });
});

describe('the build tools\' reader', () => {
    it('reads a tool variable of the registry, and refuses a name that is not one', () => {
        expect(toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR', { BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR: '/tmp/x' })).toBe('/tmp/x');
        expect(toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR', {})).toBeUndefined();
        expect(toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR', { BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR: '' })).toBeUndefined();
        expect(() => toolEnv('HOME', { HOME: '/root' })).toThrow('HOME is not a build-tool variable of the registry');
        expect(() => toolEnv('BRUTOR_GATEWAY_PUBLIC_URL', {})).toThrow('is not a build-tool variable of the registry');
    });

    it('reads a tool variable whatever else the environment sets: a name it does not define is not read', () => {
        const environment = { BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR: '/tmp/x', OBFUSCATE_FULL: '1', GATEWAY_URL: 'http://a', NODE_ENV: 'production' };
        expect(toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR', environment)).toBe('/tmp/x');
        expect(toolFlag('PRIVATE_REFERENCES_REPORT', environment)).toBe(false);
    });

    it('reads a switch in the one spelling every variable accepts, and refuses another without printing it', () => {
        for (const yes of ['true', '1', 'yes', 'on', 'TRUE']) expect(parseBoolean(yes)).toBe(true);
        for (const no of ['false', '0', 'no', 'off']) expect(parseBoolean(no)).toBe(false);
        expect(parseBoolean('maybe')).toBeUndefined();
        expect(toolFlag('PRIVATE_REFERENCES_REPORT', { PRIVATE_REFERENCES_REPORT: '1' })).toBe(true);
        expect(toolFlag('PRIVATE_REFERENCES_REPORT', {})).toBe(false);
        expect(() => toolFlag('PRIVATE_REFERENCES_REPORT', { PRIVATE_REFERENCES_REPORT: 'maybe' })).toThrow(
            'PRIVATE_REFERENCES_REPORT is not valid: it must be one of true, false, 1, 0, yes, no, on, off',
        );
    });
});

describe('nothing else reads the environment', () => {
    /** What may name each source of configuration, and why. */
    const READERS: Readonly<Record<string, readonly string[]>> = {
        'import.meta.env': [
            // The one module that reads the application's configuration.
            'src/env.ts',
            // The types of what Vite puts there.
            'src/vite-env.d.ts',
        ],
        'process.env': [
            // The one module that reads the build tools' environment.
            'scripts/tool-env.mjs',
        ],
        'window._env_': [
            'src/env.ts',
            // The container's entrypoint writes it; the file it writes over leaves it unset.
            'docker-entrypoint.sh',
            'public/env-config.js',
        ],
    };
    const PATTERNS: Readonly<Record<string, RegExp>> = {
        'import.meta.env': /import\.meta\.env/,
        'process.env': /process\.env/,
        'window._env_': /\b_env_\b/,
    };
    const CODE = /\.(?:ts|tsx|js|jsx|mjs|cjs|sh|html)$|(?:^|\/)Dockerfile$/;
    const isTest = (file: string): boolean => /\.(?:test|spec)\.tsx?$|(?:^|\/)__tests__\/|(?:^|\/)src\/test\//.test(file);

    it.each(Object.keys(PATTERNS))('%s is named by its reader only', (source) => {
        const offenders = textFiles()
            .filter(inScope)
            .filter((file) => CODE.test(file) && !READERS[source].includes(file))
            // A test may set `window._env_` for the module it tests; it reads the environment through the readers like everything else.
            .filter((file) => !(source === 'window._env_' && isTest(file)))
            .filter((file) => file !== 'src/__tests__/envRegistry.test.ts')
            .filter((file) => PATTERNS[source].test(read(file)));
        expect(offenders, `These name ${source}. Read it through ${READERS[source][0]}, or say what the code does without naming it.`).toEqual([]);
    });

    it('the readers are there, and read', () => {
        expect(read('src/env.ts')).toMatch(/import\.meta\.env/);
        expect(read('src/env.ts')).toMatch(/window\._env_/);
        expect(read('scripts/tool-env.mjs')).toMatch(/process\.env/);
    });
});
