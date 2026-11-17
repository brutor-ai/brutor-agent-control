/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Security sub-tab: the tenant's two-factor policy.
 *
 * `mfa_policy` is a stored, enforced control, so: the stored value is what
 * the selector shows, a change
 * is sent as `mfa_policy`, a save from another tab does not round-trip it
 * (a stale form must not relax a policy another admin just set), the
 * control plane's 409 ("enable 2FA on your own account first") is shown as
 * written, and the help text says what each value does.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const request = vi.fn();
const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: {
        request,
        // The client's own methods, as the client issues them: through `request`.
        getCurrentTenant: () => request('/v1/admin/tenants/current'),
        getGatePolicyVocabulary: () => request('/v1/admin/ai-systems/gate-policy/vocabulary'),
        listLLMs: () => request('/v1/admin/llms?mode=chat&enabled=true', { method: 'GET' }),
        updateCurrentTenantSettings: (body: unknown) => request('/v1/admin/tenants/current', { method: 'PATCH', body: JSON.stringify(body) }) } }),
}));
vi.mock('../../common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));
vi.mock('../../common/Toast', () => ({
    useToast: () => toast,
}));

import TenantSettingsTab from '../TenantSettingsTab';

const OWN_2FA_FIRST =
    'Enable two-factor authentication on your own account first. This policy would require it of every administrator, and you would be signed out and have to enrol at your next sign-in.';

const tenant = (over: Record<string, unknown> = {}) => ({
    id: 't1',
    name: 't1',
    display_name: 'Tenant One',
    organization_name: 'Org',
    primary_contact_email: 'a@b.c',
    mfa_policy: 'optional',
    data_residency: null,
    data_retention: null,
    ...over,
});

function wireRequests(current: Record<string, unknown>, patch?: (body: Record<string, unknown>) => unknown) {
    const answer = (path: string, init?: RequestInit): unknown => {
        if (path === '/v1/admin/tenants/current' && init?.method === 'PATCH') {
            const body = JSON.parse(init.body as string) as Record<string, unknown>;
            return patch ? patch(body) : { ...current, ...body };
        }
        if (path === '/v1/admin/tenants/current') return current;
        if (path.startsWith('/v1/admin/llms')) return { items: [], models: [], total: 0 };
        return {};
    };
    // A refusal thrown by `patch` becomes a rejected request.
    request.mockImplementation((path: string, init?: RequestInit) =>
        Promise.resolve().then(() => answer(path, init)),
    );
}

async function openSecurity() {
    render(<TenantSettingsTab />);
    await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
    fireEvent.click(await screen.findByText('Security'));
    return screen.findByTestId('mfa-policy-select');
}

function patchBodies(): Record<string, unknown>[] {
    return request.mock.calls
        .filter(([p, init]) => p === '/v1/admin/tenants/current' && (init as RequestInit)?.method === 'PATCH')
        .map(([, init]) => JSON.parse((init as RequestInit).body as string) as Record<string, unknown>);
}

describe('TenantSettingsTab — Security (two-factor policy)', () => {
    beforeEach(() => {
        request.mockReset();
        Object.values(toast).forEach((fn) => fn.mockReset());
    });

    it.each(['optional', 'required_admins', 'required_all'])('shows the stored policy: %s', async (stored) => {
        wireRequests(tenant({ mfa_policy: stored }));
        expect(await openSecurity()).toHaveValue(stored);
    });

    it('sends a changed policy as mfa_policy and shows what the server stored', async () => {
        wireRequests(tenant());
        const select = await openSecurity();
        fireEvent.change(select, { target: { value: 'required_admins' } });
        fireEvent.click(screen.getByText('Save changes'));

        await waitFor(() => expect(patchBodies()).toHaveLength(1));
        expect(patchBodies()[0].mfa_policy).toBe('required_admins');
        await waitFor(() => expect(toast.success).toHaveBeenCalled());
        expect(screen.getByTestId('mfa-policy-select')).toHaveValue('required_admins');

        // Saved: a second save does not send it again.
        fireEvent.click(screen.getByText('Save changes'));
        await waitFor(() => expect(patchBodies()).toHaveLength(2));
        expect(patchBodies()[1]).not.toHaveProperty('mfa_policy');
    });

    it('does not round-trip the policy when it was not changed', async () => {
        wireRequests(tenant({ mfa_policy: 'required_all' }));
        render(<TenantSettingsTab />);
        await waitFor(() => expect(request).toHaveBeenCalledWith('/v1/admin/tenants/current'));
        fireEvent.click(await screen.findByText('Profile'));
        fireEvent.click(await screen.findByText('Save changes'));
        await waitFor(() => expect(patchBodies()).toHaveLength(1));
        expect(patchBodies()[0]).not.toHaveProperty('mfa_policy');
    });

    it("shows the control plane's 409 as written and keeps the stored policy", async () => {
        wireRequests(tenant(), () => {
            throw Object.assign(new Error(OWN_2FA_FIRST), { status: 409 });
        });
        const select = await openSecurity();
        fireEvent.change(select, { target: { value: 'required_all' } });
        fireEvent.click(screen.getByText('Save changes'));

        const alert = await screen.findByTestId('mfa-policy-error');
        expect(alert).toHaveTextContent(OWN_2FA_FIRST);
        expect(toast.error).toHaveBeenCalledWith('Save failed', OWN_2FA_FIRST);
        expect(toast.success).not.toHaveBeenCalled();
        // The stored policy did not change, and the page says so.
        expect(screen.getByTestId('mfa-policy-stored')).toHaveTextContent(/Optional/);

        // Choosing again clears the message.
        fireEvent.change(screen.getByTestId('mfa-policy-select'), { target: { value: 'optional' } });
        expect(screen.queryByTestId('mfa-policy-error')).toBeNull();
    });

    it('warns before a stricter policy is saved, not for a looser one', async () => {
        wireRequests(tenant({ mfa_policy: 'required_admins' }));
        const select = await openSecurity();
        expect(screen.queryByTestId('mfa-policy-tightening')).toBeNull();
        fireEvent.change(select, { target: { value: 'required_all' } });
        expect(screen.getByTestId('mfa-policy-tightening')).toHaveTextContent(/signed out/i);
        fireEvent.change(select, { target: { value: 'optional' } });
        expect(screen.queryByTestId('mfa-policy-tightening')).toBeNull();
    });

    it('says what each of the three values does', async () => {
        wireRequests(tenant());
        await openSecurity();
        const help = screen.getByTestId('mfa-policy-help');
        // Who must enrol, value by value (each in its own entry: a phrase
        // elsewhere in the text must not stand in for it).
        expect(screen.getByTestId('mfa-policy-help-optional')).toHaveTextContent(/^Optional: nobody is required/i);
        const admins = screen.getByTestId('mfa-policy-help-required_admins');
        expect(admins).toHaveTextContent(/^Required for administrators: every administrator of this tenant must have 2FA/i);
        expect(admins).not.toHaveTextContent(/Portal/);
        const everyone = screen.getByTestId('mfa-policy-help-required_all');
        expect(everyone).toHaveTextContent(/^Required for everyone: administrators as above, and Portal users must have 2FA/i);
        // An entry for each value the selector offers, and no other.
        const offered = Array.from(screen.getByTestId('mfa-policy-select').querySelectorAll('option')).map((o) => o.value);
        expect(offered).toEqual(['optional', 'required_admins', 'required_all']);
        for (const value of offered) expect(screen.getByTestId(`mfa-policy-help-${value}`)).toBeInTheDocument();
        // What happens to people without 2FA.
        expect(help).toHaveTextContent(/signed out/i);
        expect(help).toHaveTextContent(/next sign-in/i);
        // You cannot lock yourself out, and 2FA cannot be switched off under the policy.
        expect(help).toHaveTextContent(/your own account/i);
        expect(help).toHaveTextContent(/cannot be disabled/i);
        // External identity providers.
        expect(help).toHaveTextContent(/identity provider/i);
        // System administrators are outside it.
        expect(help).toHaveTextContent(/System administrators/i);
    });
});
