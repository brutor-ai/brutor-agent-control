/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Admin Console may not be framed by another page, and the browser is
 * told so where it listens: in the headers of every response.
 *
 * `X-Frame-Options` and the `frame-ancestors` directive of a
 * Content-Security-Policy work as response headers only. In a `<meta>` tag
 * the browser ignores both.
 *
 * nginx sends them, and an nginx `add_header` is inherited from the level
 * above only by a block that has no `add_header` of its own. The blocks that
 * serve `index.html`, the scripts and the health check each set a header of
 * their own, so each must carry the security headers too. This test reads
 * nginx.conf, resolves which block serves a path the way nginx does, and
 * checks that block. The Enterprise User Portal has the same test.
 *
 * The Community package checks its own page, image and build context. The
 * Enterprise package runs this file again (it reads the edition under test)
 * and checks the Enterprise ones as well, so the Community run reads nothing
 * under `enterprise/` and passes with that tree deleted.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { enterpriseUnderTest } from '../test/sourceRoots';

const ROOT = path.resolve(__dirname, '..', '..');
const read = (file: string) => readFileSync(path.join(ROOT, file), 'utf8');

/** The edition under test: the Community package's own, and the Enterprise package's beside it in its run. */
const enterprise = enterpriseUnderTest();
/** The page each edition under test builds. */
const PAGES = ['index.html', ...(enterprise ? ['enterprise/index.html'] : [])];
/** The image each edition under test builds; each copies the nginx configuration. */
const DOCKERFILES = ['Dockerfile', ...(enterprise ? ['enterprise/Dockerfile'] : [])];
/** The ignore file of each image's build context. */
const DOCKERIGNORES = ['.dockerignore', ...(enterprise ? ['enterprise/Dockerfile.dockerignore'] : [])];

/** The policy the console needs (Monaco evaluates code and runs blob workers), as before framing was added to it. */
const CONSOLE_POLICY = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:",
    "worker-src 'self' blob:",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: http://localhost:* https:",
    "font-src 'self' data:",
    "connect-src 'self' ws: wss: http://localhost:* https://*",
];

const directives = (value: string) =>
    value
        .split(';')
        .map((directive) => directive.trim())
        .filter(Boolean);

/** The headers every response must carry, and what each must say. */
const REQUIRED: Record<string, (value: string) => boolean> = {
    'X-Frame-Options': (value) => value === 'DENY',
    'Content-Security-Policy': (value) =>
        directives(value).includes("frame-ancestors 'none'") &&
        CONSOLE_POLICY.every((directive) => directives(value).includes(directive)) &&
        directives(value).length === CONSOLE_POLICY.length + 1,
    'X-Content-Type-Options': (value) => value === 'nosniff',
    'Referrer-Policy': (value) => value === 'strict-origin-when-cross-origin',
    // The browser's own filter is switched off: where it still exists it can
    // be turned against the page it filters.
    'X-XSS-Protection': (value) => value === '0',
};

/**
 * nginx.conf with every `include` of a file of this repository replaced by
 * the file, as nginx reads it. The container path of each file is the one
 * the Dockerfiles copy it to; `mime.types` comes with the image.
 */
function copiedFiles(dockerfile: string): Map<string, string> {
    const copies = [...read(dockerfile).matchAll(/^COPY\s+(?!--from)(\S+)\s+(\/etc\/nginx\/\S+)\s*$/gm)];
    return new Map(copies.map((m) => [m[2], m[1]]));
}

const copied = copiedFiles('Dockerfile');
const nginxSource = read('nginx.conf');
const includedFiles = [...nginxSource.matchAll(/^\s*include\s+(\S+);/gm)].map((m) => m[1]);
const nginx = nginxSource.replace(/^\s*include\s+(\S+);/gm, (line, target: string) => {
    if (target === '/etc/nginx/mime.types') return line;
    const source = copied.get(target);
    if (source === undefined) throw new Error(`nginx.conf includes ${target}, which the Dockerfile does not copy`);
    return read(source);
});

interface Header {
    name: string;
    value: string;
    always: boolean;
}

/** The `add_header` directives written directly in a block, not in a block inside it. */
function ownHeaders(body: string): Header[] {
    let depth = 0;
    let own = '';
    for (const char of body) {
        if (char === '{') depth += 1;
        else if (char === '}') depth -= 1;
        else if (depth === 0) own += char;
    }
    return [...own.matchAll(/^\s*add_header\s+(\S+)\s+("[^"]*"|\S+?)(\s+always)?\s*;/gm)].map((m) => ({
        name: m[1],
        value: m[2].replace(/^"|"$/g, ''),
        always: m[3] !== undefined,
    }));
}

