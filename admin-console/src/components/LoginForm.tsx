/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { AlertCircle, Eye, EyeOff, Loader, Building2 } from 'lucide-react';
import { BrutorLogo } from './BrutorLogo';
import { useAuth } from '../hooks/useAuth';
import { useSlot, type SignInSlotItem } from '../app/slots';
import { accountLockMessage } from '../lib/accountLock';
import { errorKind, errorMessage, getString } from '../lib/narrow';
import { asyncHandler } from '../lib/asyncSafe';
import TwoFactorEnrollmentStep from './TwoFactorEnrollmentStep';
import { productName } from '../app/productName';

/** The tenant administrator, or the id of a contributed principal. */
type AdminType = string;

const LoginForm: React.FC = () => {
    const [adminType, setAdminType] = useState<AdminType>('tenant');
    // Who can sign in beside the tenant administrator (slot `app.principals`).
    // With nobody contributed the form is the tenant administrator's and has
    // no choice of administrator type.
    const principals = useSlot('app.principals');
    const principal = principals.find((p) => p.id === adminType);
    // Which tenant to sign in to is asked only where something contributes
    // the question (slot `app.sign-in`): the console's own form has no
    // tenant field and signs in without naming one.
    const tenantSignIn: SignInSlotItem | undefined = useSlot('app.sign-in')[0];
    const initialTenant = tenantSignIn?.tenant.initial ?? '';
    const [credentials, setCredentials] = useState({
        username: '',
        password: '',
        tenant_id: initialTenant,
    });
    /** The tenant signed in to: the field's, trimmed, for a tenant administrator where the field is there. */
    const tenantOf = (type: AdminType): string | undefined =>
        type === 'tenant' && tenantSignIn ? credentials.tenant_id.trim() : undefined;
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [showPassword, setShowPassword] = useState(false);

    // 2FA challenge state — populated when password verifies but the
    // admin has TOTP enabled. We swap the form over to a code entry
    // input and POST the temp_token + code to /login/2fa.
    const [needs2FA, setNeeds2FA] = useState(false);
    const [tempToken, setTempToken] = useState('');
    const [totpCode, setTotpCode] = useState('');

    // Enrolment owed — the password verified, the tenant's two-factor
    // policy requires 2FA and this admin has none. No session was issued;
    // the temp token opens the enrolment step, which ends in one.
    const [enrollment, setEnrollment] = useState<{ tempToken: string; tenantName?: string } | null>(null);

    // Use the AuthProvider's login method
    const { login, complete2FALogin, gatewayClient } = useAuth();

    const handleLogin = async (e: React.FormEvent) => {
        e.preventDefault();
        setLoading(true);
        setError('');

        try {
            // Only pass tenant_id for tenant admins
            const tenantId = tenantOf(adminType);

            await login(
                credentials.username,
                credentials.password,
                adminType,
                tenantId
            );
        } catch (err: any) {
            // useAuth.login throws Error('2FA_REQUIRED') with temp_token
            // attached when the admin has TOTP enabled. Switch the form
            // into code-entry mode instead of surfacing a login error.
            if (err?.message === '2FA_REQUIRED' && err?.temp_token) {
                setNeeds2FA(true);
                setTempToken(err.temp_token);
                setTotpCode('');
                setError('');
            } else if (errorMessage(err, '') === '2FA_ENROLLMENT_REQUIRED' && getString(err, 'temp_token')) {
                setEnrollment({
                    tempToken: getString(err, 'temp_token') ?? '',
                    tenantName: getString(err, 'tenant_display_name'),
                });
                setError('');
            } else {
                console.error('Login error:', errorKind(err));
                setError(err.message || 'Login failed');
            }
        } finally {
            setLoading(false);
        }
    };

    const handle2FASubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setLoading(true);
        setError('');

        try {
            const resp = await gatewayClient?.completeTwoFactorLogin(tempToken, totpCode);
            if (resp?.token) {
                // complete2FALogin mirrors the post-login setup the
                // password-only flow performs (localStorage + state)
                // so the session is usable immediately.
                complete2FALogin(
                    resp,
                    adminType,
                    tenantOf(adminType),
                );
            } else {
                setError('2FA verification failed');
            }
        } catch (err) {
            setError(accountLockMessage(err) ?? errorMessage(err, 'Invalid 2FA code'));
        } finally {
            setLoading(false);
        }
    };

    const handleInputChange = (field: 'username' | 'password' | 'tenant_id', value: string) => {
        setCredentials(prev => ({ ...prev, [field]: value }));
        if (error) setError(''); // Clear error when user starts typing
    };

    const handleAdminTypeChange = (type: AdminType) => {
        setAdminType(type);
        setError('');
        // Reset credentials when switching
        setCredentials({
            username: '',
            password: '',
            tenant_id: initialTenant,
        });
    };

    // --- 2FA enrolment view ---
    if (enrollment) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-gradient-to-br bg-surface-alt">
                <div className="max-w-md w-full space-y-6 p-8 rounded-xl shadow-lg bg-surface-raised">
                    <div className="flex justify-center">
                        <BrutorLogo size="large" variant="auto" />
                    </div>
                    <TwoFactorEnrollmentStep
                        tempToken={enrollment.tempToken}
                        tenantName={enrollment.tenantName}
                        backLabel="Back to sign in"
                        onSignedIn={(session) => complete2FALogin(session, 'tenant', tenantOf('tenant'))}
                        onBack={() => { setEnrollment(null); setError(''); }}
                    />
                </div>
            </div>
        );
    }

    // --- 2FA challenge view ---
    // After password verifies, if the admin has TOTP enabled the backend
    // returns requires_2fa + temp_token. Render a minimal code-entry
    // form instead of the password form; resetting to idle re-shows the
    // password form.
    if (needs2FA) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-gradient-to-br bg-surface-alt">
                <div className="max-w-md w-full space-y-6 p-8 rounded-xl shadow-lg bg-surface-raised">
                    <div className="text-center">
                        <div className="flex justify-center mb-6">
                            <BrutorLogo size="large" variant="auto" />
                        </div>
                        <h2 className="text-2xl font-bold text-secondary">Two-Factor Authentication</h2>
                        <p className="mt-2 text-sm text-secondary">
                            Enter the 6-digit code from your authenticator app, or a backup code.
                        </p>
                    </div>
                    <form onSubmit={asyncHandler(handle2FASubmit, 'submit')} className="space-y-4">
                        <input
                            type="text"
                            value={totpCode}
                            onChange={(e) => setTotpCode(e.target.value.replace(/\s/g, '').slice(0, 10))}
                            className="w-full px-3 py-2 border border-strong rounded-md text-center text-lg tracking-widest font-mono bg-surface text-default focus:ring-2 focus:ring-primary-500 focus:border-primary-500"
                            placeholder="000000"
                            autoFocus
                            disabled={loading}
                            autoComplete="one-time-code"
                            inputMode="numeric"
                        />
                        {error && (
                            <div className="flex items-center space-x-2 p-3 bg-red-50 border border-red-200 rounded-md">
                                <AlertCircle className="h-4 w-4 text-red-600 flex-shrink-0" />
                                <span className="text-sm text-red-700">{error}</span>
                            </div>
                        )}
                        <button
                            type="submit"
                            disabled={loading || totpCode.length < 6}
                            className="w-full flex justify-center py-2 px-4 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50"
                        >
                            {loading ? (
                                <div className="flex items-center space-x-2">
                                    <Loader className="h-4 w-4 animate-spin" />
                                    <span>Verifying...</span>
                                </div>
                            ) : (
                                <span>Verify and sign in</span>
                            )}
                        </button>
                        <button
                            type="button"
                            onClick={() => { setNeeds2FA(false); setTempToken(''); setTotpCode(''); setError(''); }}
                            className="w-full text-sm text-muted hover:text-secondary"
                        >
                            Back to sign in
                        </button>
                    </form>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen flex items-center justify-center bg-gradient-to-br bg-surface-alt">
            <div className="max-w-md w-full space-y-8 p-8 rounded-xl shadow-lg bg-surface-raised">
                {/* Header with Brutor Logo */}
                <div className="text-center">
                    <div className="flex justify-center mb-6">
                        <BrutorLogo size="large" variant="auto" />
                    </div>
                    <h2 className="text-3xl font-bold text-secondary">
                        {productName()}
                    </h2>
                    <p className="mt-2 text-sm text-secondary">
                        Sign in to manage your AI Systems.
                    </p>
                </div>

                {/* Admin Type Toggle */}
                {principals.length > 0 && (
                <div className="space-y-2">
                    <label className="block text-sm font-medium text-secondary">
                        Administrator Type
                    </label>
                    <div className="grid grid-cols-2 gap-2">
                        <button
                            type="button"
                            onClick={() => handleAdminTypeChange('tenant')}
                            disabled={loading}
                            className={`
                                flex items-center justify-center gap-2 px-4 py-3 rounded-lg border-2 transition-all
                                ${adminType === 'tenant'
                                ? 'border-primary-500 bg-primary-50 text-primary-700'
                                : 'border-strong bg-surface text-secondary hover:border-gray-400'
                            }
                                ${loading ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                            `}
                        >
                            <Building2 className="h-5 w-5" />
                            <span className="font-medium">Tenant Admin</span>
                        </button>
                        {principals.map((option) => (
                        <button
                            key={option.id}
                            type="button"
                            onClick={() => handleAdminTypeChange(option.id)}
                            disabled={loading}
                            className={`
                                flex items-center justify-center gap-2 px-4 py-3 rounded-lg border-2 transition-all
                                ${adminType === option.id
                                ? 'border-primary-500 bg-primary-50 text-primary-700'
                                : 'border-strong bg-surface text-secondary hover:border-gray-400'
                            }
                                ${loading ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                            `}
                        >
                            <option.signInForm.icon className="h-5 w-5" />
                            <span className="font-medium">{option.label}</span>
                        </button>
                        ))}
                    </div>

                    {/* Helper text */}
                    <div className="text-xs text-secondary mt-2">
                        {principal ? (
                            <p>{principal.signInForm.hint}</p>
                        ) : (
                            <p>✓ Tenant admins manage resources within their assigned tenant</p>
                        )}
                    </div>
                </div>
                )}

                {/* Login Form */}
                <div className="mt-8 space-y-6">
                    <form onSubmit={asyncHandler(handleLogin, 'submit')} className="space-y-4">
                        {/* The tenant field, where one is contributed (only for tenant admins) */}
                        {adminType === 'tenant' && tenantSignIn && (
                            <tenantSignIn.tenant.Field
                                value={credentials.tenant_id}
                                onChange={(value) => handleInputChange('tenant_id', value)}
                                disabled={loading}
                            />
                        )}

                        {/* What a contributed principal says about its sign-in */}
                        {principal && <principal.signInForm.Note />}

                        {/* Username Field */}
                        <div>
                            <label htmlFor="username" className="block text-sm font-medium text-secondary mb-1">
                                Username *
                            </label>
                            <input
                                id="username"
                                type="text"
                                value={credentials.username}
                                onChange={(e) => handleInputChange('username', e.target.value)}
                                className="relative block w-full px-3 py-2 border border-strong rounded-md placeholder-gray-500 text-default bg-surface focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-500 disabled:bg-surface-alt"
                                placeholder="Enter your username"
                                disabled={loading}
                                required
                                autoComplete="username"
                            />
                        </div>

                        {/* Password Field */}
                        <div>
                            <label htmlFor="password" className="block text-sm font-medium text-secondary mb-1">
                                Password *
                            </label>
                            <div className="relative">
                                <input
                                    id="password"
                                    type={showPassword ? "text" : "password"}
                                    value={credentials.password}
                                    onChange={(e) => handleInputChange('password', e.target.value)}
                                    className="relative block w-full px-3 py-2 pr-10 border border-strong rounded-md placeholder-gray-500 text-default bg-surface focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-primary-500 disabled:bg-surface-alt"
                                    placeholder="Enter your password"
                                    disabled={loading}
                                    required
                                    autoComplete="current-password"
                                />
                                <button
                                    type="button"
                                    onClick={() => setShowPassword(!showPassword)}
                                    disabled={loading}
                                    className="absolute inset-y-0 right-0 pr-3 flex items-center"
                                    title={showPassword ? "Hide password" : "Show password"}
                                >
                                    {showPassword ? (
                                        <EyeOff className="h-4 w-4 text-muted hover:text-secondary" />
                                    ) : (
                                        <Eye className="h-4 w-4 text-muted hover:text-secondary" />
                                    )}
                                </button>
                            </div>
                        </div>

                        {/* Error Message */}
                        {error && (
                            <div className="flex items-center space-x-2 p-3 bg-red-50 border border-red-200 rounded-md">
                                <AlertCircle className="h-4 w-4 text-red-600 flex-shrink-0" />
                                <span className="text-sm text-red-700">{error}</span>
                            </div>
                        )}

                        {/* Submit Button */}
                        <button
                            type="submit"
                            disabled={
                                loading ||
                                !credentials.username ||
                                !credentials.password ||
                                (adminType === 'tenant' && tenantSignIn !== undefined && !credentials.tenant_id.trim())
                            }
                            className="w-full flex justify-center py-2 px-4 bg-primary-500 hover:bg-primary-600 text-white rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {loading ? (
                                <div className="flex items-center space-x-2">
                                    <Loader className="h-4 w-4 animate-spin" />
                                    <span>Signing in...</span>
                                </div>
                            ) : (
                                <span>
                                    {principal ? principal.signInForm.submit : 'Sign in as Tenant Admin'}
                                    {adminType === 'tenant' && tenantSignIn && credentials.tenant_id && (
                                        <span className="ml-1 text-primary-100">({credentials.tenant_id})</span>
                                    )}
                                </span>
                            )}
                        </button>
                    </form>
                </div>

                {/* Footer */}
                <div className="text-center text-xs text-muted">
                    <p>{productName()} v{__APP_VERSION__}</p>
                    {tenantSignIn && <p className="mt-1">{tenantSignIn.footer}</p>}
                </div>
            </div>
        </div>
    );
};

export default LoginForm;