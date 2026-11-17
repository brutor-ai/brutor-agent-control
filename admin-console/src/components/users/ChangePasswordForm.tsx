/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The signed-in administrator's change of its own password
 * (`POST /v1/admin/users/me/change-password`, which needs the current
 * password): on the profile page, and on the screen an account whose
 * password was generated for it is sent to first
 * (`PasswordChangeRequiredScreen`).
 */

import React, { useState } from 'react';
import { AlertCircle, CheckCircle, Loader, Eye, EyeOff, Save } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';
import { useGateway } from '../../hooks/useGateway';
import { accountLockMessage } from '../../lib/accountLock';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler } from '../../lib/asyncSafe';
import { PASSWORD_RULE_HINT, passwordMeetsRule } from '../../lib/passwordRule';

interface ChangePasswordFormProps {
    /** Called once the password is changed. */
    afterChange?: () => void;
}

const ChangePasswordForm: React.FC<ChangePasswordFormProps> = ({ afterChange }) => {
    const { user } = useAuth();
    const { gatewayClient } = useGateway();

    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [showCurrent, setShowCurrent] = useState(false);
    const [showNew, setShowNew] = useState(false);
    const [showConfirm, setShowConfirm] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [successMsg, setSuccessMsg] = useState<string | null>(null);

    // Every administrator can change its own password — the backend
    // endpoint dispatches on admin_type internally.
    const canChangePassword = !!user?.id;

    const handleChangePassword = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setSuccessMsg(null);

        if (!user?.id) {
            setError('Not signed in.');
            return;
        }
        // Client-side guardrails. The backend re-validates — these
        // just catch obvious typos before the round-trip.
        if (!currentPassword) {
            setError('Enter your current password.');
            return;
        }
        if (!passwordMeetsRule(newPassword)) {
            setError(`New password must be ${PASSWORD_RULE_HINT}.`);
            return;
        }
        if (newPassword !== confirmPassword) {
            setError('The two new passwords don\u2019t match.');
            return;
        }
        if (newPassword === currentPassword) {
            setError('New password must differ from current password.');
            return;
        }

        setSaving(true);
        try {
            // Hit the dedicated self-service endpoint that requires the
            // current password. Using PATCH /tenant/{id} with {password}
            // would be a foot-gun — a hijacked session could silently change
            // the password and lock out the real user.
            await gatewayClient.changeOwnPassword({
                current_password: currentPassword,
                new_password: newPassword,
            });
            setCurrentPassword('');
            setNewPassword('');
            setConfirmPassword('');

            setSuccessMsg('Password updated. Next sign-in will require the new password.');
            afterChange?.();
        } catch (e) {
            // 423: the account is locked (five wrong passwords lock it for
            // 15 minutes; a lock stops new sign-ins, not this session) or
            // deactivated. The answer says which, and when a lock ends.
            setError(accountLockMessage(e) ?? errorMessage(e, 'Failed to update password.'));
        } finally {
            setSaving(false);
        }
    };

    return (
        <>
        {!canChangePassword && (
            <p className="text-sm text-secondary">
                Sign in to change your password.
            </p>
        )}
        {canChangePassword && (
            <form onSubmit={asyncHandler(handleChangePassword, 'submit')} className="space-y-4">
                <PasswordInput
                    label="Current password"
                    value={currentPassword}
                    onChange={setCurrentPassword}
                    show={showCurrent}
                    onToggleShow={() => setShowCurrent(s => !s)}
                    autoComplete="current-password"
                />
                <PasswordInput
                    label="New password"
                    value={newPassword}
                    onChange={setNewPassword}
                    show={showNew}
                    onToggleShow={() => setShowNew(s => !s)}
                    autoComplete="new-password"
                />
                <PasswordInput
                    label="Confirm new password"
                    value={confirmPassword}
                    onChange={setConfirmPassword}
                    show={showConfirm}
                    onToggleShow={() => setShowConfirm(s => !s)}
                    autoComplete="new-password"
                />

                {error && (
                    <div className="flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200">
                        <AlertCircle className="w-4 h-4 text-red-600 mt-0.5 flex-shrink-0" />
                        <p className="text-sm text-red-700">{error}</p>
                    </div>
                )}
                {successMsg && (
                    <div className="flex items-start gap-2 p-3 rounded-md bg-green-50 border border-green-200">
                        <CheckCircle className="w-4 h-4 text-green-600 mt-0.5 flex-shrink-0" />
                        <p className="text-sm text-green-700">{successMsg}</p>
                    </div>
                )}

                <div className="flex justify-end">
                    <button
                        type="submit"
                        disabled={saving || !currentPassword || !newPassword || !confirmPassword}
                        className="inline-flex items-center gap-2 px-4 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded-md disabled:opacity-50 transition-colors"
                    >
                        {saving ? (
                            <Loader className="w-4 h-4 animate-spin" />
                        ) : (
                            <Save className="w-4 h-4" />
                        )}
                        Update password
                    </button>
                </div>
            </form>
        )}
        </>
    );
};

interface PasswordInputProps {
    label: string;
    value: string;
    onChange: (v: string) => void;
    show: boolean;
    onToggleShow: () => void;
    autoComplete?: string;
}

const PasswordInput: React.FC<PasswordInputProps> = ({
    label,
    value,
    onChange,
    show,
    onToggleShow,
    autoComplete,
}) => (
    <div>
        <label className="block text-sm font-medium text-secondary mb-1">{label}</label>
        <div className="relative">
            <input
                type={show ? 'text' : 'password'}
                value={value}
                onChange={e => onChange(e.target.value)}
                autoComplete={autoComplete}
                className="w-full px-3 py-2 pr-10 border border-strong rounded-md bg-surface text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
            <button
                type="button"
                onClick={onToggleShow}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-default"
                tabIndex={-1}
            >
                {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
        </div>
    </div>
);

export default ChangePasswordForm;
