/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// The third-party licences of a build: the generator of THIRD_PARTY_LICENSES
// and its guard.
//
// Brutor AI's rule for third-party code: packages ship unmodified with their
// licence texts, in a generated file in each image and in the repository that
// lists every dependency with its licence.
//
// What is listed is what the production build ships: the packages whose
// modules Rollup put into a chunk (every module of the build's graph that
// is in a chunk, tree-shaken or not), the bundler's own helpers (Vite's), and
// the packages a stylesheet copies from (`alsoShipped` in the policy:
// Tailwind's base styles). A package in `dependencies` that no module
// imports is not shipped and not listed; a package in `devDependencies` that
// is bundled is. Each package is checked against package-lock.json (it must
// be the locked version), and its licence against the one allowed list,
// `third-party-licenses.json` beside this file. An unknown licence, one that
// is not allowed, a package without a licence file or one that is not the
// locked version fails the build, every problem named at once.
//
//   vite build                   the plugin writes <outDir>/THIRD_PARTY_LICENSES
//   node scripts/third-party-licenses.mjs check <built> <committed>
//                                exit 1 when they differ (the image build and CI)
//   node scripts/third-party-licenses.mjs update <built> <committed>
//                                copy the build's file over the committed one

import { copyFileSync, existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * @typedef {object} Election
 * @property {string} licence The licence the package is taken under.
 * @property {string[]} [files] The package's licence files that state it; all of them when absent.
 * @property {string} why
 */

/**
 * @typedef {object} Policy
 * @property {string[]} allowed SPDX identifiers (and `X WITH Y`) a shipped package may be under.
 * @property {Record<string, Election>} elections Packages under a choice of licences, and the one taken.
 * @property {Record<string, string>} alsoShipped Packages a build ships text of without a module in the graph, and why.
 * @property {Record<string, { file: string, source: string, why: string }>} texts Licence texts a package does not carry, kept beside this file (`file`, relative to the policy's directory), from `source`.
 * @property {Record<string, Vendored>} vendored Copies of a package that another package carries in its own files.
 * @property {Record<string, { file: string, marker: string, source: string }>} fullTexts The full text of a licence
 *   (`file`, relative to the policy's directory), for packages under it whose files carry its notice only: a file
 *   without `marker` is a notice.
 * @property {string} dir The policy's directory.
 */

/**
 * A copy of a package inside another one. When `module` (relative to the
 * lock file's directory) is in the build, the package is listed at the
 * version `versionPattern` finds in that file, with the licence and licence
 * files of the installed package at `licenceFrom`.
 * @typedef {object} Vendored
 * @property {string} module
 * @property {string} versionPattern
 * @property {string} licenceFrom
 * @property {string} why
 */

/**
 * @typedef {object} ShippedPackage
 * @property {string} name
 * @property {string} version
 * @property {string | undefined} declared The licence as the package declares it.
 * @property {string} [licence] The licence it is taken under.
 * @property {string} text Its licence files, as they are.
 */

const POLICY_NAME = 'scripts/third-party-licenses.json';

/** @param {string} file @returns {Policy} */
export function readPolicy(file) {
    const raw = JSON.parse(readFileSync(file, 'utf8'));
    return {
        allowed: raw.allowed,
        elections: raw.elections ?? {},
        alsoShipped: raw.alsoShipped ?? {},
        texts: raw.texts ?? {},
        vendored: raw.vendored ?? {},
        fullTexts: raw.fullTexts ?? {},
        dir: path.dirname(path.resolve(file)),
    };
}

/**
 * Parse an SPDX licence expression into alternatives: a list (OR) of lists
 * (AND) of identifiers (`X WITH Y` is one). Undefined when it is not one.
 * @param {string} expression
 * @returns {string[][] | undefined}
 */
function parseExpression(expression) {
    const tokens = expression.replace(/([()])/g, ' $1 ').trim().split(/\s+/);
    let at = 0;
    /** @returns {string[][] | undefined} */
    function or() {
        let result = and();
        while (result && tokens[at] === 'OR') {
            at += 1;
            const next = and();
            result = next && [...result, ...next];
        }
        return result;
    }
    /** @returns {string[][] | undefined} */
    function and() {
        let result = atom();
        while (result && tokens[at] === 'AND') {
            at += 1;
            const next = atom();
            result = next && result.flatMap((left) => next.map((right) => [...left, ...right]));
        }
        return result;
    }
    /** @returns {string[][] | undefined} */
    function atom() {
        const token = tokens[at];
        if (token === '(') {
            at += 1;
            const inner = or();
            if (tokens[at] !== ')') return undefined;
            at += 1;
            return inner;
        }
        if (token === undefined || !/^[A-Za-z0-9.+-]+$/.test(token) || ['AND', 'OR', 'WITH'].includes(token)) return undefined;
        at += 1;
        if (tokens[at] === 'WITH') {
            const exception = tokens[at + 1];
            if (exception === undefined || !/^[A-Za-z0-9.+-]+$/.test(exception)) return undefined;
            at += 2;
            return [[`${token} WITH ${exception}`]];
        }
        return [[token]];
    }
    const result = or();
    return result && at === tokens.length ? result : undefined;
}

/**
 * The licence a package is taken under, or the problems that keep it out.
 * An OR is taken under the package's election when the policy has one, else
 * under its first alternative that is wholly allowed; an AND needs every
 * part allowed.
 * @param {string} name
 * @param {string | undefined} declared
 * @param {Policy} policy
 * @returns {{ licence: string | undefined, problems: string[] }}
 */
export function resolveLicence(name, declared, policy) {
    if (!declared) return { licence: undefined, problems: [`${name}: no licence declared`] };
    const alternatives = /^(UNLICENSED|SEE LICEN[CS]E IN\b)/i.test(declared) ? undefined : parseExpression(declared);
    if (!alternatives) return { licence: undefined, problems: [`${name}: unknown licence "${declared}"`] };
    const election = policy.elections[name];
    if (election) {
        if (!alternatives.some((terms) => terms.length === 1 && terms[0] === election.licence)) {
            return { licence: undefined, problems: [`${name}: elected ${election.licence}, which "${declared}" does not offer`] };
        }
        if (!policy.allowed.includes(election.licence)) {
            return { licence: undefined, problems: [`${name}: elected ${election.licence}, which is not on the allowed list (${POLICY_NAME})`] };
        }
        return { licence: election.licence, problems: [] };
    }
    const taken = alternatives.find((terms) => terms.every((term) => policy.allowed.includes(term)));
    if (taken) return { licence: taken.join(' AND '), problems: [] };
    return { licence: undefined, problems: [`${name}: ${declared} is not on the allowed list (${POLICY_NAME})`] };
}

/**
 * Resolve every package; the problems of all of them.
 * @param {ShippedPackage[]} packages
 * @param {Policy} policy
 * @returns {{ packages: ShippedPackage[], problems: string[] }}
 */
export function licenceProblems(packages, policy) {
    const problems = [];
    const resolved = [];
    for (const pkg of packages) {
        const { licence, problems: own } = resolveLicence(pkg.name, pkg.declared, policy);
        problems.push(...own);
        if (!pkg.text.trim()) problems.push(`${pkg.name} ${pkg.version}: no licence file in the package`);
        resolved.push({ ...pkg, licence });
    }
    return { packages: resolved, problems };
}

/**
 * The package directory a module id is in (the innermost `node_modules`), or
 * undefined for a module of this repository or a virtual one.
 * @param {string} id
 * @returns {string | undefined}
 */
export function packageDirOf(id) {
    const file = id.replace(/^\0/, '').split('?')[0];
    const match = /^(.*\/node_modules\/(?:@[^/]+\/)?[^/@][^/]*)(?:\/|$)/.exec(file);
    return match?.[1];
}

/** Vite's helpers in a chunk: the preload helper, the module preload polyfill, the CommonJS helpers. */
const isBundlerHelper = (/** @type {string} */ id) => /vite\/(preload-helper|modulepreload-polyfill)|commonjsHelpers/.test(id);

/** A package's licence files: LICENSE, LICENCE, COPYING, NOTICE and third-party notices, at its top. */
const LICENCE_FILE = /^(licen[cs]e|copying|notice|third[-_]?party[-_]?notices)([.\-_].*)?$/i;

/**
 * The text of a package's licence files, unmodified but for line endings.
 * @param {string} dir
 * @param {string[] | undefined} only
 */
function licenceText(dir, only) {
    const files = readdirSync(dir)
        .filter((file) => LICENCE_FILE.test(file) && statSync(path.join(dir, file)).isFile())
        .filter((file) => !only || only.includes(file))
        .sort();
    const texts = files.map((file) => readFileSync(path.join(dir, file), 'utf8').replace(/\r\n?/g, '\n').trimEnd());
    if (texts.length <= 1) return texts[0] ?? '';
    return files.map((file, i) => `--- ${file} ---\n${texts[i]}`).join('\n\n');
}

/** @param {{ license?: unknown, licenses?: unknown }} manifest @returns {string | undefined} */
function declaredLicence(manifest) {
    const { license, licenses } = manifest;
    if (typeof license === 'string') return license;
    if (license && typeof license === 'object' && 'type' in license) return String(license.type);
    if (Array.isArray(licenses)) {
        const types = licenses.map((entry) => (typeof entry === 'string' ? entry : entry?.type)).filter(Boolean);
        return types.length === 1 ? types[0] : types.length ? `(${types.join(' OR ')})` : undefined;
    }
    return undefined;
}

/**
 * The shipped packages of a build, read from its module ids.
 * @param {{ moduleIds: Iterable<string>, lockDir: string, policy: Policy }} options
 * @returns {{ packages: ShippedPackage[], problems: string[] }}
 */
export function collectPackages({ moduleIds, lockDir, policy }) {
    const lock = JSON.parse(readFileSync(path.join(lockDir, 'package-lock.json'), 'utf8'));
    const dirs = new Set();
    const files = new Set();
    for (const id of moduleIds) {
        if (isBundlerHelper(id)) dirs.add(path.join(lockDir, 'node_modules/vite'));
        const dir = packageDirOf(id);
        if (dir) dirs.add(dir);
        files.add(id.replace(/^\0/, '').split('?')[0]);
    }
    for (const name of Object.keys(policy.alsoShipped)) dirs.add(path.join(lockDir, 'node_modules', name));

    const problems = [];
    /** @type {ShippedPackage[]} */
    const packages = [];
    for (const [name, copy] of Object.entries(policy.vendored)) {
        const module = path.join(lockDir, copy.module);
        if (!files.has(module)) continue;
        const version = new RegExp(copy.versionPattern).exec(readFileSync(module, 'utf8'))?.[1];
        const from = path.join(lockDir, copy.licenceFrom);
        if (!version || !existsSync(path.join(from, 'package.json'))) {
            problems.push(`${name}: the copy in ${copy.module} names no version, or ${copy.licenceFrom} is not installed`);
            continue;
        }
        const manifest = JSON.parse(readFileSync(path.join(from, 'package.json'), 'utf8'));
        packages.push({ name, version, declared: declaredLicence(manifest), text: licenceText(from, policy.elections[name]?.files) });
    }
    for (const dir of [...dirs].sort()) {
        const where = path.relative(lockDir, dir).split(path.sep).join('/');
        const manifestFile = path.join(dir, 'package.json');
        if (!existsSync(manifestFile)) {
            problems.push(`${where}: no package.json`);
            continue;
        }
        const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
        if (/^(@brutor\/|brutor-)/.test(manifest.name)) continue;
        const locked = lock.packages?.[where];
        if (!locked || locked.link) {
            problems.push(`${manifest.name}: ${where} is not in package-lock.json`);
            continue;
        }
        if (locked.version !== manifest.version) {
            problems.push(`${manifest.name}: installed ${manifest.version}, package-lock.json has ${locked.version} (run npm ci)`);
            continue;
        }
        const kept = policy.texts[manifest.name];
        const own = licenceText(dir, policy.elections[manifest.name]?.files);
        const added = kept ? readFileSync(path.join(policy.dir, kept.file), 'utf8').replace(/\r\n?/g, '\n').trimEnd() : '';
        packages.push({
            name: manifest.name,
            version: manifest.version,
            declared: declaredLicence(manifest),
            text: [own, added && `--- ${kept?.source} ---\n${added}`].filter(Boolean).join('\n\n'),
        });
    }
    // A package installed twice at one version is one entry.
    const unique = new Map(packages.map((pkg) => [`${pkg.name}@${pkg.version}`, pkg]));
    return { packages: [...unique.values()], problems };
}

const RULE = '='.repeat(78);
const THIN = '-'.repeat(78);

/**
 * The full licence texts of a policy, read.
 * @param {Policy} policy
 * @returns {{ id: string, marker: string, text: string }[]}
 */
export function fullTextsOf(policy) {
    return Object.entries(policy.fullTexts).map(([id, full]) => ({
        id,
        marker: full.marker,
        text: readFileSync(path.join(policy.dir, full.file), 'utf8').replace(/\r\n?/g, '\n').trimEnd(),
    }));
}

/**
 * THIRD_PARTY_LICENSES: the packages, then each distinct licence text once,
 * then the full text of a licence some package carries the notice of only.
 * @param {{ title: string, command: string, packages: ShippedPackage[], fullTexts?: { id: string, marker: string, text: string }[] }} options
 */
export function renderLicences({ title, command, packages, fullTexts = [] }) {
    const byName = (/** @type {ShippedPackage} */ a, /** @type {ShippedPackage} */ b) =>
        a.name < b.name ? -1 : a.name > b.name ? 1 : a.version < b.version ? -1 : a.version > b.version ? 1 : 0;
    const sorted = [...packages].sort(byName);
    const lines = [
        `Third-party licences: ${title}`,
        `Generated by scripts/third-party-licenses.mjs from package-lock.json and the modules of the production build; do not edit. Regenerate: ${command}.`,
        '',
        `Dependencies (${sorted.length})`,
        ...sorted.map((pkg) => {
            const offered = pkg.declared && pkg.declared.replace(/^\((.*)\)$/, '$1') !== pkg.licence ? ` [offered as ${pkg.declared}]` : '';
            return `${pkg.name} ${pkg.version} ${pkg.licence}${offered}`;
        }),
    ];
    /** @type {Map<string, ShippedPackage[]>} */
    const byText = new Map();
    for (const pkg of sorted) byText.set(pkg.text, [...(byText.get(pkg.text) ?? []), pkg]);
    for (const [text, users] of byText) {
        lines.push(
            '',
            RULE,
            [...new Set(users.map((pkg) => pkg.licence))].join(', '),
            `Used by: ${users.map((pkg) => `${pkg.name} ${pkg.version}`).join(', ')}`,
            THIN,
            text,
        );
    }
    for (const full of fullTexts) {
        const noticeOnly = sorted.filter((pkg) => (pkg.licence ?? '').split(' AND ').includes(full.id) && !pkg.text.includes(full.marker));
        if (!noticeOnly.length) continue;
        const names = noticeOnly.map((pkg) => `${pkg.name} ${pkg.version}`).join(', ');
        lines.push('', RULE, `${full.id}: the full text of the licence (${names} carry its notice only)`, `Used by: ${names}`, THIN, full.text);
    }
    return `${lines.join('\n')}\n`;
}

/**
 * The Vite plugin: in a production build, write THIRD_PARTY_LICENSES into
 * the output, or fail the build with every licence problem. `builds` names
 * each build by its root directory (one configuration can serve two
 * builds: the Enterprise console's extends the Community one's).
 * @param {{ lockDir: string, policyFile: string, builds: Record<string, { title: string, command: string }> }} options
 * @returns {import('vite').Plugin}
 */
export function thirdPartyLicenses({ lockDir, policyFile, builds }) {
    /** @type {{ title: string, command: string } | undefined} */
    let build;
    return {
        name: 'brutor:third-party-licenses',
        apply: 'build',
        configResolved(config) {
            build = builds[path.resolve(config.root)];
        },
        generateBundle(_options, bundle) {
            if (!build) {
                this.error(`third-party licences: no build is named for this root (${Object.keys(builds).join(', ')})`);
            }
            const { title, command } = build;
            const policy = readPolicy(policyFile);
            const moduleIds = Object.values(bundle).flatMap((output) => (output.type === 'chunk' ? output.moduleIds : []));
            const collected = collectPackages({ moduleIds, lockDir, policy });
            const resolved = licenceProblems(collected.packages, policy);
            const problems = [...collected.problems, ...resolved.problems];
            if (problems.length) {
                this.error(`third-party licences (${POLICY_NAME}):\n  ${problems.join('\n  ')}`);
            }
            this.emitFile({
                type: 'asset',
                fileName: 'THIRD_PARTY_LICENSES',
                source: renderLicences({ title, command, packages: resolved.packages, fullTexts: fullTextsOf(policy) }),
            });
        },
    };
}

/** The command line: `check` and `update`. */
function main(/** @type {string[]} */ args) {
    const [command, built, committed] = args;
    if (!['check', 'update'].includes(command) || !built || !committed) {
        console.error('usage: third-party-licenses.mjs check|update <built THIRD_PARTY_LICENSES> <committed THIRD_PARTY_LICENSES>');
        return 2;
    }
    if (!existsSync(built)) {
        console.error(`${built} does not exist: build first (vite build writes it)`);
        return 2;
    }
    if (command === 'update') {
        copyFileSync(built, committed);
        return 0;
    }
    if (existsSync(committed) && readFileSync(built, 'utf8') === readFileSync(committed, 'utf8')) return 0;
    console.error(
        `${committed} is not the build's ${built}: the shipped dependencies changed. ` +
            'Regenerate it (the command is in its second line) and commit it.',
    );
    return 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
    process.exitCode = main(process.argv.slice(2));
}
