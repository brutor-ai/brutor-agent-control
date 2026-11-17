/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where a sibling repository is: `scripts/sibling-repositories.mjs`, the one
 * place a test or a script of this repository looks for the Control Plane or
 * the gateway core beside it.
 *
 * Beside this repository a sibling is under its repository's name
 * (`../brutor-gateway-control`) or, in the public repository, under its
 * directory there (`../control-plane`); the repository's name is looked at
 * first. Where it is under neither, the cross-component tests are skipped,
 * and what they say names both places.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import { SIBLINGS, missingSibling, siblingPlaces, siblingRepository } from '../../scripts/sibling-repositories.mjs';
import { REPO_ROOT } from '../test/sourceRoots';

const made: string[] = [];

/** A directory holding `admin/` (this repository) and the sibling directories named. */
function layout(...siblings: string[]): string {
    const root = mkdtempSync(path.join(tmpdir(), 'sibling-repositories-'));
    made.push(root);
    for (const dir of ['admin', ...siblings]) mkdirSync(path.join(root, dir));
    return path.join(root, 'admin');
}

afterEach(() => {
    for (const root of made.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe('siblingRepository', () => {
    it('names each sibling by its repository and by its directory in the public repository', () => {
        expect(SIBLINGS['control-plane'].directories).toEqual(['brutor-gateway-control', 'control-plane']);
        expect(SIBLINGS['core-gateway'].directories).toEqual(['brutor-gateway-core', 'core-gateway']);
    });

    it.each([
        ['control-plane', 'brutor-gateway-control'],
        ['core-gateway', 'brutor-gateway-core'],
    ] as const)('finds the %s under its repository name', (name, directory) => {
        const admin = layout(directory);
        const found = siblingRepository(name, admin);
        expect(found.root).toBe(path.resolve(admin, '..', directory));
    });

    it.each([
        ['control-plane', 'control-plane'],
        ['core-gateway', 'core-gateway'],
    ] as const)('finds the %s under its directory in the public repository', (name, directory) => {
        const admin = layout(directory);
        const found = siblingRepository(name, admin);
        expect(found.root).toBe(path.resolve(admin, '..', directory));
    });

    it('looks under the repository name first when both are there', () => {
        const admin = layout('brutor-gateway-control', 'control-plane');
        expect(siblingRepository('control-plane', admin).root).toBe(path.resolve(admin, '..', 'brutor-gateway-control'));
    });

    it('finds nothing under neither name, and says where it looked, both places in order', () => {
        const admin = layout('brutor-gateway-core');
        const found = siblingRepository('control-plane', admin);
        expect(found.root).toBeNull();
        expect(found.places).toEqual([path.resolve(admin, '..', 'brutor-gateway-control'), path.resolve(admin, '..', 'control-plane')]);
        expect(siblingPlaces('control-plane')).toBe('../brutor-gateway-control and at ../control-plane');
        expect(missingSibling('control-plane')).toBe(
            'the Control Plane is not beside this repository: it was looked for at ../brutor-gateway-control and at ../control-plane'
        );
        expect(missingSibling('core-gateway')).toBe(
            'the gateway core is not beside this repository: it was looked for at ../brutor-gateway-core and at ../core-gateway'
        );
    });

    it('a file under a sibling name is not the sibling', () => {
        const admin = layout();
        // A plain file where the repository would be.
        writeFileSync(path.resolve(admin, '..', 'core-gateway'), '');
        expect(siblingRepository('core-gateway', admin).root).toBeNull();
    });

    it('looks beside this repository by default', () => {
        expect(siblingRepository('control-plane').places).toEqual([
            path.resolve(REPO_ROOT, '..', 'brutor-gateway-control'),
            path.resolve(REPO_ROOT, '..', 'control-plane'),
        ]);
    });

    it('refuses a sibling it does not know, by name', () => {
        // @ts-expect-error: not a sibling's name
        expect(() => siblingRepository('portal')).toThrow('No sibling repository is called "portal"');
    });
});
