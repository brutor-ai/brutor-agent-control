/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A page that shows one tenant's data, opened by an administrator of no
 * tenant.
 *
 * The Control Plane answers such an administrator 400 on every
 * tenant-scoped route unless the request names a tenant. The page
 * therefore asks for the tenant first, and its requests name it. A tenant
 * administrator is of one tenant and is asked nothing.
 */

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

let principal: unknown = null;
vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => ({ principal }) }));

import { actingTenant, setActingTenant } from '../../../lib/gatewayApi/base';
import { forgetChosenTenant } from '../chosenTenant';
import { TenantScoped } from '../TenantScoped';

/** A page that records which tenant the client named when it was first drawn. */
const seenAtMount: Array<string | null> = [];
function Page() {
    const [seen] = React.useState(() => {
        seenAtMount.push(actingTenant());
        return actingTenant();
    });
    return <p>the page, for {seen ?? 'nobody'}</p>;
}

const Choice = ({ value, onChange }: { value: string; onChange: (id: string) => void }) => (
    <select aria-label="Tenant" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose a tenant</option>
        <option value="acme">Acme</option>
        <option value="globex">Globex</option>
    </select>
);

const open = () =>
    render(
        <TenantScoped pageName="Approvals">
            <Page />
        </TenantScoped>,
    );

beforeEach(() => {
    principal = null;
    seenAtMount.length = 0;
    forgetChosenTenant();
    setActingTenant(null);
});
afterEach(cleanup);

describe('TenantScoped', () => {
    it('shows the page to a tenant administrator, and asks nothing', () => {
        open();
        expect(screen.getByText('the page, for nobody')).toBeInTheDocument();
        expect(screen.queryByTestId('tenant-needed')).not.toBeInTheDocument();
        expect(actingTenant()).toBeNull();
    });

    it('asks an administrator of no tenant which tenant to show, in place of the page', () => {
        principal = { id: 'system', TenantChoice: Choice };
        open();
        const ask = screen.getByTestId('tenant-needed');
        expect(ask).toHaveTextContent('Choose a tenant');
        expect(ask).toHaveTextContent('Approvals shows the data of one tenant. Choose the tenant to show.');
        expect(screen.queryByText(/^the page/)).not.toBeInTheDocument();
        expect(seenAtMount).toEqual([]);
    });

    it('shows the page for the chosen tenant, whose requests name it from the first one', () => {
        principal = { id: 'system', TenantChoice: Choice };
        open();
        fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'acme' } });
        expect(screen.getByText('the page, for acme')).toBeInTheDocument();
        expect(seenAtMount).toEqual(['acme']);
        expect(screen.queryByTestId('tenant-needed')).not.toBeInTheDocument();
        expect(screen.getByTestId('tenant-shown')).toHaveTextContent('Showing tenant');
    });

    it('draws the page again for another tenant', () => {
        principal = { id: 'system', TenantChoice: Choice };
        open();
        fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'acme' } });
        fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'globex' } });
        expect(screen.getByText('the page, for globex')).toBeInTheDocument();
        expect(seenAtMount).toEqual(['acme', 'globex']);
    });

    it('stops naming the tenant when the page is left, and remembers the choice for the next such page', () => {
        principal = { id: 'system', TenantChoice: Choice };
        const first = open();
        fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'acme' } });
        first.unmount();
        expect(actingTenant()).toBeNull();

        open();
        expect(screen.getByText('the page, for acme')).toBeInTheDocument();
        expect(actingTenant()).toBe('acme');
    });

    it('says the page needs a tenant when the administrator has no way to choose one', () => {
        principal = { id: 'other' };
        open();
        expect(screen.getByTestId('tenant-needed')).toHaveTextContent(
            'Approvals shows the data of one tenant. Sign in as an administrator of that tenant to see it.',
        );
        expect(screen.queryByText(/^the page/)).not.toBeInTheDocument();
    });
});
