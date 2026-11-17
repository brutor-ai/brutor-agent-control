/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where the console's CI workflows are: ONE function, `findWorkflows`, for
 * every test that reads a workflow.
 *
 * The workflows live in one of two places, and a test never spells either:
 *
 * - **its own repository**: `.github/workflows/` under the repository root,
 *   where the image workflow is `docker-build.yml`;
 * - **one directory of the public repository**: this tree is a component
 *   directory (`admin-console/`) and GitHub runs only the repository root's
 *   workflows, so they are `../.github/workflows/`: the component's own
 *   `admin-console.yml` (it builds and starts the image). The published
 *   image names are written once, in `../package/docker-bake.hcl` (the
 *   root's `publish-images.yml` publishes every image through it, by
 *   hand): in this layout that file is what `publish` is.
 *
 * Neither there is a failure, never a skip: a guard that reads no workflow
 * would pass without reading one.
 */

import { existsSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

import { REPO_ROOT } from './sourceRoots';

/** The public repository's image build, relative to its root: the one place the published image names are written. */
export const BAKE = 'package/docker-bake.hcl';

/** This tree's directory in the public repository, which names its root workflow. */
export const COMPONENT = 'admin-console';

/** The image workflow of the console's own repository. */
export const OWN_IMAGE = 'docker-build.yml';

export interface Workflows {
    /** `own`: `.github/workflows/` of this repository; `monorepo`: `../.github/workflows/`. */
    layout: 'own' | 'monorepo';
    /** Every workflow of the repository that runs them, sorted, as absolute paths. */
    all: string[];
    /** The workflow that names and builds the image: the own `docker-build.yml`, or the root's `<component>.yml`. */
    image: string;
    /** What names the published image: the own `docker-build.yml`, or the public repository's bake file (`BAKE`). */
    publish: string;
    /** This component's files: all of `all` in its own repository, the image workflow and the bake file in the public one. */
    mine: string[];
    /** The component's directory in the public repository. */
    component: string;
}

const isFile = (file: string): boolean => existsSync(file) && statSync(file).isFile();
const isDir = (dir: string): boolean => existsSync(dir) && statSync(dir).isDirectory();

/** The workflows of the tree at `root`. Throws when neither layout is there. */
export function findWorkflows(root: string = REPO_ROOT, component: string = COMPONENT, ownImage: string = OWN_IMAGE): Workflows {
    const own = path.join(root, '.github', 'workflows');
    const shared = path.join(path.dirname(path.resolve(root)), '.github', 'workflows');
    let layout: Workflows['layout'];
    let dir: string;
    if (isDir(own)) {
        layout = 'own';
        dir = own;
    } else if (isFile(path.join(shared, `${component}.yml`))) {
        layout = 'monorepo';
        dir = shared;
    } else {
        throw new Error(
            `no workflows: neither ${own} nor ../.github/workflows/${component}.yml beside ${root} is there, ` +
                'and a guard that reads no workflow would pass without reading one',
        );
    }
    const image = path.join(dir, layout === 'own' ? ownImage : `${component}.yml`);
    const publish = layout === 'own' ? image : path.join(path.dirname(path.resolve(root)), BAKE);
    for (const file of [image, publish]) {
        if (!isFile(file)) throw new Error(`the ${layout} layout has no ${file}`);
    }
    const all = readdirSync(dir)
        .filter((name) => /\.ya?ml$/.test(name))
        .sort()
        .map((name) => path.join(dir, name));
    return { layout, all, image, publish, mine: layout === 'own' ? all : [image, publish], component };
}

/**
 * How many times the bake file publishes `repository` built from `component`'s directory: the target block
 * `target "<component>" {`, `inherits = ["community"]`, `context = "<component>"`, `tags = tags("<repository>")`.
 */
export function publishedFromComponent(bake: string, repository: string, component: string): number {
    return bake.split(`target "${component}" {\n  inherits = ["community"]\n  context = "${component}"\n  tags = tags("${repository}")\n`).length - 1;
}

/** The bake file sets the registry to `ghcr.io`, once. */
export function bakeRegistryIsGhcrOnce(bake: string): boolean {
    return bake.split('variable "REGISTRY" {\n  default = "ghcr.io"\n}\n').length === 2;
}

/** How many times a component's workflow builds its image through the bake file's target for it. */
export function bakeBuilds(workflow: string, component: string): number {
    return workflow.split(`docker buildx bake -f ${BAKE} ${component} --load`).length - 1;
}
