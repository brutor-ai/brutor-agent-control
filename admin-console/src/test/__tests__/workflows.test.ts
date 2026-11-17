/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * `findWorkflows` reads the console's own workflows or the public
 * repository's root ones, and fails, never skips, when neither is there.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';

import { bakeBuilds, bakeRegistryIsGhcrOnce, findWorkflows, publishedFromComponent } from '../workflows';

const base = mkdtempSync(path.join(os.tmpdir(), 'workflows-layouts-'));
const write = (file: string): void => {
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, 'name: x\n');
};

afterAll(() => rmSync(base, { recursive: true, force: true }));

describe('the workflows', () => {
    it('are those of its own repository when it has them', () => {
        const own = path.join(base, 'own');
        write(path.join(own, '.github/workflows/docker-build.yml'));
        write(path.join(own, '.github/workflows/ci.yml'));
        const found = findWorkflows(own);
        expect(found.layout).toBe('own');
        expect(found.all.map((file) => path.basename(file))).toEqual(['ci.yml', 'docker-build.yml']);
        expect(found.image).toBe(found.publish);
        expect(path.basename(found.image)).toBe('docker-build.yml');
        expect(found.mine).toEqual(found.all);
    });

    it('are the root workflows of the public repository in its component directory', () => {
        const mono = path.join(base, 'mono');
        for (const name of ['admin-console.yml', 'publish-images.yml', 'core-gateway.yml']) write(path.join(mono, '.github/workflows', name));
        write(path.join(mono, 'package/docker-bake.hcl'));
        mkdirSync(path.join(mono, 'admin-console'));
        const found = findWorkflows(path.join(mono, 'admin-console'));
        expect(found.layout).toBe('monorepo');
        expect(found.all).toHaveLength(3);
        expect(path.basename(found.image)).toBe('admin-console.yml');
        // The published names are in the bake file, not in a workflow.
        expect(path.relative(mono, found.publish)).toBe(path.join('package', 'docker-bake.hcl'));
        expect(found.mine).toEqual([found.image, found.publish]);
        expect(found.component).toBe('admin-console');
    });

    it('are a failure when neither is there', () => {
        const bare = path.join(base, 'bare', 'admin-console');
        mkdirSync(bare, { recursive: true });
        expect(() => findWorkflows(bare)).toThrow(/no workflows/);
    });

    it('name the published image from its bake target', () => {
        const block = (context: string, name: string): string =>
            `target "admin-console" {\n  inherits = ["community"]\n  context = "${context}"\n  tags = tags("${name}")\n}\n`;
        expect(publishedFromComponent(block('admin-console', 'brutor-ai/x'), 'brutor-ai/x', 'admin-console')).toBe(1);
        expect(publishedFromComponent(block('admin-console', 'brutor-ai/y'), 'brutor-ai/x', 'admin-console')).toBe(0);
        expect(publishedFromComponent(block('core-gateway', 'brutor-ai/x'), 'brutor-ai/x', 'admin-console')).toBe(0);
        expect(bakeRegistryIsGhcrOnce('variable "REGISTRY" {\n  default = "ghcr.io"\n}\n')).toBe(true);
        expect(bakeRegistryIsGhcrOnce('variable "REGISTRY" {\n  default = "docker.io"\n}\n')).toBe(false);
        expect(bakeBuilds('run: docker buildx bake -f package/docker-bake.hcl admin-console --load --set x', 'admin-console')).toBe(1);
        expect(bakeBuilds('run: docker buildx bake -f package/docker-bake.hcl core-gateway --load', 'admin-console')).toBe(0);
    });
});
