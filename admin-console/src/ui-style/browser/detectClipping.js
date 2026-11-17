/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Finds content that is unreachable because an ancestor clips it and nothing
 * in between can scroll.
 *
 * ── Why this exists ────────────────────────────────────────────────────
 * This is the one failure mode in the layout system that no other gate can
 * see. `tsc` and ESLint do not know about layout; jsdom has no layout engine,
 * so a unit test measuring `scrollHeight` gets zeros. The symptom is content
 * that simply is not there, with no scrollbar and no error — the Settings
 * page hid Data Retention, Quotas and Features below the fold for weeks.
 *
 * The check has to run in a real browser. It is written as a self-contained
 * function with no imports so it can be injected into a page — by a
 * Playwright/Puppeteer spec, by the devtools console, or by an agent driving
 * Chrome — rather than requiring this package to depend on a browser.
 *
 *   const findings = detectClipping();   // [] means nothing is unreachable
 *
 * @param {object}  [options]
 * @param {Element} [options.root=document.body] subtree to examine
 * @param {number}  [options.tolerance=2] px of overflow to ignore, covering
 *                  sub-pixel rounding and 1px borders
 * @param {number}  [options.minArea=200] ignore elements too small to hold
 *                  content a user could miss
 * @returns {Array<{selector: string, hidden: number, clippedBy: string,
 *                  scrollHeight: number, clientHeight: number, text: string}>}
 */
export function detectClipping(options) {
    const opts = options || {};
    const root = opts.root || document.body;
    const tolerance = opts.tolerance === undefined ? 2 : opts.tolerance;
    const minArea = opts.minArea === undefined ? 200 : opts.minArea;

    /** A short, human-readable identifier for an element. */
    function describe(el) {
        if (!el || el === document.documentElement) return 'html';
        const id = el.id ? `#${el.id}` : '';
        const cls = typeof el.className === 'string' && el.className
            ? `.${el.className.trim().split(/\s+/).slice(0, 4).join('.')}`
            : '';
        return `${el.tagName.toLowerCase()}${id}${cls}`;
    }

    /*
     * Read the longhand, falling back to the shorthand. Browsers resolve
     * `overflow: hidden` into `overflowY` for us, but jsdom does not expand
     * the shorthand at all — so without the fallback this function is
     * untestable outside a browser, and silently blind to any engine that
     * reports only the shorthand.
     */
    const overflowY = (style) => style.overflowY || style.overflow || 'visible';
    const scrolls = (style) => /auto|scroll/.test(overflowY(style));
    const clips = (style) => /hidden|clip/.test(overflowY(style));

    const findings = [];

    for (const el of root.querySelectorAll('*')) {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        const hidden = el.scrollHeight - el.clientHeight;
        if (hidden <= tolerance) continue;

        // The element itself can scroll, so its content is reachable.
        if (scrolls(style)) continue;

        const rect = el.getBoundingClientRect();
        if (rect.width * rect.height < minArea) continue;

        /*
         * Walk up looking for whoever resolves the overflow. A scrolling
         * ancestor means the content is reachable; a clipping ancestor
         * reached first means it is not. Overflow propagates through
         * `visible` ancestors, which is why this is a walk and not a check
         * of the immediate parent.
         */
        let clippedBy = null;
        for (let a = el.parentElement; a; a = a.parentElement) {
            const s = getComputedStyle(a);
            if (scrolls(s)) break;
            if (clips(s)) { clippedBy = a; break; }
            if (a === document.body) {
                // The document scroller is the last resort.
                const doc = document.scrollingElement || document.documentElement;
                if (doc.scrollHeight - doc.clientHeight > tolerance) break;
            }
        }

        if (!clippedBy) continue;

        findings.push({
            selector: describe(el),
            hidden,
            clippedBy: describe(clippedBy),
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
            text: (el.textContent || '').trim().slice(0, 80),
        });
    }

    /*
     * Nested elements report the same clipping. Keep the outermost finding
     * per clipping ancestor: that is the region to fix.
     */
    const seen = new Set();
    return findings.filter((f) => {
        if (seen.has(f.clippedBy)) return false;
        seen.add(f.clippedBy);
        return true;
    });
}

/**
 * The same check as a single expression, for pasting into a console or an
 * `evaluate()` call where an import is not available.
 */
export const DETECT_CLIPPING_SOURCE = `(${detectClipping.toString()})()`;
