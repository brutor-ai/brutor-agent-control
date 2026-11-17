/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Admin Console table style is a single source of truth.
 *
 * `tableStyles.ts` opens with "single source of truth" and says of the
 * header typography and colour tokens that they "are not negotiable". A
 * file that does not import it lets adjacent tabs disagree on header
 * typography and divider strategy — including the surfaces an operator lives
 * in: Proxy Logs, Mission Control, the Audit viewers, the system panes.
 *
 * These two lists are RATCHETS, in the same spirit as the ESLint
 * `--max-warnings` cap: they record the exceptions, and they may only ever
 * shrink. Both are empty, so any file that hand-rolls the style fails
 * immediately.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { NOT_SOURCE_DIRS, scanRoots } from '../../../test/sourceRoots'

const ROOT = resolve(__dirname, '../../../..')

/** Every source file of both editions: one table look across the console. */
const allSourceFiles = (): string[] => scanRoots((root) => sourceFiles(root))

/** The header typography `TABLE_TH_TYPE` exists to own. */
const HEADER_TYPOGRAPHY = 'text-xs font-medium text-muted uppercase tracking-wider'

/**
 * Files that spell the header typography out by hand instead of using
 * `TABLE_TH` / `TABLE_TH_TYPE`. MAY ONLY SHRINK.
 */
const HAND_ROLLED_TYPOGRAPHY: readonly string[] = [
    // Empty: every file uses the shared style. Do not add to it.
]

/**
 * `.tsx` files with a `<table` that do not import `tableStyles`.
 * MAY ONLY SHRINK.
 */
const TABLES_NOT_USING_STYLES: readonly string[] = [
    // Empty: every table uses the shared style. Do not add to it.
]

/**
 * Genuinely exempt, with the reason. Not a ratchet — these will never adopt.
 *
 * `printableDocuments.ts` and `statementHtml.ts` build standalone HTML
 * documents as strings for print and export. They have no `className` and no
 * Tailwind; their tables carry their own inline styles because they are read
 * outside the console entirely.
 */
const EXEMPT = new Set<string>([
    'src/lib/printableDocuments.ts',
    'src/lib/statementHtml.ts',
])

function sourceFiles(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry)
        if (statSync(full).isDirectory()) {
            if (entry === '__tests__' || NOT_SOURCE_DIRS.has(entry)) continue
            sourceFiles(full, out)
        } else if (/\.(ts|tsx)$/.test(entry)) {
            out.push(full)
        }
    }
    return out
}

const rel = (p: string) => p.slice(ROOT.length + 1).replace(/\\/g, '/')

describe('table style adoption', () => {
    it('no NEW file hand-rolls the header typography', () => {
        const offenders = allSourceFiles()
            .filter(p => !p.endsWith('tableStyles.ts'))
            .filter(p => readFileSync(p, 'utf8').includes(HEADER_TYPOGRAPHY))
            .map(rel)
            .filter(p => !EXEMPT.has(p))
            .filter(p => !HAND_ROLLED_TYPOGRAPHY.includes(p))
            .sort()

        expect(
            offenders,
            `${offenders.join(', ')} spells out the table header typography ` +
            `instead of importing TABLE_TH / TABLE_TH_TYPE from ` +
            `components/ui/tableStyles. Those tokens are the single source of ` +
            `truth; a local copy is how adjacent tabs came to look different.`
        ).toEqual([])
    })

    it('no NEW table skips tableStyles', () => {
        const offenders = allSourceFiles()
            .filter(p => p.endsWith('.tsx'))
            .filter(p => {
                const s = readFileSync(p, 'utf8')
                return s.includes('<table') && !s.includes('tableStyles')
            })
            .map(rel)
            .filter(p => !EXEMPT.has(p))
            .filter(p => !TABLES_NOT_USING_STYLES.includes(p))
            .sort()

        expect(
            offenders,
            `${offenders.join(', ')} renders a <table> without importing ` +
            `tableStyles. Use TABLE_CONTAINER / TABLE_THEAD / TABLE_TH / ` +
            `TABLE_TBODY so the console has one table look.`
        ).toEqual([])
    })

    it('the ratchets only shrink', () => {
        // A stale entry is as bad as a missing one: it lets a migrated file
        // regress silently. Both lists must name only real offenders.
        const litNow = new Set(
            allSourceFiles()
                .filter(p => !p.endsWith('tableStyles.ts'))
                .filter(p => readFileSync(p, 'utf8').includes(HEADER_TYPOGRAPHY))
                .map(rel)
        )
        const stale = HAND_ROLLED_TYPOGRAPHY.filter(p => !litNow.has(p))
        expect(
            stale,
            `${stale.join(', ')} no longer hand-rolls the typography — remove ` +
            `it from HAND_ROLLED_TYPOGRAPHY so the file cannot regress.`
        ).toEqual([])

        const tablesNow = new Set(
            allSourceFiles()
                .filter(p => p.endsWith('.tsx'))
                .filter(p => {
                    const s = readFileSync(p, 'utf8')
                    return s.includes('<table') && !s.includes('tableStyles')
                })
                .map(rel)
        )
        const staleTables = TABLES_NOT_USING_STYLES.filter(p => !tablesNow.has(p))
        expect(
            staleTables,
            `${staleTables.join(', ')} now imports tableStyles — remove it from ` +
            `TABLES_NOT_USING_STYLES.`
        ).toEqual([])
    })
})
