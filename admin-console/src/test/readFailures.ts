/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * For the guards that hold a component to showing the reads it says failed
 * (`readFailuresShown.test.ts`, one per edition).
 *
 * A loader that keeps its data in its component says `marks.failed('part', …)`
 * or `marks.lost('part', …)` (`hooks/useRead.ts`); the page shows the failure
 * of a part by asking for it by the same name. A part that is marked and
 * never asked for is a failure held and not shown.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { productionSources } from './caughtValues';
import { REPO_ROOT } from './sourceRoots';

/** The parts `text` marks as failed by a literal name, less the ones it asks the failure of by that name. */
export function partsMarkedNotShown(text: string): string[] {
    const marked = new Set([...text.matchAll(/\.(?:failed|lost)\(\s*'([^']+)'/g)].map((m) => m[1]));
    const shown = new Set([...text.matchAll(/\b\w*[Ff]ailure\(\s*'([^']+)'/g)].map((m) => m[1]));
    return [...marked].filter((part) => !shown.has(part)).sort();
}

/** `file: part, part` for every production file under `root` that uses `useReadFailures` and marks a part it never shows. */
export function unshownReadFailures(root: string, shownByList: readonly string[]): string[] {
    return productionSources(root).flatMap((file) => {
        const name = path.relative(REPO_ROOT, file).split(path.sep).join('/');
        const text = readFileSync(file, 'utf8');
        if (!text.includes('useReadFailures(') && !/useReadFailures</.test(text)) return [];
        if (shownByList.includes(name)) return [];
        const parts = partsMarkedNotShown(text);
        return parts.length > 0 ? [`${name}: ${parts.join(', ')}`] : [];
    });
}

/** The production files under `root` that hold failures with `useReadFailures`. */
export function filesHoldingReadFailures(root: string): string[] {
    return productionSources(root).filter((file) => /useReadFailures[<(]/.test(readFileSync(file, 'utf8')));
}
