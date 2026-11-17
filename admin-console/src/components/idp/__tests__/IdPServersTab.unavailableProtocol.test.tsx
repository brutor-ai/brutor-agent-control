/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Identity Providers list and a stored provider of a protocol this
 * edition does not have.
 *
 * The control plane refuses (409 `identity_protocol_unavailable`) to change,
 * delete, test or discover such a row. Two things are pinned:
 *
 * - whatever the console believes, a refusal is shown with the server's
 *   sentence and the row stays as it was;
 * - where the console can tell beforehand (the row's protocol is not
 *   OIDC, the console's one protocol), the actions are already unavailable
 *   and say why, and nothing is sent, in the full build as in Community.
 *
 * The rejections are the ones the real API client throws for the server's
 * answer (`test/serverAnswers.ts`).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { callsWhileIdle } from '../../../test/idle';
import { WithBuiltinSlots } from '../../../test/builtinSlots';

const shared = vi.hoisted(() => () => import('../../../test/idp/idpServersTab'));
vi.mock('../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../../../components/common/Toast', async () => (await shared()).toastModule());

import IdPServersTab from '../IdPServersTab';
import { gateway, toast } from '../../../test/idp/idpServersTab';
import { identityProtocolUnavailable, thrownByClient } from '../../../test/serverAnswers';
import { action, OKTA, resetUnavailableProtocol, rowOf } from '../../../test/idp/idpUnavailableProtocol';

const SAML_REASON = 'This identity provider uses the protocol saml, which is not available in this edition.';

type Composition = 'community' | 'full';

/** The console composed without and with the Enterprise extension. */
async function open(composition: Composition) {
    render(<IdPServersTab />, composition === 'full' ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByText('Okta');
}

const sentNothing = () => {
    expect(gateway.deleteIdPConfig).not.toHaveBeenCalled();
    expect(gateway.testExistingIdP).not.toHaveBeenCalled();
    expect(gateway.discoverIdPEndpoints).not.toHaveBeenCalled();
    expect(gateway.updateIdPConfig).not.toHaveBeenCalled();
};

beforeEach(resetUnavailableProtocol);

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('IdPServersTab in a composition without the row\'s protocol', () => {
    it('marks the row and gives the reason on each action it cannot take', async () => {
        await open('community');
        const adfs = within(rowOf('ADFS'));
        expect(adfs.getByText('saml')).toBeInTheDocument();
        expect(adfs.getByTestId('idp-protocol-unavailable')).toHaveTextContent('Not available in this edition');
        expect(adfs.getByTestId('idp-protocol-unavailable')).toHaveAttribute('title', SAML_REASON);

        const test = adfs.getByTitle(`Test Connection is not available. ${SAML_REASON}`);
        const del = adfs.getByTitle(`Delete is not available. ${SAML_REASON}`);
        expect(test).toBeDisabled();
        expect(del).toBeDisabled();
        fireEvent.click(test);
        fireEvent.click(del);
        expect(screen.queryByPlaceholderText('Enter "ADFS"')).not.toBeInTheDocument();
        sentNothing();

        // Looking at it is not refused.
        expect(adfs.getByTitle('View Details')).toBeEnabled();
    });

    it('names a protocol it has never heard of instead of calling it OIDC', async () => {
        await open('community');
        const dir = within(rowOf('Directory'));
        expect(dir.getByText('ldap')).toBeInTheDocument();
        expect(dir.queryByText('OIDC')).not.toBeInTheDocument();
        expect(dir.getByTestId('idp-protocol-unavailable')).toHaveAttribute(
            'title',
            'This identity provider uses the protocol ldap, which is not available in this edition.',
        );
        expect(dir.getByTitle(/^Delete is not available\./)).toBeDisabled();
    });

    it('leaves an OIDC provider with every action', async () => {
        await open('community');
        const okta = within(rowOf('Okta'));
        expect(okta.getByText('OIDC')).toBeInTheDocument();
        expect(okta.queryByTestId('idp-protocol-unavailable')).not.toBeInTheDocument();
        for (const title of ['Test Connection', 'View Details', 'Edit', 'Delete']) {
            expect(okta.getByTitle(title)).toBeEnabled();
        }

        gateway.testExistingIdP.mockResolvedValue({ connected: true });
        fireEvent.click(okta.getByTitle('Test Connection'));
        await waitFor(() => expect(gateway.testExistingIdP).toHaveBeenCalledWith('okta'));

        fireEvent.click(okta.getByTitle('Delete'));
        expect(await screen.findByPlaceholderText('Enter "Okta"')).toBeInTheDocument();
    });

    it.each([undefined, null, 'oidc'])('treats a provider whose protocol is %s as OIDC', async (protocol) => {
        gateway.fetchIdPConfigs.mockResolvedValue({ idps: { okta: { ...OKTA, protocol } } });
        await open('community');
        const okta = within(rowOf('Okta'));
        expect(okta.getByText('OIDC')).toBeInTheDocument();
        expect(okta.queryByTestId('idp-protocol-unavailable')).not.toBeInTheDocument();
        expect(okta.getByTitle('Test Connection')).toBeEnabled();
        expect(okta.getByTitle('Delete')).toBeEnabled();
    });

    it('shows the details, with discovery unavailable and the reason beside it', async () => {
        await open('community');
        fireEvent.click(action('ADFS', 'View Details'));
        expect(await screen.findByText('Identity Provider Details')).toBeInTheDocument();
        const discover = screen.getByRole('button', { name: 'Discover Endpoints' });
        expect(discover).toBeDisabled();
        expect(screen.getByTestId('idp-discover-unavailable')).toHaveTextContent(SAML_REASON);
        fireEvent.click(discover);
        sentNothing();

        // An OIDC provider's details are as they were.
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        fireEvent.click(action('Okta', 'View Details'));
        await waitFor(() => expect(gateway.fetchIdPConfig).toHaveBeenCalledWith('okta'));
        expect(await screen.findByRole('button', { name: 'Discover Endpoints' })).toBeEnabled();
        expect(screen.queryByTestId('idp-discover-unavailable')).not.toBeInTheDocument();
    });

    it('opens Edit on the form\'s notice, which offers no save', async () => {
        await open('community');
        fireEvent.click(action('ADFS', 'Edit'));
        expect(await screen.findByTestId('idp-unsupported-protocol')).toHaveTextContent(
            'This identity provider uses the protocol saml, which this Admin Console cannot edit. It has not been changed.',
        );
        expect(screen.queryByRole('button', { name: 'Update Provider' })).not.toBeInTheDocument();
        sentNothing();
    });

    it('fetches the list once and then leaves the server alone', async () => {
        await open('community');
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
        expect(await callsWhileIdle(Object.values(gateway))).toBe(0);
    });
});

describe('IdPServersTab in the full build', () => {
    it('marks a stored SAML provider as the Community console does, and a protocol it has never heard of', async () => {
        await open('full');
        const adfs = within(rowOf('ADFS'));
        expect(adfs.getByTestId('idp-protocol-unavailable')).toHaveAttribute('title', SAML_REASON);
        expect(adfs.getByTitle(/^Delete is not available\./)).toBeDisabled();
        const dir = within(rowOf('Directory'));
        expect(dir.getByTestId('idp-protocol-unavailable')).toBeInTheDocument();
        expect(dir.getByTitle(/^Test Connection is not available\./)).toBeDisabled();
    });
});

describe('IdPServersTab: a refusal by the control plane is shown as the server worded it', () => {
    // The list takes the row for an OIDC one; the control plane it talks to
    // says otherwise. Each action is sent, refused, and reported.
    const refused = (act: string) => {
        const answer = identityProtocolUnavailable('Okta', 'saml', act);
        return { answer, message: answer.body.detail.message };
    };
    const FORMS: [string, boolean][] = [
        ['as the API client throws it', true],
        ['handed over as the response body', false],
    ];

    it.each(FORMS)('test connection, %s', async (_name, throughClient) => {
        const { answer, message } = refused('tested');
        gateway.testExistingIdP.mockRejectedValue(
            throughClient ? await thrownByClient(answer.status, answer.body) : answer.body,
        );
        await open('community');
        fireEvent.click(action('Okta', 'Test Connection'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Connection Test Failed', message));
        expect(gateway.testExistingIdP).toHaveBeenCalledWith('okta');
        expect(toast.success).not.toHaveBeenCalled();
        // The row is as it was and the action is offered again.
        await waitFor(() => expect(action('Okta', 'Test Connection')).toBeEnabled());
        expect(within(rowOf('Okta')).getByText('Enabled')).toBeInTheDocument();
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
    });

    it.each(FORMS)('delete, %s: inline, the dialog open, the row still listed', async (_name, throughClient) => {
        const { answer, message } = refused('deleted');
        gateway.deleteIdPConfig.mockRejectedValue(
            throughClient ? await thrownByClient(answer.status, answer.body) : answer.body,
        );
        await open('community');
        fireEvent.click(action('Okta', 'Delete'));
        const input = await screen.findByPlaceholderText('Enter "Okta"');
        fireEvent.change(input, { target: { value: 'Okta' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(await screen.findByText(message)).toBeInTheDocument();
        expect(gateway.deleteIdPConfig).toHaveBeenCalledWith('okta');
        expect(screen.getByPlaceholderText('Enter "Okta"')).toBeInTheDocument();
        expect(toast.success).not.toHaveBeenCalled();
        expect(rowOf('Okta')).toBeInTheDocument();
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
    });

    it.each(FORMS)('discover, %s', async (_name, throughClient) => {
        const { answer, message } = refused('discovered');
        gateway.discoverIdPEndpoints.mockRejectedValue(
            throughClient ? await thrownByClient(answer.status, answer.body) : answer.body,
        );
        await open('community');
        fireEvent.click(action('Okta', 'View Details'));
        fireEvent.click(await screen.findByRole('button', { name: 'Discover Endpoints' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Discovery Failed', message));
        expect(gateway.discoverIdPEndpoints).toHaveBeenCalledWith('okta', true);
        expect(toast.success).not.toHaveBeenCalled();
        // Neither the table nor the panel was reloaded: nothing changed.
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
        expect(gateway.fetchIdPConfig).toHaveBeenCalledTimes(1);
        expect(screen.getByText('Identity Provider Details')).toBeInTheDocument();
    });
});