/** The text between the brace that opens at `open` and the one that closes it. */
function blockBody(text: string, open: number): string {
    let depth = 0;
    for (let at = open; at < text.length; at += 1) {
        if (text[at] === '{') depth += 1;
        else if (text[at] === '}') {
            depth -= 1;
            if (depth === 0) return text.slice(open + 1, at);
        }
    }
    throw new Error('a block of nginx.conf is not closed');
}

const serverOpen = /^\s*server\s*\{/m.exec(nginx);
const server = serverOpen ? blockBody(nginx, serverOpen.index + serverOpen[0].length - 1) : '';
const locations = [...server.matchAll(/^\s*location\s+([^{]+?)\s*\{/gm)].map((m) => ({
    name: m[1],
    body: blockBody(server, m.index + m[0].length - 1),
    headers: ownHeaders(blockBody(server, m.index + m[0].length - 1)),
}));
type Location = (typeof locations)[number];

/**
 * The location nginx serves a path from: an exact match; else the longest
 * matching prefix when it is marked `^~`; else the first regular-expression
 * location in the order of the file that matches; else the longest matching
 * prefix.
 */
function locationFor(requestPath: string): Location | undefined {
    const exact = locations.find((location) => location.name === `= ${requestPath}`);
    if (exact) return exact;
    const prefixOf = (name: string) => (/^[~=]/.test(name) ? null : name.replace(/^\^~\s+/, ''));
    const longestPrefix = locations
        .filter((location) => {
            const prefix = prefixOf(location.name);
            return prefix !== null && requestPath.startsWith(prefix);
        })
        .sort((a, b) => (prefixOf(b.name) ?? '').length - (prefixOf(a.name) ?? '').length)[0];
    if (longestPrefix?.name.startsWith('^~')) return longestPrefix;
    for (const location of locations) {
        const regex = /^(~\*?)\s+(.+)$/.exec(location.name);
        if (regex && new RegExp(regex[2], regex[1] === '~*' ? 'i' : '').test(requestPath)) return location;
    }
    return longestPrefix;
}

/**
 * The block whose headers answer a path. `try_files` ending in a URI is an
 * internal redirect when no earlier argument names a file, and the headers
 * then come from the block that serves that URI. A path with no dot in its
 * last segment is taken to be an address of the application, not a file.
 */
function answeringLocation(requestPath: string): Location | undefined {
    const location = locationFor(requestPath);
    const fallback = /^\s*try_files\s+[^;]*\s(\/\S+);/m.exec(location?.body ?? '');
    const looksLikeFile = /\.[^/]+$/.test(requestPath);
    if (fallback && !looksLikeFile && fallback[1] !== requestPath) return locationFor(fallback[1]);
    return location;
}

/** The headers a response for the path carries: the block's own, or the server's when the block has none. */
function headersFor(requestPath: string): Header[] {
    const location = answeringLocation(requestPath);
    return location && location.headers.length > 0 ? location.headers : serverHeaders;
}

const serverHeaders = ownHeaders(server);

const PATHS = [
    '/',
    '/index.html',
    '/some/spa/route',
    '/assets/index-abc.js',
    '/assets/x.worker.js',
    '/assets/style.css',
    '/assets/logo-abc.png',
    '/favicon.svg',
    '/env-config.js',
    '/health',
];

describe('the page', () => {
    it.each(PAGES)('%s does not set X-Frame-Options or frame-ancestors in a <meta> tag', (file) => {
        const page = read(file);
        expect(page).not.toMatch(/http-equiv=["']X-Frame-Options["']/i);
        expect(page).not.toMatch(/<meta[^>]*frame-ancestors/i);
    });

    it.each(PAGES)('%s leaves every security header to the response, which is its single authority', (file) => {
        // A policy in a <meta> tag is enforced next to the one in the header,
        // so a second, different copy would block what the header allows.
        const httpEquiv = [...read(file).matchAll(/http-equiv=["']([^"']+)["']/gi)].map((m) => m[1].toLowerCase());
        expect(httpEquiv.filter((name) => name !== 'x-ua-compatible')).toEqual([]);
    });
});

describe('the Vite servers', () => {
    // `npm run dev` and `vite preview` serve the page without nginx; they
    // send the same headers, read from the same file.
    const viteSource = read('vite.config.ts');

    it('read the headers from the file nginx includes', () => {
        expect(viteSource).toMatch(/nginx-security-headers\.conf/);
    });

    it.each(['server', 'preview'] as const)('%s sends every security header nginx sends', async (mode) => {
        const { default: config, securityHeaders } = await import('../../vite.config');
        expect(securityHeaders).toEqual(Object.fromEntries(serverHeaders.map((header) => [header.name, header.value])));
        expect(config[mode]?.headers).toEqual(securityHeaders);
    });
});

describe('the headers nginx sends', () => {
    it('has the blocks this test reads', () => {
        expect(serverOpen).not.toBeNull();
        expect(locations.map((location) => location.name)).toContain('~* \\.html$');
        expect(locations.map((location) => location.name)).toContain('/');
        expect(locations.map((location) => location.name)).toContain('/health');
    });

    it('sets every security header once, with `always`, for the server', () => {
        for (const [name, accepts] of Object.entries(REQUIRED)) {
            const sent = serverHeaders.filter((header) => header.name === name);
            expect(sent, `${name} is sent once`).toHaveLength(1);
            expect(accepts(sent[0].value), `${name}: ${sent[0].value}`).toBe(true);
            expect(sent[0].always, `${name} is sent on every status`).toBe(true);
        }
    });

    it.each(PATHS)('sends every security header, with `always`, for %s', (requestPath) => {
        const headers = headersFor(requestPath);
        for (const [name, accepts] of Object.entries(REQUIRED)) {
            const sent = headers.filter((header) => header.name === name);
            expect(sent, `${requestPath}: ${name} is sent once`).toHaveLength(1);
            expect(accepts(sent[0].value), `${requestPath}: ${name}: ${sent[0].value}`).toBe(true);
            expect(sent[0].always, `${requestPath}: ${name} is sent on every status`).toBe(true);
        }
    });

    it('serves an address of the application as index.html, from the block for .html', () => {
        for (const requestPath of ['/', '/some/spa/route']) {
            expect(answeringLocation(requestPath)?.name).toBe('~* \\.html$');
        }
    });

    it('repeats them in every block that sets a header of its own', () => {
        for (const location of locations.filter((entry) => entry.headers.length > 0)) {
            expect(location.headers, `location ${location.name}`).toEqual(expect.arrayContaining(serverHeaders));
        }
    });

    it('sends each header once in a block', () => {
        for (const location of [{ name: 'server', headers: serverHeaders }, ...locations]) {
            const names = location.headers.map((header) => header.name);
            expect(new Set(names).size, `location ${location.name}: ${names.join(', ')}`).toBe(names.length);
        }
    });

    it('never sets Content-Type with add_header', () => {
        // `add_header` adds a second Content-Type next to the one nginx
        // derives from the file or from `default_type`.
        const set = [{ name: 'server', headers: serverHeaders }, ...locations].filter((location) =>
            location.headers.some((header) => header.name.toLowerCase() === 'content-type'),
        );
        expect(set.map((location) => location.name)).toEqual([]);
    });

    it('does not name its version', () => {
        expect(nginx).toMatch(/^\s*server_tokens\s+off;/m);
    });

    it('sends no header from an `if` block', () => {
        // A further level of `add_header` inheritance this test does not read.
        expect(server).not.toMatch(/^\s*if\s*\(/m);
    });
});

describe('a file nginx.conf includes', () => {
    it('is copied by every image of the edition under test to the same place', () => {
        const own = includedFiles.filter((file) => file !== '/etc/nginx/mime.types');
        for (const dockerfile of DOCKERFILES) {
            const copies = copiedFiles(dockerfile);
            expect(copies.get('/etc/nginx/nginx.conf'), `${dockerfile} copies nginx.conf`).toBe('nginx.conf');
            for (const file of own) {
                expect(copies.get(file), `${dockerfile} copies ${file}`).toBe(copied.get(file));
            }
        }
    });

    it('is not left out of any build context of the edition under test', () => {
        const ignored = DOCKERIGNORES.flatMap((file) =>
            read(file)
                .split('\n')
                .map((line) => line.trim())
                .filter((line) => line && !line.startsWith('#')),
        );
        for (const source of copied.values()) {
            const excluding = ignored.filter(
                (pattern) => pattern === source || (pattern.startsWith('*') && source.endsWith(pattern.slice(1))),
            );
            expect({ source, excluding }).toEqual({ source, excluding: [] });
        }
    });
});

describe('the health check', () => {
    const health = locations.find((location) => location.name === '/health');

    it('answers at /health and sets its type with default_type', () => {
        expect(locationFor('/health')).toBe(health);
        expect(health?.body).toMatch(/^\s*default_type\s+text\/plain;/m);
    });
});
