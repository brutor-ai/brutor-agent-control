/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Finds references to records a reader of this repository cannot open.
 *
 * Design documents, review findings, plan steps and decision records are
 * kept outside the repositories. A comment, a test name or a message that
 * cites one by number tells the reader nothing, so the rule or the reason
 * is written out where it applies and the citation is not. Published
 * standards are different: anyone can look them up, and the ones that may
 * be cited are listed here with their titles.
 *
 * Every text file is read, comments included, as one stream: a line break
 * and the comment marker after it count as one space, so a citation split
 * over two lines is found. Letter case and the punctuation between the word
 * and the number make no difference. The rules and the examples are the
 * same in every repository of the product; a change to one is a change to
 * all of them. The lists hold the shared entries and, beside them, the
 * public names this console shows or reads that the others do not: the
 * timestamp, CSV, transparency-log and SCIM standards (3161, 4180, 6962,
 * 7643, 7644), two GPUs, four percentiles, and two published API versions.
 *
 * `noPrivateReferences.test.ts` runs the scan over the tree.
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { NOT_SOURCE_DIRS, REPO_ROOT } from './sourceRoots';

// ─── The rules ──────────────────────────────────────────────────────────────

/** Published RFCs that may be cited. Any other RFC number is taken for an internal document. */
export const PUBLIC_RFCS: Record<number, string> = {
    1918: 'Address Allocation for Private Internets',
    2119: 'Key words for use in RFCs to Indicate Requirement Levels',
    3161: 'Internet X.509 Public Key Infrastructure Time-Stamp Protocol (TSP)',
    3339: 'Date and Time on the Internet: Timestamps',
    3986: 'Uniform Resource Identifier (URI): Generic Syntax',
    4122: 'A Universally Unique IDentifier (UUID) URN Namespace',
    4180: 'Common Format and MIME Type for Comma-Separated Values (CSV) Files',
    4648: 'The Base16, Base32, and Base64 Data Encodings',
    5280: 'Internet X.509 Public Key Infrastructure Certificate and CRL Profile',
    6238: 'TOTP: Time-Based One-Time Password Algorithm',
    6749: 'The OAuth 2.0 Authorization Framework',
    6750: 'The OAuth 2.0 Authorization Framework: Bearer Token Usage',
    6890: 'Special-Purpose IP Address Registries',
    6962: 'Certificate Transparency',
    7009: 'OAuth 2.0 Token Revocation',
    7515: 'JSON Web Signature (JWS)',
    7517: 'JSON Web Key (JWK)',
    7518: 'JSON Web Algorithms (JWA)',
    7519: 'JSON Web Token (JWT)',
    7591: 'OAuth 2.0 Dynamic Client Registration Protocol',
    7636: 'Proof Key for Code Exchange by OAuth Public Clients',
    7643: 'System for Cross-domain Identity Management: Core Schema',
    7644: 'System for Cross-domain Identity Management: Protocol',
    7662: 'OAuth 2.0 Token Introspection',
    8032: 'Edwards-Curve Digital Signature Algorithm (EdDSA)',
    8259: 'The JavaScript Object Notation (JSON) Data Interchange Format',
    8414: 'OAuth 2.0 Authorization Server Metadata',
    8446: 'The Transport Layer Security (TLS) Protocol Version 1.3',
    8693: 'OAuth 2.0 Token Exchange',
    8707: 'Resource Indicators for OAuth 2.0',
    8785: 'JSON Canonicalization Scheme (JCS)',
    9068: 'JSON Web Token (JWT) Profile for OAuth 2.0 Access Tokens',
    9110: 'HTTP Semantics',
    9207: 'OAuth 2.0 Authorization Server Issuer Identification',
    9457: 'Problem Details for HTTP APIs',
    9562: 'Universally Unique IDentifiers (UUIDs)',
    9728: 'OAuth 2.0 Protected Resource Metadata',
};

