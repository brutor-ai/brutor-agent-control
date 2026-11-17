/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** A cost in dollars, with more decimals the smaller it is. */
export const fmtCost = (n: number | undefined): string => {
    if (!n || n === 0) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(6)}`;
    if (n < 1) return `$${n.toFixed(4)}`;
    return `$${n.toFixed(2)}`;
};
