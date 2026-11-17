/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Permission keys for the System console.
//
// The System nav is hidden when `system:read` is absent (AppSidebar.isVisible),
// but a curl-armed operator can still hit the route directly — so the parent
// re-checks at render time and shows a hard-deny pane. The same check guards
// destructive actions inside each sub-section.
//
// These live in their own module for two reasons: several panes need them
// (copy-pasted into each one, a renamed permission would silently keep
// granting access under the old string), and exporting
// them from a component file opts that file out of Fast Refresh.

export const PERM_READ = 'system:read';
export const PERM_OPERATE = 'system:operate';
export const PERM_MAINTENANCE = 'system:maintenance:toggle';
