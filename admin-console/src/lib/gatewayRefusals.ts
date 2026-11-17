/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Refusals of the gateway that reach the console as recorded text or as an
 * answer the Control Plane hands on, in words an administrator can act on.
 *
 * **Argument policies.** A call refused by an argument policy is recorded
 * as `Argument policy (<policy>/<rule>): <the gateway's sentence>`. Two
 * rules are the gateway's own and no rule of any policy:
 *
 *   arguments_truncated  the arguments are beyond what the gateway analyses
 *                        for a policy (by default 512 values, 8 levels, 64
 *                        items of a list; a policy may state its own), so no
 *                        rule can be trusted over them and the call is refused
 *   policy_unavailable   the policies that apply could not be loaded (a
 *                        stored policy whose rules the gateway cannot read),
 *                        and the call is refused rather than let through
 *
 * **MCP server tokens.** The gateway keeps an MCP server's static token
 * encrypted and opens it for each call. A stored value that does not
 * decrypt, or that cannot be a request header, is not sent in any form: the
 * call is answered 502 and the server is not called until the token is
 * saved again.
 *
 * **Evidence.** A tenant with a signing key of its own whose key the
 * gateway cannot decrypt gets nothing sealed (`tenant_key_unavailable`, 503)
 * until the key is rotated. The Control Plane hands the gateway's answer on
 * under the same code, and the code is what is recognised: no failure is
 * read from the words of a sentence.
 */

import { ANALYSIS_LIMITS } from './argumentPolicyTypes';
import { getRecord, getString } from './narrow';

const NOTHING_SENT = 'Nothing was sent.';

/** What to do about a refusal `text` records; null when it records none the console has words for. */
export function explainGatewayRefusal(text: string | null | undefined): string | null {
    if (!text) return null;
    if (text.includes('arguments_truncated')) {
        const policy = /Argument policy \((.+)\/arguments_truncated\)/.exec(text)?.[1];
        const against = policy ? ` against argument policy "${policy}"` : '';
        const [values, levels, items] = ANALYSIS_LIMITS;
        return `The call was refused because its arguments are too large to check${against}. Unless a policy sets its own limits, the gateway reads at most ${values.byDefault} values, ${levels.byDefault} levels of nesting and ${items.byDefault} items of a list in the arguments of one call. It refuses a call beyond the limits of a JSON policy that applies to it, whatever the rules say. ${NOTHING_SENT} Send the call with smaller arguments, or raise the limits of the policy (Advanced: up to ${values.most} values, ${levels.most} levels and ${items.most} items).`;
    }
    if (text.includes('Configured auth token for this server cannot be read')) {
        return `The gateway could not read the token stored for this MCP server, so it did not call the server. ${NOTHING_SENT} Open the MCP server, enter its token again and save. The token becomes unreadable when the installation's encryption key (ENCRYPTION_KEY) changes; restoring the key also makes it readable again.`;
    }
    if (text.includes('Configured auth token for this server is malformed')) {
        return `The token stored for this MCP server cannot be sent as it is (it holds a character a request header cannot carry), so the gateway did not call the server. ${NOTHING_SENT} Open the MCP server, enter its token again and save.`;
    }
    if (text.includes('policy_unavailable')) {
        return `The call was refused because the gateway could not read the argument policies that apply to it, and it does not let a call through unchecked. ${NOTHING_SENT} The usual cause is a stored argument policy whose rules the gateway cannot read: open the argument policies of the group, save the one that fails again or delete it. The gateway log names it.`;
    }
    return null;
}

/** The `error` of the Control Plane's answer (and of the gateway's) when a tenant's own signing key cannot be read. */
export const TENANT_KEY_UNAVAILABLE = 'tenant_key_unavailable';

/** Whether a response body says the tenant's own signing key cannot be read. */
export function isTenantKeyUnavailableBody(body: unknown): boolean {
    const detail = getRecord(body, 'detail') ?? body;
    return getString(detail, 'error') === TENANT_KEY_UNAVAILABLE;
}

/** Whether a caught value (what the API client threw) says the tenant's own signing key cannot be read. */
export function isTenantKeyUnavailable(error: unknown): boolean {
    return isTenantKeyUnavailableBody(getRecord(error, 'details'));
}

export const TENANT_KEY_UNAVAILABLE_TEXT =
    "The gateway cannot read this tenant's signing key, so it seals no evidence for the tenant. Rotate the key: the new key signs from then on, and what was not sealed is sealed afterwards. " +
    "The key becomes unreadable when the installation's encryption key (ENCRYPTION_KEY) changes; restoring it also makes the key readable again.";
