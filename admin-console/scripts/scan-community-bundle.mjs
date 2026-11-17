#!/usr/bin/env node
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Scan a Community build of the Admin Console for what belongs to the
 * Enterprise Edition.
 *
 *     node scripts/scan-community-bundle.mjs [dist]
 *
 * The Community build is not obfuscated, so its text can be read: a control
 * plane route under an Enterprise prefix, or the id of an Enterprise
 * navigation entry, in any file of the build is a piece of an Enterprise
 * feature that was bundled; so is an Enterprise route under a prefix both
 * editions have routes under. What to look for is in
 * `community-bundle-denylist.json` beside this file.
 *
 * - A route prefix is found where it is followed by anything but a
 *   character that continues the path segment: `/v1/admin/policies` is
 *   found in `/v1/admin/policies/x` and in `'/v1/admin/policies'`, not in
 *   `/v1/admin/policies-archive`.
 * - Some Enterprise routes are under a prefix the Community console also has
 *   routes under (`/v1/admin/users`: the system administrator's routes are
 *   `/v1/admin/users/system/...`). Those are listed by their own beginning
 *   (`sharedPrefixRoutes`) and found the same way.
 * - A navigation id is found as a whole string literal: `"kb-collections"`,
 *   not `effective-kb-collections`. A few of the ids are ordinary words the
 *   Community console also has as strings of its own (`sharedNavIds`, each
 *   with the reason and the label of the Enterprise entry). Those are found
 *   as the Enterprise navigation entry, `id:"policies",label:"Policy as
 *   Code"`, and as a tab id a screen claims, `ids:["policies"]`.
 *
 * Exits 0 with nothing found, 1 with findings (each printed with its file
 * and what surrounds it), 2 when there is no build to scan.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TEXT_FILE = /\.(js|mjs|cjs|css|html|json|map|txt|svg)$/;

export function readDenylist(file = path.join(HERE, 'community-bundle-denylist.json')) {
    return JSON.parse(readFileSync(file, 'utf8'));
}

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * What of `denylist` is in `text`. Each finding: `{ kind, what, index }`.
 * `sharedNavIds` maps an id to `{ why, label }`.
 */
export function scanText(text, denylist) {
    const findings = [];
    for (const prefix of [...denylist.routePrefixes, ...(denylist.sharedPrefixRoutes ?? [])]) {
        const pattern = new RegExp(`${escapeRegExp(prefix)}(?![A-Za-z0-9_-])`, 'g');
        for (const match of text.matchAll(pattern)) findings.push({ kind: 'route', what: prefix, index: match.index });
    }
    const shared = denylist.sharedNavIds ?? {};
    for (const id of denylist.navIds) {
        const literal = `(["'\`])${escapeRegExp(id)}\\1`;
        if (id in shared) {
            const label = `(["'\`])${escapeRegExp(shared[id].label)}\\2`;
            const entry = new RegExp(`\\bid\\s*:\\s*${literal}\\s*,\\s*label\\s*:\\s*${label}`, 'g');
            const claimed = new RegExp(`\\bids\\s*:\\s*\\[[^\\]]*${literal}`, 'g');
            for (const pattern of [entry, claimed]) {
                for (const match of text.matchAll(pattern)) findings.push({ kind: 'nav', what: id, index: match.index });
            }
        } else {
            for (const match of text.matchAll(new RegExp(literal, 'g'))) findings.push({ kind: 'nav', what: id, index: match.index });
        }
    }
    return findings.sort((a, b) => a.index - b.index);
}

function filesUnder(dir) {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        return entry.isDirectory() ? filesUnder(full) : [full];
    });
}

/** Scan every text file under `dist`. Each finding gains `file` and `around`. */
export function scanBuild(dist, denylist = readDenylist()) {
    const findings = [];
    let scanned = 0;
    for (const file of filesUnder(dist)) {
        if (!TEXT_FILE.test(file)) continue;
        const text = readFileSync(file, 'utf8');
        scanned += 1;
        for (const finding of scanText(text, denylist)) {
            findings.push({
                ...finding,
                file: path.relative(dist, file),
                around: text.slice(Math.max(0, finding.index - 60), finding.index + finding.what.length + 60).replace(/\s+/g, ' '),
            });
        }
    }
    return { scanned, findings };
}

/** The JavaScript of a build that the obfuscator has been over cannot be read: the scan would find nothing in it. */
export function looksObfuscated(dist) {
    return filesUnder(dist)
        .filter((file) => file.endsWith('.js'))
        .some((file) => /\b_0x[0-9a-f]{4,}\b/.test(readFileSync(file, 'utf8').slice(0, 200_000)));
}

function main() {
    const dist = path.resolve(process.argv[2] ?? 'dist');
    if (!existsSync(dist) || !statSync(dist).isDirectory()) {
        console.error(`no build to scan: ${dist} (run the Community build first: npm run build)`);
        process.exit(2);
    }
    if (looksObfuscated(dist)) {
        console.error(`${dist} is an obfuscated build: its text cannot be scanned. The Community build is not obfuscated.`);
        process.exit(1);
    }
    const denylist = readDenylist();
    const { scanned, findings } = scanBuild(dist, denylist);
    if (scanned === 0) {
        console.error(`no text file under ${dist}`);
        process.exit(2);
    }
    for (const finding of findings) {
        console.error(`${finding.kind === 'route' ? 'Enterprise route prefix' : 'Enterprise navigation id'} ${finding.what} in ${finding.file}: …${finding.around}…`);
    }
    console.log(
        `${scanned} files scanned for ${denylist.routePrefixes.length} route prefixes, ${(denylist.sharedPrefixRoutes ?? []).length} Enterprise routes under shared prefixes and ${denylist.navIds.length} navigation ids: ${findings.length} found`,
    );
    process.exit(findings.length === 0 ? 0 : 1);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
