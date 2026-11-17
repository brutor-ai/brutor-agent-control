/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Each image of the console says what it is, and is published under the
 * name the product gives it.
 *
 * The names, titles, licences and source repositories are one list,
 * `scripts/image-names.txt`. Each Dockerfile carries the labels of its
 * edition from that list; the image workflow writes the names out (a name
 * derived from the repository the workflow runs in would change with the
 * repository), builds each image from its own Dockerfile, and gives every
 * image it publishes the two labels only the build knows: the version and
 * the revision.
 *
 * The Community package checks its own Dockerfile. The Enterprise package
 * runs this file again (it reads the edition under test) and checks
 * `enterprise/Dockerfile` as well, so the Community run reads nothing under
 * `enterprise/`.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { REPO_ROOT, enterpriseUnderTest } from '../test/sourceRoots';
import { BAKE, bakeBuilds, bakeRegistryIsGhcrOnce, findWorkflows, publishedFromComponent } from '../test/workflows';

const LIST = 'scripts/image-names.txt';
/** The workflows, in this repository or at the root of the public one (`../test/workflows.ts`). */
const WORKFLOWS = findWorkflows();
const VENDOR = 'Brutor AI Ltd';
const ORGANISATION = 'https://github.com/brutor-ai/';
const VERSION_LABELS = ['org.opencontainers.image.version', 'org.opencontainers.image.revision'];

const read = (file: string): string => readFileSync(path.join(REPO_ROOT, file), 'utf8');

interface Image {
    edition: string;
    name: string;
    title: string;
    licenses: string;
    source: string;
    dockerfile: string;
}

function images(text: string = read(LIST)): Image[] {
    return text
        .split('\n')
        .filter((line) => line.trim() !== '' && !line.startsWith('#'))
        .map((line) => {
            const c = line.split('\t');
            if (c.length !== 6) throw new Error(`${LIST}: six tab-separated columns: ${JSON.stringify(line)}`);
            return { edition: c[0], name: c[1], title: c[2], licenses: c[3], source: c[4], dockerfile: c[5] };
        });
}

/** The variable of the image workflow that holds an edition's image name. */
const variableOf = (edition: string): string => (edition === 'community' ? 'COMMUNITY_IMAGE_NAME' : 'IMAGE_NAME');

/** The LABELs of a Dockerfile's last stage: the image that is shipped. One quoted value per line. */
function labels(dockerfile: string): Map<string, string> {
    const lastFrom = dockerfile.lastIndexOf('\nFROM ');
    const stage = lastFrom < 0 ? dockerfile : dockerfile.slice(lastFrom);
    const out = new Map<string, string>();
    for (const line of stage.split('\n')) {
        const m = /^\s*LABEL\s+(\S+?)=(.*)$/.exec(line);
        if (!m) continue;
        const value = m[2].trim();
        if (!/^".*"$/.test(value)) throw new Error(`one quoted value per LABEL line: ${line}`);
        if (out.has(m[1])) throw new Error(`the label ${m[1]} is set twice`);
        out.set(m[1], value.slice(1, -1));
    }
    return out;
}

function labelFaults(image: Image, have: Map<string, string>): string[] {
    const faults: string[] = [];
    const want: Array<[string, string]> = [
        ['org.opencontainers.image.title', image.title],
        ['org.opencontainers.image.licenses', image.licenses],
        ['org.opencontainers.image.source', image.source],
        ['org.opencontainers.image.vendor', VENDOR],
        ['ai.brutor.edition', image.edition],
    ];
    for (const [key, value] of want) {
        const got = have.get(key);
        if (got === undefined) faults.push(`${key} is not set (it is ${JSON.stringify(value)})`);
        else if (got !== value) faults.push(`${key} is ${JSON.stringify(got)}, not ${JSON.stringify(value)}`);
    }
    if (!have.get('org.opencontainers.image.description')?.includes('Brutor Agent Control Admin Console')) {
        faults.push('org.opencontainers.image.description names the product and the component');
    }
    for (const [key, value] of have) {
        if (['maintainer', 'version', 'description', 'org.opencontainers.image.authors', ...VERSION_LABELS].includes(key)) {
            faults.push(`the label ${key} (${JSON.stringify(value)}) is not set in a Dockerfile`);
        }
        if (value.includes('github.com/') && !value.startsWith(ORGANISATION)) {
            faults.push(`${key} names a GitHub namespace that is not the organisation's: ${JSON.stringify(value)}`);
        }
    }
    return faults;
}

/** Lines of a workflow, outside comments, that take a name from the repository the workflow runs in. */
function derivedNames(workflow: string): string[] {
    return workflow
        .split('\n')
        .map((line, index) => ({ line, number: index + 1 }))
        .filter(({ line }) => !line.trimStart().startsWith('#'))
        .filter(({ line }) => /github\.repository|GITHUB_REPOSITORY/.test(line.replace(/\s+/g, '')))
        .map(({ line, number }) => `${number}: ${line.trim()}`);
}

