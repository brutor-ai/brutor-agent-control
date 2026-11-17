/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useLayoutEffect, useState } from 'react';

/**
 * The rendered content width of an element, kept current with a
 * ResizeObserver. Returns a callback ref to attach and the width in px —
 * `null` until measured, and whenever there is no layout engine to ask
 * (jsdom / happy-dom report 0), so a caller can fall back to its default
 * layout rather than to a zero-width one.
 *
 * Measured in a layout effect, so the first paint already uses the width
 * (no flash of the wrong layout).
 */
export function useElementWidth<T extends HTMLElement = HTMLDivElement>(): [(el: T | null) => void, number | null] {
    const [el, setEl] = useState<T | null>(null);
    const [width, setWidth] = useState<number | null>(null);

    useLayoutEffect(() => {
        if (!el) return;
        const read = () => {
            const w = el.clientWidth;
            setWidth(w > 0 ? w : null);
        };
        read();
        if (typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(read);
        ro.observe(el);
        return () => ro.disconnect();
    }, [el]);

    return [setEl, width];
}
