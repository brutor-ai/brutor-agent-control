/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * TenantAdminProfile — self-scoped settings for the currently
 * logged-in admin. The backend endpoints it wraps
 * (`/v1/admin/users/me/change-password` and `/v1/admin/users/me/2fa/*`)
 * serve every kind of administrator, and so does this page: what it
 * tells a principal other than the tenant administrator comes from that
 * principal's item in the slot `app.principals` (`profile`).
 *
 * Two sub-tabs:
 *
 *   * **Profile** — read-only identity fields plus a "Change
 *     password" form. Hits the dedicated self-service endpoint
 *     that requires the current password.
 *   * **Security** — hosts `TwoFactorSetup` for TOTP enrollment /
 *     disablement. The framing makes "this is about ME, not
 *     other admins I manage" explicit.
 *
 * Anything that manipulates OTHER admin users lives next door in
 * `AdminUserAndRoleManagement`. Keep that split — the two scopes on
 * one tab are confusing.
 */

import React, { useState } from 'react';
import { User, Lock } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';
import { TwoFactorSetup } from './TwoFactorSetup';
import ChangePasswordForm from './ChangePasswordForm';

type SubTabId = 'profile' | 'security';

const TenantAdminProfile: React.FC = () => {
    const [activeSubTab, setActiveSubTab] = useState<SubTabId>('profile');
    // A contributed principal (slot `app.principals`) says what the page
    // tells it; a tenant administrator gets the page as it is.
    const { principal } = useAuth();

    return (
        <div className="h-full app-column">
            {/* Sub-tabs Navigation */}
            <div className="flex border-b border-default bg-surface px-6">
                <button
                    onClick={() => setActiveSubTab('profile')}
                    className={`flex items-center space-x-2 px-4 py-3 border-b-2 transition-colors ${
                        activeSubTab === 'profile'
                            ? 'border-primary-500 text-primary-600 bg-primary-50'
                            : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt'
                    }`}
                >
                    <User className="w-4 h-4" />
                    <span className="font-medium">Profile</span>
                </button>
                <button
                    onClick={() => setActiveSubTab('security')}
                    className={`flex items-center space-x-2 px-4 py-3 border-b-2 transition-colors ${
                        activeSubTab === 'security'
                            ? 'border-primary-500 text-primary-600 bg-primary-50'
                            : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt'
                    }`}
                >
                    <Lock className="w-4 h-4" />
                    <span className="font-medium">Security</span>
                </button>
            </div>

            <div className="scroll-region">
                {activeSubTab === 'profile' && <ProfilePanel />}
                {activeSubTab === 'security' && (
                    <div className="p-6 max-w-2xl mx-auto">
                        <div className="mb-6">
                            <h2 className="text-2xl font-bold text-default mb-2">Account Security</h2>
                            <p className="text-secondary">
                                Two-factor authentication for your admin account. Settings here apply
                                only to you; other admins manage their own 2FA independently.
                                {principal && <principal.profile.SecurityNote />}
                            </p>
                        </div>
                        <TwoFactorSetup />
                    </div>
                )}
            </div>
        </div>
    );
};

// ---------------------------------------------------------------------------
// Profile panel — read-only identity fields + change password form.
// ---------------------------------------------------------------------------

const ProfilePanel: React.FC = () => {
    const { user, principal } = useAuth();

    const tenantDisplay = principal
        ? principal.profile.tenant
        : user?.tenant_display_name || user?.tenant_id || '—';

    return (
        <div className="p-6 max-w-2xl mx-auto space-y-8">
            {/* Heading */}
            <div>
                <h2 className="text-2xl font-bold text-default mb-2">Your Profile</h2>
                <p className="text-secondary">
                    Identity and credentials for <span className="font-medium text-default">you</span>{' '}
                    — the currently signed-in admin. Changes here don&rsquo;t affect other admins
                    {principal ? principal.profile.scope : ' in the tenant.'}
                </p>
            </div>

            {/* Identity (read-only) */}
            <section className="bg-surface border border-default rounded-lg p-5 space-y-4">
                <h3 className="text-sm font-semibold text-default">Identity</h3>
                <ProfileRow label="Username" value={user?.username} />
                <ProfileRow label="Email" value={user?.email} />
                <ProfileRow label="Full name" value={user?.full_name} />
                {/* A contributed principal has no tenant role (its sign-in
                    answers no role_id): the role it has is the principal. */}
                <ProfileRow label="Role" value={user?.role_name || user?.role_id || principal?.label} />
                <ProfileRow label="Admin type" value={user?.admin_type ?? '—'} />
                <ProfileRow label="Tenant" value={tenantDisplay} />
                {user?.last_login && (
                    <ProfileRow
                        label="Last sign-in"
                        value={new Date(user.last_login).toLocaleString()}
                    />
                )}
                {/* Editing email / full_name lives in the admin users tab
                    (edit dialog) — but only for tenant admins. A
                    contributed principal is in no tenant's admin-users
                    list, so omit that pointer to avoid sending it on a
                    hunt for a screen it can't see. */}
                {!principal && (
                    <p className="text-xs text-muted pt-2 border-t border-default">
                        To change email, full name, or role, open User Management → Admin Users &
                        Roles → Admin Users and edit your own row.
                    </p>
                )}
            </section>

            {/* Change password */}
            <section className="bg-surface border border-default rounded-lg p-5 space-y-4">
                <h3 className="text-sm font-semibold text-default">Change password</h3>

                {/* What a contributed principal has to know before changing its password */}
                {principal && <principal.profile.PasswordNotice />}

                <ChangePasswordForm />
            </section>
        </div>
    );
};

const ProfileRow: React.FC<{ label: string; value?: string | null }> = ({ label, value }) => (
    <div className="flex items-baseline justify-between gap-4">
        <span className="text-sm text-muted w-32 flex-shrink-0">{label}</span>
        <span className="text-sm text-default flex-1 break-words text-right min-h-0 min-w-0">
            {value ? value : <span className="text-muted italic">—</span>}
        </span>
    </div>
);

export default TenantAdminProfile;