/** A capital letter and a number that name a public thing, not a plan step. */
export const PUBLIC_TERMS: Record<string, string> = {
    S3: 'Amazon S3 object storage and its API',
    R2: 'Cloudflare R2 object storage',
    A2: 'the first half of A2A, the Agent2Agent protocol',
    A10: 'NVIDIA A10 GPU',
    A100: 'NVIDIA A100 GPU',
    P50: 'the 50th percentile',
    P90: 'the 90th percentile',
    P95: 'the 95th percentile',
    P99: 'the 99th percentile',
};

/** Dates that are the published version of a public interface. */
export const PUBLIC_DATES: Record<string, string> = {
    '2024-11-05': 'Model Context Protocol specification revision',
    '2025-03-26': 'Model Context Protocol specification revision',
    '2025-06-18': 'Model Context Protocol specification revision',
    '2025-11-25': 'Model Context Protocol specification revision',
    '2026-07-28': 'Model Context Protocol specification revision',
    '2025-07-09': 'MCP Registry API version',
    '2022-11-28': 'GitHub REST API version',
    '2022-06-28': 'Notion API version',
};

/** Paths and names that lead into a private record. */
const PRIVATE_PATHS = [
    'impl/rfc',
    'docs/plans/',
    'rename-list',
    'env-registry.md',
    'impl contracts',
    'implementation contracts',
    'implementation-contracts',
    'engineering-assurance',
    'engineering assurance',
];

/**
 * Repositories of the product that are not published, named in full: a path
 * into one (`brutor-gateway-portal/src/…`) or a pointer to it leads nowhere
 * for a reader of the published tree. Matched as a whole name. The same list
 * as the shared scanners' `PRIVATE_REPOSITORIES` (the cut tooling's list and
 * the licence library).
 */
const PRIVATE_REPOSITORIES = [
    'anthropic-enterprise-mock',
    'cli',
    'discovery-sdk',
    'endpoint-collector',
    'gateway-kb-uploader',
    'gateway-portal',
    'gateway-test',
    'kb-connector-sync',
    'license',
    'mcp-images',
    'mcp-registry',
    'rfcs',
    'saas-infra',
    'scripts',
    'trial-service',
    'trial-signup',
    'user-portal',
].map((name) => `brutor-${name}`);

/** Wording that tells how a rule came about instead of what it is. */
const HISTORY_PHRASES = ["owner's decision", "owner's instruction", "owner's ruling", 'owner decided', '(was:'];

/**
 * A decision cited instead of the rule it made: what the owner decided (`owner decision L1`,
 * `owner ruling`), a numbered answer (`legal answer 5`), the day it was made (`decided on
 * 2026-11-16`) and a numbered decision (`decision 11`, `Decision Q12`). The words the owner spoke
 * in: each followed by an id (a number, or up to six capitals and a number) or not.
 */
const OWNER_WORDS = ['decision', 'ruling', 'instruction', 'answer'];
const DATE_WORDS = ['on', 'in', 'of', 'from'];

/** Prefixes of numbered records kept outside the repositories: [prefix, fewest digits, kind]. */
const TRACKED_PREFIXES: Array<[string, number, string]> = [
    ['D-', 1, 'plan step or decision id'],
    ['E-', 2, 'engineering-assurance id'],
    ['S-', 2, 'engineering-assurance id'],
    ['INV-', 1, 'engineering-assurance id'],
    ['CTL-', 1, 'engineering-assurance id'],
    ['CHG-', 1, 'engineering-assurance id'],
    ['OVR-', 1, 'engineering-assurance id'],
    ['PERF-', 1, 'engineering-assurance id'],
    ['CORE-', 1, 'engineering-assurance id'],
    ['ADMIN-', 1, 'engineering-assurance id'],
    ['PORTAL-', 1, 'engineering-assurance id'],
    ['CONTROL-', 1, 'engineering-assurance id'],
    ['KBCONN-', 1, 'engineering-assurance id'],
];