/** The jobs of a workflow, by name: the text of each, comments left out. */
function jobs(workflow: string): Map<string, string> {
    const body = workflow.slice(workflow.indexOf('\njobs:\n'));
    const out = new Map<string, string>();
    const parts = body.split(/\n {2}([\w-]+):\n/);
    for (let at = 1; at + 1 < parts.length; at += 2) {
        out.set(parts[at], parts[at + 1].split('\n').filter((line) => !line.trimStart().startsWith('#')).join('\n'));
    }
    return out;
}

/** How a workflow names an image: a fault, or null when it names it as the list does. */
type Naming = (workflow: string, image: Image) => string | null;

const repositoryOf = (image: Image): string => image.name.replace(/^ghcr\.io\//, '');

/** The workflow sets the edition's variable to the name, once. */
const byVariable: Naming = (workflow, image) => {
    const variable = variableOf(image.edition);
    const repository = repositoryOf(image);
    return workflow.split(`\n  ${variable}: ${repository}\n`).length === 2 ? null : `${variable} is ${repository}, set once (${LIST})`;
};

/**
 * What the workflows of the found layout get wrong against the list. In the
 * console's own repository one workflow names, builds and publishes both
 * images. In the public repository the bake file (`BAKE`) is the one place
 * the Community image is named, tagged and given the version and revision
 * labels; the component's workflow builds the image through its bake
 * target and names no published image; and nothing names the Enterprise
 * image.
 */
function layoutFaults(image: string, publish: string, list: readonly Image[]): string[] {
    if (WORKFLOWS.layout === 'own') return workflowFaults(image, list);
    const community = list.filter((entry) => entry.edition === 'community');
    const enterprise = list.filter((entry) => entry.edition !== 'community').map(repositoryOf);
    const imageFile = path.basename(WORKFLOWS.image);
    const faults: string[] = [];
    if (bakeBuilds(image, WORKFLOWS.component) !== 1) faults.push(`${imageFile}: builds its image through ${BAKE} (${WORKFLOWS.component}), once`);
    if (image.includes('brutor-ai/brutor-agent-control-')) faults.push(`${imageFile}: names a published image; the names are in ${BAKE}`);
    if (image.includes('steps.meta.outputs.labels')) faults.push(`${imageFile}: passes the metadata action's labels, which replace the Dockerfile's`);
    if (!bakeRegistryIsGhcrOnce(publish)) faults.push(`${BAKE}: REGISTRY is ghcr.io, set once`);
    for (const entry of community) {
        const repository = repositoryOf(entry);
        if (publishedFromComponent(publish, repository, WORKFLOWS.component) !== 1) {
            faults.push(`${BAKE}: the bake target publishes ${repository} from ${WORKFLOWS.component}/, once (${LIST})`);
        }
    }
    const base = /^target "community" \{\n([\s\S]*?)^\}/m.exec(publish)?.[1] ?? '';
    for (const label of VERSION_LABELS) {
        if (!base.includes(`"${label}" = `)) faults.push(`${BAKE}: publishes an image without ${label}`);
    }
    for (const [file, text] of [...WORKFLOWS.all.map((f) => [f, readFileSync(f, 'utf8')]), [WORKFLOWS.publish, publish]]) {
        for (const name of enterprise.filter((n) => text.includes(n))) faults.push(`${path.basename(file)} names the Enterprise image ${name}`);
    }
    return faults;
}

/**
 * What is wrong with the image workflow against the list: an image name not
 * written out as the list has it, a job that builds one edition's Dockerfile
 * under another edition's name, a published image without its version or
 * revision, the metadata action's labels (they replace the Dockerfile's).
 */
function workflowFaults(workflow: string, list: readonly Image[]): string[] {
    const faults: string[] = [];
    if (workflow.split('\n  REGISTRY: ghcr.io\n').length !== 2) faults.push('REGISTRY is ghcr.io, set once');
    for (const image of list) {
        const fault = byVariable(workflow, image);
        if (fault) faults.push(fault);
    }
    if (workflow.includes('steps.meta.outputs.labels')) faults.push("passes the metadata action's labels, which replace the Dockerfile's");
    const byDockerfile = new Map(list.map((image) => [image.dockerfile, image]));
    let built = 0;
    for (const [name, text] of jobs(workflow)) {
        const dockerfiles = [...text.matchAll(/(?:\bfile:|\s-f)\s+\.\/(\S+)/g)].map((m) => m[1]);
        if (dockerfiles.length === 0) continue;
        built += 1;
        const variables = new Set([...text.matchAll(/\b(COMMUNITY_IMAGE_NAME|IMAGE_NAME)\b/g)].map((m) => m[1]));
        for (const dockerfile of dockerfiles) {
            const image = byDockerfile.get(dockerfile);
            if (!image) {
                faults.push(`job ${name} builds ${dockerfile}, which ${LIST} does not list`);
                continue;
            }
            const others = [...variables].filter((variable) => variable !== variableOf(image.edition));
            if (others.length > 0) faults.push(`job ${name} builds ${dockerfile} (the ${image.edition} image) and names it ${others.join(', ')}`);
        }
        const publishes = /\bpush:\s*\$\{\{|\bpush:\s*true|buildx build[^\n]*--push|build --push/.test(text);
        if (publishes) {
            for (const label of VERSION_LABELS) {
                if (!text.includes(`${label}=`)) faults.push(`job ${name} publishes an image without ${label}`);
            }
        }
    }
    if (built === 0) faults.push('no job builds an image');
    return faults;
}

describe('the images of the console', () => {
    const list = images();

    it('are the two editions of the Admin Console, under the names the product gives them', () => {
        expect(list.map((image) => image.edition)).toEqual(['community', 'enterprise']);
        for (const image of list) {
            const community = image.edition === 'community';
            expect(image).toEqual({
                edition: image.edition,
                name: `ghcr.io/brutor-ai/brutor-agent-control-${community ? '' : 'enterprise-'}admin-console`,
                title: `Brutor Agent Control Admin Console (${community ? 'Community' : 'Enterprise'} Edition)`,
                licenses: community ? 'Apache-2.0' : 'LicenseRef-Brutor-Proprietary',
                source: `${ORGANISATION}brutor-agent-control${community ? '' : '-enterprise'}`,
                dockerfile: community ? 'Dockerfile' : 'enterprise/Dockerfile',
            });
        }
    });

    it('each Dockerfile of the edition under test carries the labels of its edition', () => {
        const checked = list.filter((image) => image.edition === 'community' || enterpriseUnderTest());
        for (const image of checked) {
            expect({ dockerfile: image.dockerfile, faults: labelFaults(image, labels(read(image.dockerfile))) }).toEqual({
                dockerfile: image.dockerfile,
                faults: [],
            });
        }
        expect(checked.length).toBe(enterpriseUnderTest() ? 2 : 1);
    });

    it('no workflow derives a name from the repository it runs in', () => {
        expect(WORKFLOWS.all.length).toBeGreaterThanOrEqual(2);
        for (const file of WORKFLOWS.all) {
            expect({ file, derived: derivedNames(readFileSync(file, 'utf8')) }).toEqual({ file, derived: [] });
        }
    });

    it('the image workflow names, builds and labels each image as the list says', () => {
        expect(layoutFaults(readFileSync(WORKFLOWS.image, 'utf8'), readFileSync(WORKFLOWS.publish, 'utf8'), list)).toEqual([]);
    });
});

describe('the image checks', () => {
    const community: Image = {
        edition: 'community',
        name: 'ghcr.io/brutor-ai/brutor-agent-control-admin-console',
        title: 'Brutor Agent Control Admin Console (Community Edition)',
        licenses: 'Apache-2.0',
        source: 'https://github.com/brutor-ai/brutor-agent-control',
        dockerfile: 'Dockerfile',
    };
    const good = [
        'FROM node AS builder',
        'LABEL stage="builder"',
        'FROM nginx',
        'LABEL org.opencontainers.image.title="Brutor Agent Control Admin Console (Community Edition)"',
        'LABEL org.opencontainers.image.description="Brutor Agent Control Admin Console, Community Edition"',
        'LABEL org.opencontainers.image.licenses="Apache-2.0"',
        'LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"',
        'LABEL org.opencontainers.image.vendor="Brutor AI Ltd"',
        'LABEL ai.brutor.edition="community"',
        '',
    ].join('\n');

    it('accept the labels of the list, and read only the shipped stage', () => {
        expect(labelFaults(community, labels(good))).toEqual([]);
        expect(labels(good).has('stage')).toBe(false);
    });

    it.each([
        [good.replace('"Apache-2.0"', '"LicenseRef-Brutor-Proprietary"'), 'licenses'],
        [good.replace('LABEL org.opencontainers.image.licenses="Apache-2.0"\n', ''), 'licenses'],
        [good.replace('brutor-ai/brutor-agent-control"', 'someone/admin-console"'), 'source'],
        [good.replace('(Community Edition)', '(Enterprise Edition)'), 'title'],
        [good.replace('Brutor AI Ltd', 'Brutor'), 'vendor'],
        [good.replace('="community"', '="enterprise"'), 'ai.brutor.edition'],
        [`${good}LABEL maintainer="A Person"\n`, 'maintainer'],
        [`${good}LABEL org.opencontainers.image.version="0.11.0"\n`, 'org.opencontainers.image.version'],
        [`${good}LABEL org.opencontainers.image.revision="abc123"\n`, 'org.opencontainers.image.revision'],
        [good.replace(/LABEL org\.opencontainers\.image\.description=.*\n/, 'LABEL description="A React app"\n'), 'description'],
    ])('catch a Dockerfile whose %#th label is wrong (%s)', (dockerfile, about) => {
        expect(labelFaults(community, labels(dockerfile)).some((fault) => fault.includes(about))).toBe(true);
    });

    it('catch a name taken from the repository, in any spelling', () => {
        for (const line of [
            '  IMAGE_NAME: ${{ github.repository }}',
            '          images: ghcr.io/${{github.repository}}',
            '          docker build -t "ghcr.io/$GITHUB_REPOSITORY:test" .',
        ]) {
            expect({ line, derived: derivedNames(line).length }).toEqual({ line, derived: 1 });
        }
        for (const line of ['  IMAGE_NAME: brutor-ai/brutor-agent-control-admin-console', '  # never ${{ github.repository }}']) {
            expect({ line, derived: derivedNames(line) }).toEqual({ line, derived: [] });
        }
    });

    it('catch a workflow that names, builds or labels an image other than as the list says', () => {
        const list = images();
        const image = readFileSync(WORKFLOWS.image, 'utf8');
        const publish = readFileSync(WORKFLOWS.publish, 'utf8');
        expect(layoutFaults(image, publish, list)).toEqual([]);
        const metadata = (text: string): string => text.replace(/labels: \|\n(\s+org\..*\n)+/, 'labels: ${{ steps.meta.outputs.labels }}\n');
        // [image workflow, publish workflow, the fault expected]. In the console's own repository they are one file.
        const planted: Array<[string, string, string]> =
            WORKFLOWS.layout === 'own'
                ? [
                      [image.replace('\n  IMAGE_NAME: brutor-ai/', '\n  IMAGE_NAME: someone/'), publish, 'IMAGE_NAME is'],
                      [image.replace('\n  COMMUNITY_IMAGE_NAME: brutor-ai/brutor-agent-control-admin-console\n', '\n'), publish, 'COMMUNITY_IMAGE_NAME is'],
                      [image.replace('file: ./enterprise/Dockerfile', 'file: ./Dockerfile'), publish, 'the community image) and names it IMAGE_NAME'],
                      [image.replace(/\n {12}org\.opencontainers\.image\.revision=.*/, ''), publish, 'without org.opencontainers.image.revision'],
                      [image.replace(/--label "org\.opencontainers\.image\.version=[^"]*" \\\n\s*/, ''), publish, 'without org.opencontainers.image.version'],
                      [metadata(image), publish, "the metadata action's labels"],
                  ]
                : [
                      [image.replace(`${BAKE} admin-console --load`, `${BAKE} core-gateway --load`), publish, 'builds its image through'],
                      [`${image}  COMMUNITY_IMAGE_NAME: brutor-ai/brutor-agent-control-admin-console\n`, publish, 'names a published image'],
                      [image, publish.replace('tags("brutor-ai/brutor-agent-control-admin-console")', 'tags("someone/admin-console")'), 'the bake target publishes'],
                      [image, publish.replace('context = "admin-console"', 'context = "elsewhere"'), 'the bake target publishes'],
                      [image, publish.replace('default = "ghcr.io"', 'default = "docker.io"'), 'REGISTRY is ghcr.io'],
                      [image, publish.replace(/\n\s*"org\.opencontainers\.image\.revision" = .*/, ''), 'without org.opencontainers.image.revision'],
                      [image, publish.replace(/\n\s*"org\.opencontainers\.image\.version" = .*/, ''), 'without org.opencontainers.image.version'],
                      [image, `${publish}# ghcr.io/brutor-ai/brutor-agent-control-enterprise-admin-console\n`, 'names the Enterprise image'],
                      [`${image}          labels: \${{ steps.meta.outputs.labels }}\n`, publish, "the metadata action's labels"],
                  ];
        for (const [imageText, publishText, about] of planted) {
            expect(imageText === image && publishText === publish ? `${about}: the plant changed nothing` : '').toBe('');
            const faults = layoutFaults(imageText, publishText, list);
            expect(faults.some((fault) => fault.includes(about)), `${about}: ${faults.join('; ')}`).toBe(true);
        }
    });

    it('read the list as six tab-separated columns', () => {
        expect(() => images('community ghcr.io/x title Apache-2.0 https://x Dockerfile\n')).toThrow(/six tab-separated columns/);
    });
});
