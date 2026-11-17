/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The API client's module registry.
 *
 * The client is a base plus 42 modules, composed onto one prototype. These
 * tests hold the composition to what a single class offers: every method
 * answered once, by one module,
 * with the base's session and request method underneath; an extension's
 * module reaching the client the same way; and a refusal, with nothing
 * installed, for the two mistakes that would silently change the API —
 * a module registered twice and a method defined twice.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import type { EnterpriseFeature } from '../../../app/editions';
import { GatewayApiClient } from '../../gatewayApiClient';
import { GatewayApiBase } from '../base';
import {
    BUILTIN_API_MODULES,
    installApiModules,
    installedApiModules,
    type ApiModuleSpec,
} from '../modules';
import { editionUnderTest } from '../../../test/underTest';
import { NOT_SOURCE_DIRS, importedPaths, scanRoots, sourceName } from '../../../test/sourceRoots';

/**
 * The modules of the edition under test (`test/underTest.ts`): the built-in
 * (Community) ones and, where the Enterprise package runs this file again,
 * the Enterprise extension's after them. All 42 are held to the same
 * rules, and the client they are checked on is the one composed of
 * them all.
 */
const EXTENSION_API_MODULES = (await editionUnderTest().registries()).apiModules;
const ALL_MODULES: readonly ApiModuleSpec[] = [...BUILTIN_API_MODULES, ...EXTENSION_API_MODULES];
installApiModules(GatewayApiClient, EXTENSION_API_MODULES);
/**
 * False in the Community package's run: the modules are the Community ones
 * alone there, and every rule below holds them the same way.
 */
const HAS_ENTERPRISE = EXTENSION_API_MODULES.length > 0;

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE_DIR = resolve(HERE, '..');

/** The Enterprise modules and their features. */
const RECORDED_ENTERPRISE: Record<string, EnterpriseFeature> = {
    multiTenant: 'multi_tenant',
    portal: 'portal',
    discovery: 'discovery',
    mcpRegistry: 'mcp_registry',
    deployment: 'deployment',
    trafficImport: 'traffic_import',
    batchJobs: 'batch_jobs',
    policyAsCode: 'policy_as_code',
    promptCache: 'prompt_cache',
    responseCache: 'response_cache',
    knowledgeBase: 'knowledge_base',
};

const methodsOf = (mixin: ApiModuleSpec['mixin']): string[] =>
    Object.getOwnPropertyNames(mixin.prototype as object).filter((n) => n !== 'constructor');

const ownMethods = (proto: object): string[] =>
    Object.getOwnPropertyNames(proto).filter((n) => n !== 'constructor');

class TestBase extends GatewayApiBase {}

describe('the built-in API modules', () => {
    it('have unique names and a known feature or none', () => {
        const names = ALL_MODULES.map((m) => m.name);
        expect(new Set(names).size).toBe(names.length);
        expect(names.length).toBe(HAS_ENTERPRISE ? 40 : 29);
        // The built-in ones are the Community Edition's: the first 29, none of an Enterprise feature.
        expect(ALL_MODULES.slice(0, BUILTIN_API_MODULES.length)).toEqual(BUILTIN_API_MODULES);
        expect(BUILTIN_API_MODULES.length).toBe(29);
        expect(BUILTIN_API_MODULES.filter((m) => m.feature !== null)).toEqual([]);
        // `feature` is typed `EnterpriseFeature | null`, so an unknown name
        // does not compile; what is checked here is the names and the count.
        for (const m of ALL_MODULES) expect(m.name, 'a module has a name').not.toBe('');
    });

    it('are tagged as recorded', () => {
        const tagged = Object.fromEntries(
            ALL_MODULES.filter((m) => m.feature !== null).map((m) => [m.name, m.feature])
        );
        expect(tagged).toEqual(HAS_ENTERPRISE ? RECORDED_ENTERPRISE : {});
    });

    it('each extend the base and nothing else', () => {
        const parents = ALL_MODULES.map((m) => [m.name, Object.getPrototypeOf(m.mixin) === GatewayApiBase]);
        expect(parents.filter(([, ok]) => !ok)).toEqual([]);
    });

    it('declare every method once, and none that the base has', () => {
        const seen = new Map<string, string>();
        const base = new Set(ownMethods(GatewayApiBase.prototype));
        const shadowing: string[] = [];
        const twice: string[] = [];
        for (const m of ALL_MODULES) {
            for (const method of methodsOf(m.mixin)) {
                if (base.has(method)) shadowing.push(`${m.name}.${method}`);
                const other = seen.get(method);
                if (other !== undefined) twice.push(`${method} in ${m.name} and ${other}`);
                seen.set(method, m.name);
            }
        }
        expect(shadowing).toEqual([]);
        expect(twice).toEqual([]);
        // 599 methods in the Community modules; more than 770 with the Enterprise ones.
        expect(seen.size).toBeGreaterThan(HAS_ENTERPRISE ? 700 : 598);
    });

    it('are what the client answers: its methods are the modules’ methods, nothing more or less', () => {
        const fromModules = new Set(ALL_MODULES.flatMap((m) => methodsOf(m.mixin)));
        const onClient = new Set(ownMethods(GatewayApiClient.prototype as object));
        expect([...onClient].sort()).toEqual([...fromModules].sort());
        expect(installedApiModules(GatewayApiClient)).toEqual(ALL_MODULES.map((m) => m.name));
    });

    it('give the client working methods with the base underneath', async () => {
        const seen: string[] = [];
        const client = new GatewayApiClient('http://cp.test', () => undefined);
        client.request = ((endpoint: string) => {
            seen.push(endpoint);
            return Promise.resolve({ tenant_id: 't', tenant_name: 'n', theme: {}, is_custom: false });
        }) as typeof client.request;
        // A method from a Community module. One from an Enterprise module is
        // called beside its module (`enterprise/src/host/lib/gatewayApi/__tests__/modules.test.ts`).
        await client.fetchRoles();
        expect(seen).toEqual(['/v1/admin/users/roles']);
        expect(client).toBeInstanceOf(GatewayApiBase);
        expect(client).toBeInstanceOf(GatewayApiClient);
    });
});