/** Words that, before `#12`, make it an entry of an internal work list. */
const WORK_ITEMS = ['task', 'regression', 'bug', 'defect', 'ticket'];

const COMMENT_MARKERS = ['//!', '///', '//', '/*', '*/', '<!--', '*', '#', '>', '|'];

export interface PrivateReference {
    line: number;
    kind: string;
    cited: string;
}

/** `word` in any letter case, as a pattern. */
function anyCase(word: string): string {
    return [...word]
        .map((c) => (/[a-z]/i.test(c) ? `[${c.toLowerCase()}${c.toUpperCase()}]` : c.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&')))
        .join('');
}

/** `word` where a word starts: after a non-letter, or as the capitalised part of a camelCase name. */
function wordStart(word: string): string {
    return `(?:(?<![A-Za-z])${anyCase(word)}|(?<=[a-z])${word[0].toUpperCase()}${anyCase(word.slice(1))})`;
}

const ALONE_BEFORE = '(?<![\\w\\-#./])';
const ALONE_AFTER = '(?!\\w)(?![.\\-]\\w)';
const DECISION_ID = '[ \\-#]*(?:\\d+|[A-Z]{1,6}-?\\d+)(?!\\w)';
const DECISION = [
    `${wordStart('owner')}[ \\-]+(?:${OWNER_WORDS.map(anyCase).join('|')})[sS]?(?![A-Za-z])(?:${DECISION_ID})?`,
    `${wordStart('legal')}[ \\-]+${anyCase('answer')}[sS]?[ \\-#]*\\d+(?!\\w)`,
    `(?:${wordStart('decided')}|${wordStart('decision')}[sS]?)[ \\-]+(?:(?:${DATE_WORDS.map(anyCase).join('|')})[ \\-]+)?20\\d\\d-\\d\\d-\\d\\d(?!\\w)`,
    `${wordStart('decision')}[sS]?${DECISION_ID}`,
].join('|');

/** Every reference to a private record in `text`. */
export function privateReferences(text: string): PrivateReference[] {
    // The stream: one space per line break, comment markers dropped.
    let s = '';
    const lineAt: number[] = [];
    text.split('\n').forEach((raw, index) => {
        let line = raw.trim();
        for (let stripped = true; stripped; ) {
            stripped = false;
            for (const marker of COMMENT_MARKERS) {
                if (line.startsWith(marker)) {
                    line = line.slice(marker.length).trimStart();
                    stripped = true;
                    break;
                }
            }
        }
        line = line.replace(/\s+/g, ' ');
        if (!line) return;
        s += `${line} `;
        while (lineAt.length < s.length) lineAt.push(index + 1);
    });

    const found: Array<PrivateReference & { at: number; order: number }> = [];
    let order = 0;
    const add = (at: number, to: number, kind: string): void => {
        found.push({ at, order: order++, line: lineAt[at], kind, cited: s.slice(at, to).trim().replace(/\.+$/, '') });
    };
    const quoted = (at: number, to: number): boolean => /["']/.test(s[at - 1] ?? ' ') && /["']/.test(s[to] ?? ' ');
    const each = (pattern: string, visit: (at: number, to: number, m: RegExpExecArray) => void): void => {
        const re = new RegExp(pattern, 'g');
        for (let m = re.exec(s); m; m = re.exec(s)) visit(m.index, m.index + m[0].length, m);
    };

    // RFC 0021, RFC-0021, rfc0021, rfc 21, RFCs 0010; RFC §3.
    const internalRfc: Array<[number, number]> = [];
    const publicRfcEnds: number[] = [];
    each(`${wordStart('rfc')}[sS]?[ \\-_#]*(?:§[\\d.]*|(\\d+)(?![A-Za-z\\d])( *§[\\d.]*)?)`, (at, to, m) => {
        if (m[1] !== undefined && m[1].length > 5) return;
        if (m[1] !== undefined && PUBLIC_RFCS[Number(m[1])]) {
            publicRfcEnds.push(at + m[0].length - (m[2]?.length ?? 0));
            return;
        }
        internalRfc.push([at, to]);
        add(at, to, 'internal RFC');
    });

    // §4.2: a section of a document that is not in this repository.
    each('§[\\d.]*', (at, to) => {
        if (internalRfc.some(([from, end]) => at >= from && at < end)) return;
        if (publicRfcEnds.some((end) => at >= end && at <= end + 2)) return;
        add(at, to, 'section of an outside document');
    });

    // F245, finding 245.
    each(`${ALONE_BEFORE}F\\d{2,4}${ALONE_AFTER}`, (at, to) => {
        if (!quoted(at, to)) add(at, to, 'finding number');
    });
    each(`${wordStart('finding')}s?[ #]*\\d+`, (at, to) => add(at, to, 'finding number'));

    // ADR 0003, adr-3, Assessment 0001.
    each(`${wordStart('adr')}[ \\-_#]*\\d+`, (at, to) => add(at, to, 'ADR number'));
    each(`${wordStart('assessment')}[ \\-_#]*\\d+`, (at, to) => add(at, to, 'assessment number'));

    // Phase 3, phase-2, sub-phase 1.4, Phase I; Sprint 12, sprint #12.
    each(`${wordStart('phase')}(?:[ \\-]*\\d[\\d.]*|[ \\-]+[IVX]+(?!\\w))`, (at, to) => add(at, to, 'phase of an internal plan'));
    each(`${wordStart('sprint')}[ #]*\\d[\\d.]*`, (at, to) => add(at, to, 'sprint number'));

    // P3-11, R4-3: a step of a plan or a numbered decision. S6, M8, R7, K4, D15, A4.
    each(`${ALONE_BEFORE}[A-Z]\\d{1,2}-\\d{1,2}${ALONE_AFTER}`, (at, to) => {
        if (!quoted(at, to)) add(at, to, 'plan step or decision id');
    });
    each(`${ALONE_BEFORE}[ADKMPRS]\\d{1,2}${ALONE_AFTER}(?![ ,](?:\\d|[\\-.]\\d))`, (at, to) => {
        if (!quoted(at, to) && !PUBLIC_TERMS[s.slice(at, to)]) add(at, to, 'plan step or decision id');
    });

    // D-12; E-03, S-07; INV-12, PORTAL-26, KBCONN-1.
    for (const [prefix, least, kind] of TRACKED_PREFIXES) {
        each(`${ALONE_BEFORE}${prefix}\\d{${least},4}${ALONE_AFTER}`, (at, to) => {
            if (!quoted(at, to)) add(at, to, kind);
        });
    }

    // task #503, regression #14, REGRESSION (#11), (#4).
    each(`(?:(?<=(?<![A-Za-z])(?:${WORK_ITEMS.map(anyCase).join('|')})[ (]*)|(?<=\\()(?=#\\d+\\)))#\\d+`, (at, to) =>
        add(at, to, 'numbered work item'),
    );

    // a193: the nickname of an internal build, on its own or after a word
    // joined to it by a hyphen (pre-a193). After a hyphen that follows
    // anything else (a shade `deep-purple-a400`, a part of an id) it is data.
    each(`(?:${ALONE_BEFORE}|(?<=(?<![\\w\\-#./])[A-Za-z]+-))a\\d{3}${ALONE_AFTER}`, (at, to) => {
        if (!quoted(at, to) && !PUBLIC_TERMS[s.slice(at, to).toUpperCase()]) add(at, to, 'internal build nickname');
    });

    // A path into a private repository; wording that tells history.
    for (const phrase of PRIVATE_PATHS) each(anyCase(phrase), (at, to) => add(at, to, 'private repository or record'));
    for (const name of PRIVATE_REPOSITORIES) each(`(?<![\\w-])${anyCase(name)}(?!\\w)`, (at, to) => add(at, to, 'private repository or record'));
    for (const phrase of HISTORY_PHRASES) each(anyCase(phrase), (at, to) => add(at, to, 'history note'));

    // "owner decision L1", "legal answer 5", "decided on 2026-11-16", "(decision 11)": a decision
    // cited instead of the rule it made. Its date is part of it.
    const decided: Array<[number, number]> = [];
    each(DECISION, (at, to) => {
        decided.push([at, to]);
        add(at, to, 'cited decision');
    });

    // "(2026-10-26)", "policy 2026-10-05", "since 2026-09-06": the day something
    // was decided or happened. A quoted date, a timestamp and a `name: date`
    // or `--flag date` value are data.
    each('(?<!\\w)20\\d\\d-\\d\\d-\\d\\d(?!\\w)(?!-\\d)', (at, to) => {
        if (PUBLIC_DATES[s.slice(at, to)]) return;
        if (decided.some(([from, end]) => at >= from && at < end)) return;
        const before = s[at - 1] ?? ' ';
        const word = s.slice(s.lastIndexOf(' ', at - 2) + 1, Math.max(at - 1, 0));
        if (before === '(' || (before === ' ' && /^[A-Za-z,(]+$/.test(word))) add(at, to, 'history note');
    });

    return found.sort((a, b) => a.at - b.at || a.order - b.order).map(({ line, kind, cited }) => ({ line, kind, cited }));
}

/** What the scanner must find, and what it must leave alone. The same examples in every repository. */
export const SCANNER_EXAMPLES: Array<[string, string[]]> = [
    [
        '// the notice of RFC 0021 §10\n// see RFC-0019 §5.1, rfc 21, RFC0021 and `rfc-0023`',
        ['1: internal RFC: RFC 0021 §10', '2: internal RFC: RFC-0019 §5.1', '2: internal RFC: rfc 21', '2: internal RFC: RFC0021', '2: internal RFC: rfc-0023'],
    ],
    [
        'fn rfc0021_notice() {}\nfn noticeRfc0021() {}\nthe RFC §3.1 layout',
        ['1: internal RFC: rfc0021', '2: internal RFC: Rfc0021', '3: internal RFC: RFC §3.1'],
    ],
    [
        'fixed (F1027), see finding F245 and finding 12; findings are listed',
        ['1: finding number: F1027', '1: finding number: F245', '1: finding number: finding 12'],
    ],
    ['ADR 0003 and adr-2; Assessment 0001', ['1: ADR number: ADR 0003', '1: ADR number: adr-2', '1: assessment number: Assessment 0001']],
    [
        'Phase 3 K4, P3-11, step S6, M8 and R7; sub-phase 1.4',
        [
            '1: phase of an internal plan: Phase 3',
            '1: plan step or decision id: K4',
            '1: plan step or decision id: P3-11',
            '1: plan step or decision id: S6',
            '1: plan step or decision id: M8',
            '1: plan step or decision id: R7',
            '1: phase of an internal plan: phase 1.4',
        ],
    ],
    [
        'D15, D18 §4, D19, R4-3 and D-12',
        [
            '1: plan step or decision id: D15',
            '1: plan step or decision id: D18',
            '1: section of an outside document: §4',
            '1: plan step or decision id: D19',
            '1: plan step or decision id: R4-3',
            '1: plan step or decision id: D-12',
        ],
    ],
    [
        'built in Sprint #663; isolation work E-03; build a193',
        ['1: sprint number: Sprint #663', '1: engineering-assurance id: E-03', '1: internal build nickname: a193'],
    ],
    [
        'PORTAL-26, KBCONN-1 columns, INV-12; task #503, REGRESSION (#11), Phase I (#2)',
        [
            '1: engineering-assurance id: PORTAL-26',
            '1: engineering-assurance id: KBCONN-1',
            '1: engineering-assurance id: INV-12',
            '1: numbered work item: #503',
            '1: numbered work item: #11',
            '1: phase of an internal plan: Phase I',
            '1: numbered work item: #2',
        ],
    ],
    [
        'see brutor-rfcs/impl/rfc-0029/phase-3-plan.md',
        [
            '1: private repository or record: brutor-rfcs',
            '1: private repository or record: impl/rfc',
            '1: internal RFC: rfc-0029',
            '1: phase of an internal plan: phase-3',
        ],
    ],
    // A repository of the product that is not published, by its name or in a path.
    [
        'as the portal does (brutor-gateway-portal/src/i18n); see Brutor-Trial-Service and brutor-kb-connector-sync',
        [
            '1: private repository or record: brutor-gateway-portal',
            '1: private repository or record: Brutor-Trial-Service',
            '1: private repository or record: brutor-kb-connector-sync',
        ],
    ],
    [
        'the Brutor-License crate and brutor-scripts/release.sh; brutor-licensed, brutor-client',
        ['1: private repository or record: Brutor-License', '1: private repository or record: brutor-scripts'],
    ],
    [
        "one run per ref (cost policy 2026-10-05)\nthe owner's decision, 2026-11-13\n(was: OLD_NAME)",
        ['1: history note: 2026-10-05', "2: history note: owner's decision", '2: history note: 2026-11-13', '3: history note: (was:'],
    ],
    // A build nickname after a prefix joined by a hyphen.
    [
        'rows written pre-a009, runs sealed post-a147 and non-a012 builds',
        ['1: internal build nickname: a009', '1: internal build nickname: a147', '1: internal build nickname: a012'],
    ],
    // A citation split over a line break, with the comment marker between.
    ['// the conversation handle of RFC\n// 0021 is a ULID', ['1: internal RFC: RFC 0021']],
    ['    /// closes finding\n    /// F245 for good', ['2: finding number: F245']],
    ['# decided in Phase\n# 3 of the plan', ['1: phase of an internal plan: Phase 3']],
    ['// Widened 2026-09-09 to measure more; found live on\n// 2026-09-26', ['1: history note: 2026-09-09', '2: history note: 2026-09-26']],
    // Public standards, public names and data stay.
    ['a Bearer token (RFC 6750), a JWT (RFC 7519 §4.1), canonical JSON (RFC 8785)', []],
    ['RFC 9207, RFC 9728 and https://www.rfc-editor.org/rfc/rfc8414', []],
    ['DateTime::parse_from_rfc3339(s) and to_rfc3339()', []],
    ['Ed25519 (RFC 8032); private ranges (RFC 1918)', []],
    ['a presigned S3 URL; MiniMax M2.x; BRUTOR-V1-<base64url>', []],
    ['the L3 call runs one script from the L2 manifest', []],
    ['<c r="A1"/> with handle \'H1\' and id "U-1"', []],
    ['color: #F63; background: #a193ff; key 5fa193b2', []],
    ['the shade deep-purple-a400, the id 8f3c-a123-77aa, the file part-a123.txt, x-a123-y and an nvidia-a100 node', []],
    ['"X-GitHub-Api-Version", "2022-11-28"', []],
    ['Issued:  2026-03-14 and --expires 2027-03-14', []],
    ['"updated_at": "2026-05-23T10:00:00Z"', []],
    ['MCP protocol version 2025-06-18', []],
    ['each finding is listed; a sprint of work; one phase at a time', []],
    ['<!-- env-registry:begin (generated from src/env_vars.rs) -->', []],
    ['issue #42 and PR #43 of the repository; `&#169;`; SHA-256, UTF-8, SEP-1024', []],
    ['a two-phase commit; Phase Inversion', []],
    // A decision cited instead of the rule it made.
    [
        '# Owner decision (legal answer 5, 2026-11-16): ship them unmodified\n# (owner decision\n# L1); decided on 2026-11-16, decision of 2026-11-17',
        [
            '1: cited decision: Owner decision',
            '1: cited decision: legal answer 5',
            '2: cited decision: owner decision L1',
            '3: cited decision: decided on 2026-11-16',
            '3: cited decision: decision of 2026-11-17',
        ],
    ],
    [
        'the chunk size (decision 13), Decision Q12 and decisions 5; owner ruling, legal answers #4',
        [
            '1: cited decision: decision 13',
            '1: cited decision: Decision Q12',
            '1: cited decision: decisions 5',
            '1: cited decision: owner ruling',
            '1: cited decision: legal answers #4',
        ],
    ],
    [
        'a decision tree; the caller decides; decisions are logged; decided on the order\nwaits for a decision: 1 to 300 seconds; decision_1 = 0; the owner of a decision; the resource owner decides; a legal answer',
        [],
    ],
    ['<path d="M12 9v2m0 4h.01 M4 6h16 M4,12 S3 -1.5 2"/>', []],
];

// ─── The tree ───────────────────────────────────────────────────────────────

export interface Hit extends PrivateReference {
    file: string;
}

const LOCK_FILES = new Set(['Cargo.lock', 'package-lock.json', 'uv.lock', 'poetry.lock']);
const BINARY = /\.(?:png|jpe?g|gif|ico|webp|woff2?|ttf|otf|eot|pdf|zip|gz)$/i;

/**
 * The text files of the working tree, as paths from `root` with `/`: what
 * git tracks plus what is new and not ignored, or, where `root` is not a
 * checkout (an image build, a copy), every file outside the directories that
 * hold no source. Lock files are left out: they are another tool's output.
 */
export function textFiles(root: string = REPO_ROOT): string[] {
    let files: string[];
    if (existsSync(path.join(root, '.git'))) {
        const out = execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], {
            cwd: root,
            encoding: 'utf8',
            maxBuffer: 64 * 1024 * 1024,
        });
        files = out.split('\0').filter((file) => file !== '' && existsSync(path.join(root, file)));
    } else {
        const walk = (dir: string): string[] =>
            readdirSync(path.join(root, dir), { withFileTypes: true }).flatMap((entry) => {
                const rel = dir === '' ? entry.name : `${dir}/${entry.name}`;
                if (entry.isSymbolicLink()) return [];
                if (entry.isDirectory()) return NOT_SOURCE_DIRS.has(entry.name) || entry.name === '.git' ? [] : walk(rel);
                return [rel];
            });
        files = walk('');
    }
    return files.filter((file) => !BINARY.test(file) && !LOCK_FILES.has(file.slice(file.lastIndexOf('/') + 1))).sort();
}

/**
 * Every reference in one file: in its text, and in its name (line 0), where
 * `/` and `.` count as spaces.
 */
export function scanText(file: string, source: string): Hit[] {
    const inName = privateReferences(file.replace(/[/.]/g, ' ')).map((r) => ({ ...r, line: 0, cited: `${r.cited} (in the file's name)` }));
    return [...inName, ...privateReferences(source)].map((r) => ({ file, ...r }));
}

/** The hits of every file in `files`, read from `root`. A file with a NUL byte is not text and is skipped. */
export function scanFiles(files: readonly string[], root: string = REPO_ROOT): Hit[] {
    return files.flatMap((file) => {
        const bytes = readFileSync(path.join(root, file));
        return bytes.includes(0) ? [] : scanText(file, bytes.toString('utf8'));
    });
}

/** The package a file belongs to: everything under `enterprise/` is the Enterprise package's. */
export function packageOf(file: string): 'community' | 'enterprise' {
    return file.startsWith('enterprise/') ? 'enterprise' : 'community';
}

/** `file:line: kind: cited text`, one per hit, in file order. */
export function formatHits(hits: readonly Hit[]): string[] {
    return hits.map((hit) => `${hit.file}:${hit.line}: ${hit.kind}: ${hit.cited}`);
}

/** Hits per kind, most first. */
export function countByKind(hits: readonly Hit[]): Array<[string, number]> {
    const counts = new Map<string, number>();
    for (const hit of hits) counts.set(hit.kind, (counts.get(hit.kind) ?? 0) + 1);
    return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
