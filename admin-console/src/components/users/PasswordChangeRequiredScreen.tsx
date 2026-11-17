/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What an administrator whose password was generated for it (the first
 * administrator of a tenant created without credentials) sees, and all it
 * sees, until it has changed that password: the change-password form and a
 * way to sign out. The Control Plane answers such an account nothing else
 * (403 `password_change_required`) and the core refuses its session, so the
 * console asks nothing else either. Once the password is changed the
 * session carries on.
 */

import React from 'react';
import { KeyRound, LogOut } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { asyncHandler } from '../../lib/asyncSafe';
import ChangePasswordForm from './ChangePasswordForm';

const PasswordChangeRequiredScreen: React.FC = () => {
    const { user, logout, passwordChanged } = useAuth();

    return (
        <div className="min-h-screen bg-surface-alt flex items-center justify-center p-4">
            <div className="w-full max-w-md bg-surface border border-default rounded-lg shadow-sm p-6 space-y-5">
                <div className="flex items-start gap-3">
                    <KeyRound className="w-6 h-6 text-primary-600 flex-shrink-0 mt-0.5" />
                    <div>
                        <h1 className="text-xl font-semibold text-default">Change your password</h1>
                        <p className="mt-1 text-sm text-secondary">
                            The password of <span className="font-medium text-default">{user?.username ?? 'this account'}</span>{' '}
                            was generated for it and must be changed before you can use the console. Enter the
                            password you signed in with, then a new one.
                        </p>
                    </div>
                </div>

                <ChangePasswordForm afterChange={() => passwordChanged()} />

                <div className="pt-3 border-t border-default flex justify-end">
                    <button
                        type="button"
                        onClick={asyncHandler(logout, 'logout')}
                        className="inline-flex items-center gap-2 px-3 py-1.5 text-sm text-secondary hover:text-default rounded-md hover:bg-surface-alt"
                    >
                        <LogOut className="w-4 h-4" />
                        Sign out
                    </button>
                </div>
            </div>
        </div>
    );
};

export default PasswordChangeRequiredScreen;
