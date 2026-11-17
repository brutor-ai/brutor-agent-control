/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Every permission string the console checks is one the Control Plane
 * registers.
 *
 * The console shows an action or a sidebar entry to an administrator who
 * holds a permission (`hasPermission(...)`, `requiresPermission: ...`). A
 * string the Control Plane does not register is in nobody's session, so
 * what it guards is shown to nobody but an administrator who holds every
 * permission: a misspelt permission hides a feature without an error.
 *
 * `scripts/control-plane-permissions.json` lists what the Control Plane
 * registers, per edition, and names what it was read from: a commit
 * (`node scripts/gen-control-plane-permissions.mjs <commit>` writes it), or
 * the two permission modules in the directory of documents
 * (`BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=<dir> node scripts/gen-control-plane-permissions.mjs --documents-dir`).
 * Either way it records the SHA-256 of each module's source.
 * A Community file may check a Community permission only: a Community
 * Control Plane registers no other. An Enterprise file may check either.
 *
 * The Community package holds this to the Community tree. The Enterprise
 * package runs it again over both.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { PERMISSIONS_FILE, SOURCES, atCommit, enumValues, permissionsDocument, sourceHash } from '../../scripts/gen-control-plane-permissions.mjs';
import { siblingForTest, skipped } from '../../scripts/sibling-repositories.mjs';
import { toolEnv } from '../../scripts/tool-env.mjs';
import { NOT_SOURCE_DIRS, REPO_ROOT, enterpriseUnderTest, isEnterprisePath, scanRoots } from '../test/sourceRoots';

interface Registered {
    /** The commit the modules were read at; null when they were read from a directory of documents. */
    control_plane_commit: string | null;
    /** The SHA-256 of each module's source text. */
    source_sha256: { community: string; enterprise: string };
    community: string[];
    enterprise: string[];
}

const registered = JSON.parse(readFileSync(path.join(REPO_ROOT, PERMISSIONS_FILE), 'utf8')) as Registered;

const isTest = (file: string): boolean => /\.(?:test|spec)\.tsx?$|[\\/]__tests__[\\/]|[\\/]src[\\/]test[\\/]|[\\/]generated[\\/]/.test(file);

function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return NOT_SOURCE_DIRS.has(entry.name) ? [] : sourceFiles(full);
        return /\.tsx?$/.test(entry.name) && !entry.name.endsWith('.d.ts') && !isTest(full) ? [full] : [];
    });
}

/** The text without its comments: what a comment says of a permission is not a check. */
const code = (text: string): string => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');

/**
 * What is passed on, not checked: the sidebar hands an entry's own
 * `requiresPermission` to `hasPermission`, and the types say `string`.
 */
const PASSED_ON: ReadonlySet<string> = new Set(['item.requiresPermission', 'child.requiresPermission', 'leaf.requiresPermission', 'permission', 'string']);

interface Check {
    file: string;
    /** As written: a string literal, or the name of a constant. */
    written: string;
    /** The permission strings it stands for; empty when it could not be read. */
    values: string[];
}

