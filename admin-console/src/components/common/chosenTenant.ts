/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant an administrator of no tenant last chose on a page that shows
 * one tenant's data (`TenantScoped`), remembered for the next such page of
 * the same visit. '' for none.
 */

let chosen = '';

export const chosenTenant = (): string => chosen;

export function rememberChosenTenant(tenantId: string): void {
    chosen = tenantId;
}

/** Forget the remembered choice (a test starts without one). */
export function forgetChosenTenant(): void {
    chosen = '';
}
