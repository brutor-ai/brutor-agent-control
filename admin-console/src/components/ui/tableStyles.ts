/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Admin Console table style — single source of truth.
 *
 * Reference look (AI Models listing): muted uppercase tracked headers
 * on a `bg-surface-alt` strip, horizontal `border-default` row
 * dividers only (never vertical borders, never striping), inside a
 * bordered `rounded-lg` surface. Both themes get this for free via
 * the surface/text tokens — never hard-code a gray.
 *
 * Compose with density/alignment as needed:
 *   <th className={TABLE_TH}>                          // standard cell
 *   <th className={`${TABLE_TH_TYPE} text-right px-4 py-2`}>  // custom
 *
 * Padding note: `TABLE_TH` carries the reference `px-4 py-3`. Dense
 * contexts (dialogs, system panes) may keep tighter padding — use
 * `TABLE_TH_TYPE` plus their own spacing — but the typography and
 * color tokens are not negotiable.
 */

/** Typography + color for a header cell, without spacing/alignment. */
export const TABLE_TH_TYPE = 'text-xs font-medium text-muted uppercase tracking-wider';

/** Standard header cell at reference density (left-aligned). */
export const TABLE_TH = `px-4 py-3 text-left ${TABLE_TH_TYPE}`;

/** Header strip. Apply to <thead> (or the header row div of a
 *  div/grid-based list). */
export const TABLE_THEAD = 'bg-surface-alt border-b border-default';

/** Body divider strategy for <tbody>. Div-based lists use per-row
 *  `border-b border-default` instead — both render identically. */
export const TABLE_TBODY = 'divide-y divide-default';

/** Bordered wrapper around a table/list. */
export const TABLE_CONTAINER = 'bg-surface rounded-lg border border-default overflow-hidden';

/** Row hover used by interactive listings. */
export const TABLE_ROW_HOVER = 'hover:bg-surface-alt';