/** Every permission check of the trees under test. */
function checks(): Check[] {
    const files = scanRoots((root) => sourceFiles(root));
    const texts = new Map(files.map((file) => [file, code(readFileSync(file, 'utf8'))]));
    // The string constants of the trees, by name: a check may name one.
    const constants = new Map<string, Set<string>>();
    for (const text of texts.values()) {
        for (const match of text.matchAll(/\bconst\s+([A-Z][A-Z0-9_]*)\s*(?::\s*string\s*)?=\s*(['"])([^'"]*)\2/g)) {
            constants.set(match[1], (constants.get(match[1]) ?? new Set()).add(match[3]));
        }
    }
    const found: Check[] = [];
    for (const [file, text] of texts) {
        const written = [
            ...[...text.matchAll(/\bhasPermission\(\s*([^)]*?)\s*\)/g)].map((match) => match[1]),
            ...[...text.matchAll(/\brequiresPermission\??\s*:\s*([^,;}\n]+)/g)].map((match) => match[1].trim()),
        ];
        for (const argument of written) {
            if (PASSED_ON.has(argument)) continue;
            const literal = /^(['"])([^'"]*)\1$/.exec(argument);
            const values = literal ? [literal[2]] : [...(constants.get(argument) ?? [])];
            found.push({ file: path.relative(REPO_ROOT, file).split(path.sep).join('/'), written: argument, values });
        }
    }
    return found;
}

describe('the permission strings the console checks', () => {
    const all = checks();

    it('are found: in the sidebar, on the screens and through named constants', () => {
        expect(all.length).toBeGreaterThanOrEqual(20);
        const written = all.map((check) => check.written);
        expect(written).toContain("'system:read'");
        expect(written).toContain('ADMIN_USER_UPDATE');
        expect(written).toContain('TOOL_APPROVAL_MANAGE');
        // The Enterprise package's run reads the Enterprise tree too.
        expect(all.some((check) => check.file.startsWith('enterprise/'))).toBe(enterpriseUnderTest());
    });

    it('can each be read: a string, or a constant that holds one', () => {
        expect(all.filter((check) => check.values.length === 0).map((check) => `${check.file}: ${check.written}`)).toEqual([]);
    });

    it('are all registered by the Control Plane of the file\'s edition', () => {
        const community = new Set(registered.community);
        const either = new Set([...registered.community, ...registered.enterprise]);
        const unknown = all.flatMap((check) => {
            const allowed = isEnterprisePath(path.join(REPO_ROOT, check.file)) ? either : community;
            return check.values.filter((value) => !allowed.has(value)).map((value) => `${check.file}: ${value}`);
        });
        expect(
            unknown,
            `The Control Plane registers no such permission (${PERMISSIONS_FILE}), so nobody holds it and what it guards is never shown.`,
        ).toEqual([]);
    });

    it('unlocking a tenant administrator asks for the permission that manages administrators', async () => {
        const { ADMIN_USER_UPDATE } = await import('../lib/accountLock');
        expect(ADMIN_USER_UPDATE).toBe('admin:user:update');
        expect(registered.community).toContain(ADMIN_USER_UPDATE);
    });
});

describe('the list of what the Control Plane registers', () => {
    it('names what it was read from, and holds both editions\' permissions once each', () => {
        if (registered.control_plane_commit !== null) expect(registered.control_plane_commit).toMatch(/^[0-9a-f]{40}$/);
        expect(registered.source_sha256.community).toMatch(/^[0-9a-f]{64}$/);
        expect(registered.source_sha256.enterprise).toMatch(/^[0-9a-f]{64}$/);
        expect(registered.community.length).toBeGreaterThan(100);
        expect(registered.enterprise.length).toBeGreaterThan(10);
        const every = [...registered.community, ...registered.enterprise];
        expect(new Set(every).size).toBe(every.length);
        for (const value of every) expect(value).toMatch(/^[a-z0-9_]+(?::[a-z0-9_]+)+$/);
    });

    it('is built from the two modules\' source, and records the hash of each', () => {
        const community = 'class Permission(str, Enum):\n    A_READ = "a:read"\n';
        const enterprise = 'class EnterprisePermission(str, Enum):\n    B_READ = "b:read"\n';
        const fromCommit = permissionsDocument({ community, enterprise }, 'a'.repeat(40));
        expect(fromCommit.control_plane_commit).toBe('a'.repeat(40));
        expect(fromCommit.community).toEqual(['a:read']);
        expect(fromCommit.enterprise).toEqual(['b:read']);
        expect(fromCommit.source_sha256).toEqual({ community: sourceHash(community), enterprise: sourceHash(enterprise) });
        // Read from a directory of documents: no commit to name.
        const fromDirectory = permissionsDocument({ community, enterprise }, null);
        expect(fromDirectory.control_plane_commit).toBeNull();
        expect(fromDirectory.source_sha256).toEqual(fromCommit.source_sha256);
        expect(sourceHash('x')).toBe('2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881');
    });

    it('is read from a Python enum by its members, to the end of the class', () => {
        const source = [
            'import enum',
            'class Other(enum.Enum):',
            '    X = "other:x"',
            'class Permission(enum.Enum):',
            '    """Doc."""',
            '    # A comment',
            '    A_READ = "a:read"',
            "    A_WRITE = 'a:write'  # why",
            '',
            '    B_READ = "b:read"',
            'DESCRIPTIONS = {',
            '    "not": "a:member",',
            '}',
        ].join('\n');
        expect(enumValues(source, 'Permission')).toEqual(['a:read', 'a:write', 'b:read']);
        expect(() => enumValues(source, 'Missing')).toThrow('no class Missing');
    });

    // Beside the Control Plane repository (scripts/sibling-repositories.mjs):
    // a list read at a commit is what that commit holds. Read with `git show`,
    // never from its working tree. Elsewhere (this repository alone), or where
    // the Control Plane's history does not hold that commit, there is nothing
    // to compare with.
    const atThatCommit = 'the permission list is what the Control Plane holds at the commit it was read at';
    const commit = registered.control_plane_commit;
    const controlPlane = commit === null ? null : siblingForTest('control-plane', atThatCommit);
    const show = (file: string): string | null => {
        if (commit === null || controlPlane === null) return null;
        try {
            return execFileSync('git', ['-C', controlPlane, 'show', atCommit(commit, file)], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 16 * 1024 * 1024 });
        } catch {
            return null;
        }
    };
    const beside = show(SOURCES.community.file) !== null;
    if (controlPlane !== null && !beside) {
        skipped(atThatCommit, `the Control Plane at ${controlPlane} has no ${SOURCES.community.file} at ${commit ?? 'no commit'}`);
    }

    it.skipIf(!beside)('is what the Control Plane holds at that commit', () => {
        const community = show(SOURCES.community.file) ?? '';
        const enterprise = show(SOURCES.enterprise.file) ?? '';
        expect(registered.community).toEqual(enumValues(community, SOURCES.community.enum));
        expect(registered.enterprise).toEqual(enumValues(enterprise, SOURCES.enterprise.enum));
        expect(registered.source_sha256).toEqual({ community: sourceHash(community), enterprise: sourceHash(enterprise) });
    });

    // A list read from a directory of documents is what the two modules in
    // the directory named now hold, at their Control Plane paths.
    const documentsDir = toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR');
    const inDirectory = (file: string): string | null => {
        const full = documentsDir ? path.join(documentsDir, file) : null;
        return full !== null && existsSync(full) ? readFileSync(full, 'utf8') : null;
    };
    const fromDirectory = commit === null && inDirectory(SOURCES.community.file) !== null && inDirectory(SOURCES.enterprise.file) !== null;

    it.skipIf(!fromDirectory)('is what the permission modules in the directory of documents hold', () => {
        const community = inDirectory(SOURCES.community.file) ?? '';
        const enterprise = inDirectory(SOURCES.enterprise.file) ?? '';
        expect(registered.community).toEqual(enumValues(community, SOURCES.community.enum));
        expect(registered.enterprise).toEqual(enumValues(enterprise, SOURCES.enterprise.enum));
        expect(registered.source_sha256).toEqual({ community: sourceHash(community), enterprise: sourceHash(enterprise) });
    });
});