describe('the caller’s own tenant and tenant management', () => {
    const moduleOf = (method: string): string | undefined =>
        ALL_MODULES.find((m) => methodsOf(m.mixin).includes(method))?.name;

    it('reads the console’s own theme at /current, from the Community module, without a tenant id', async () => {
        const seen: string[] = [];
        const client = new GatewayApiClient('http://cp.test', () => undefined);
        client.request = ((endpoint: string) => {
            seen.push(endpoint);
            return Promise.resolve({ tenant_id: 't', company_name: null, variants: {} });
        }) as typeof client.request;
        await client.fetchCurrentTenantThemeVariants();
        expect(seen).toEqual(['/v1/admin/tenants/current/theme-variants']);
        expect(moduleOf('fetchCurrentTenantThemeVariants')).toBe('tenant');
    });

    it('has no request built with a tenant id under /v1/admin/tenants in the Community module', () => {
        const source = readFileSync(resolve(MODULE_DIR, 'tenant.ts'), 'utf8');
        const requested = [...source.matchAll(/[`'"](\/v1\/admin\/tenants[^`'"]*)[`'"]/g)].map((m) => m[1]);
        // (the module's own comment names `/current` too)
        expect(requested).toContain('/v1/admin/tenants/current/theme-variants');
        expect(requested.filter((p) => !/^\/v1\/admin\/tenants\/current(\/[a-z-]+)*$/.test(p))).toEqual([]);
    });
});

describe('installApiModules', () => {
    class ReportsApi extends GatewayApiBase {
        listReports(): string[] {
            return [this.getAuthTokenForTest()];
        }
        getAuthTokenForTest(): string {
            return 'token-of-the-base';
        }
    }
    const reports: ApiModuleSpec = { name: 'reports', feature: null, mixin: ReportsApi };

    it('installs an extension’s methods on the client class, with the base underneath', () => {
        class Client extends TestBase {}
        installApiModules(Client, BUILTIN_API_MODULES);
        installApiModules(Client, [reports]);
        const client = new Client('http://cp.test') as InstanceType<typeof Client> & ReportsApi;
        expect(client.listReports()).toEqual(['token-of-the-base']);
        expect(typeof (client as unknown as GatewayApiClient).fetchRoles).toBe('function');
        expect(installedApiModules(Client)).toEqual([...BUILTIN_API_MODULES.map((m) => m.name), 'reports']);
    });

    it('skips a module already installed as it is, so composing it twice installs it once', () => {
        class Client extends TestBase {}
        installApiModules(Client, [reports]);
        const before = Object.getOwnPropertyDescriptor(Client.prototype, 'listReports');
        installApiModules(Client, [reports]);
        expect(installedApiModules(Client)).toEqual(['reports']);
        expect(Object.getOwnPropertyDescriptor(Client.prototype, 'listReports')).toEqual(before);
    });

    it('refuses a module name already installed, installing nothing', () => {
        class Client extends TestBase {}
        installApiModules(Client, [reports]);
        class OtherApi extends GatewayApiBase {
            other(): number {
                return 1;
            }
        }
        expect(() => installApiModules(Client, [{ name: 'reports', feature: null, mixin: OtherApi }])).toThrow(
            /registered twice: reports/
        );
        expect('other' in Client.prototype).toBe(false);
        expect(installedApiModules(Client)).toEqual(['reports']);
    });

    it('refuses two modules of one name in the same call, installing neither', () => {
        class Client extends TestBase {}
        class OtherApi extends GatewayApiBase {
            other(): number {
                return 1;
            }
        }
        expect(() =>
            installApiModules(Client, [reports, { name: 'reports', feature: null, mixin: OtherApi }])
        ).toThrow(/registered twice: reports/);
        expect(ownMethods(Client.prototype as object)).toEqual([]);
        expect(installedApiModules(Client)).toEqual([]);
    });

    it('refuses an empty module name', () => {
        class Client extends TestBase {}
        expect(() => installApiModules(Client, [{ name: '', feature: null, mixin: ReportsApi }])).toThrow(
            /registered twice: \(empty name\)/
        );
        expect(ownMethods(Client.prototype as object)).toEqual([]);
    });

    it('refuses a method the client already answers — from the base, a module installed before, or the same call', () => {
        class Client extends TestBase {}
        installApiModules(Client, [reports]);
        class ShadowsBase extends GatewayApiBase {
            request<T>(): Promise<T> {
                return Promise.reject(new Error('never'));
            }
        }
        class ShadowsInstalled extends GatewayApiBase {
            listReports(): string[] {
                return [];
            }
        }
        class Fine extends GatewayApiBase {
            fine(): number {
                return 1;
            }
        }
        class ShadowsFine extends GatewayApiBase {
            fine(): number {
                return 2;
            }
        }
        expect(() => installApiModules(Client, [{ name: 'a', feature: null, mixin: ShadowsBase }])).toThrow(
            /module a redefines request/
        );
        expect(() => installApiModules(Client, [{ name: 'b', feature: null, mixin: ShadowsInstalled }])).toThrow(
            /module b redefines listReports/
        );
        expect(() =>
            installApiModules(Client, [
                { name: 'c', feature: null, mixin: Fine },
                { name: 'd', feature: null, mixin: ShadowsFine },
            ])
        ).toThrow(/module d redefines fine/);
        // nothing of the refused calls was installed: not even module c
        expect(ownMethods(Client.prototype as object).sort()).toEqual(['getAuthTokenForTest', 'listReports']);
        expect(installedApiModules(Client)).toEqual(['reports']);
    });
});

describe('the module files', () => {
    // A module is a class that extends the base. Its file is found wherever
    // it lives: in this directory, or anywhere else under the trees of both
    // editions (the Enterprise modules are under `enterprise/`, and
    // under these two rules).
    const FACADE = 'lib/gatewayApiClient';
    const API_DIR = 'lib/gatewayApi/';

    function sources(dir: string): string[] {
        return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
            const full = join(dir, entry.name);
            if (entry.isDirectory()) {
                return entry.name === '__tests__' || entry.name === 'test' || NOT_SOURCE_DIRS.has(entry.name) ? [] : sources(full);
            }
            return /\.tsx?$/.test(entry.name) && !/\.(test|d)\.tsx?$/.test(entry.name) ? [full] : [];
        });
    }

    /** The module files: named as `sourceName` names them (no extension), with the mixin classes each declares. */
    const moduleFiles: Array<{ name: string; file: string; src: string; classes: string[] }> = [];
    for (const file of scanRoots((root) => sources(root))) {
        const name = sourceName(file).replace(/\.tsx?$/, '');
        const src = readFileSync(file, 'utf8');
        const classes = [...src.matchAll(/\bclass\s+([A-Za-z0-9_$]+)\s+extends\s+GatewayApiBase\b/g)].map((m) => m[1]);
        // Every file of this directory is a module file, whatever it declares.
        const here = name.startsWith(API_DIR) && !['base', 'modules'].includes(name.slice(API_DIR.length));
        if (name !== FACADE && (classes.length > 0 || here)) moduleFiles.push({ name, file, src, classes });
    }
    const isModuleFile = (name: string): boolean => moduleFiles.some((m) => m.name === name);

    it('are the registered modules, one file each', () => {
        // This directory holds the built-in (Community) modules and no
        // other: an Enterprise module's file is in its feature's directory
        // under `enterprise/`.
        const files = readdirSync(MODULE_DIR).filter((f) => f.endsWith('.ts') && !['base.ts', 'modules.ts'].includes(f));
        expect(files.map((f) => f.replace(/\.ts$/, '')).sort()).toEqual(BUILTIN_API_MODULES.map((m) => m.name).sort());
        // Wherever it is, a module's file is named after the module.
        expect(moduleFiles.map((m) => m.name.split('/').pop()).sort()).toEqual(ALL_MODULES.map((m) => m.name).sort());
        const misplaced = moduleFiles
            .filter((m) => !m.name.startsWith(API_DIR))
            .filter((m) => !/^enterprise\/src\/features\/[a-z_]+\/lib\/gatewayApi\/[A-Za-z]+$/.test(m.name));
        expect(misplaced.map((m) => m.name)).toEqual([]);
        // …and, over both trees: each file declares one mixin, and the
        // mixins declared are the registered ones.
        expect(moduleFiles.filter((m) => m.classes.length !== 1).map((m) => m.name)).toEqual([]);
        expect(moduleFiles.flatMap((m) => m.classes).sort()).toEqual(ALL_MODULES.map((m) => m.mixin.name).sort());
    });

    it('import from the base only, never from another module or the facade', () => {
        // A module that imported another could not leave with its feature;
        // one that imported the facade would be a cycle.
        expect(moduleFiles.length).toBe(ALL_MODULES.length);
        for (const { name, file, src } of moduleFiles) {
            const local = importedPaths(file, src)
                .map((imported) => sourceName(imported))
                .filter((imported) => imported === FACADE || imported.startsWith(API_DIR) || isModuleFile(imported));
            expect(local, `${name} imports ${local.join(', ')}`).toEqual([`${API_DIR}base`]);
        }
    });
});
