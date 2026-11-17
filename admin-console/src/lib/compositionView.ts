/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which Composition view a viewer sees: Simple graph (default), Diagram or
 * List — remembered per browser. A convenience only: storage may be
 * unavailable (private window, blocked site data), so every read and write
 * tolerates that and falls back to the default.
 */

/** Simple graph first, then the detailed Diagram and
 *  the accessible List. */
export type CompositionViewId = 'simple' | 'diagram' | 'list';
export const COMPOSITION_VIEWS: readonly CompositionViewId[] = ['simple', 'diagram', 'list'];
export const DEFAULT_COMPOSITION_VIEW: CompositionViewId = 'simple';
/** Per-browser memory of the viewer's choice (a convenience only: storage
 *  may be unavailable, and every read and write tolerates that). */
export const COMPOSITION_VIEW_STORAGE_KEY = 'brutor.admin.composition.view';

export function readStoredView(): CompositionViewId {
  try {
    const v = window.localStorage.getItem(COMPOSITION_VIEW_STORAGE_KEY);
    return COMPOSITION_VIEWS.includes(v as CompositionViewId)
      ? (v as CompositionViewId)
      : DEFAULT_COMPOSITION_VIEW;
  } catch {
    return DEFAULT_COMPOSITION_VIEW;
  }
}

export function storeView(view: CompositionViewId): void {
  try {
    window.localStorage.setItem(COMPOSITION_VIEW_STORAGE_KEY, view);
  } catch {
    /* storage unavailable (private mode, blocked site data) — not remembered */
  }
}
