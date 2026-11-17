/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Walking a navigation tree, for the tests of the sidebar's information
 * architecture (`components/__tests__/AppSidebar.ia.test.tsx` and its
 * Enterprise half under `enterprise/src/host/`).
 */

export type AnyNode = { id: string; label: string; children?: AnyNode[] };

/** Every id in the tree, parents before their children. */
export function allIds(nodes: AnyNode[]): string[] {
    return nodes.flatMap((n) => [n.id, ...(n.children ? allIds(n.children) : [])]);
}

/** The node with that id, wherever it is in the tree. */
export function findNode(id: string, nodes: AnyNode[]): AnyNode | null {
    for (const n of nodes) {
        if (n.id === id) return n;
        if (n.children) {
            const hit = findNode(id, n.children);
            if (hit) return hit;
        }
    }
    return null;
}
