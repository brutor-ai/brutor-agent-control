/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Admin Console's API client. One class, one instance per session,
 * every method of the Control Plane's admin API.
 *
 * The class is composed: `GatewayApiBase` (./gatewayApi/base.ts)
 * holds the session and the request method, and each domain module in
 * ./gatewayApi declares the methods of one area. `installApiModules` puts
 * them on this class's prototype; the interface below gives them their
 * types. Call sites see one client, as before.
 */

import { GatewayApiBase } from './gatewayApi/base';
import { BUILTIN_API_MODULES, installApiModules } from './gatewayApi/modules';
import type { ApiModuleMethods } from './gatewayApi/modules';

export {
    TOKEN_EXPIRED_EVENT,
    PASSWORD_CHANGE_REQUIRED_EVENT,
    GATEWAY_AUTH_TOKEN_KEY,
    isJwtExpired,
    notifyTokenExpired,
    healthCheckTimeoutMs,
    BULK_HEALTH_CHECK_TIMEOUT_MS,
    TokenExpiredError,
    GatewayAPIError,
} from './gatewayApi/base';
export type { ErrorContext, ErrorReporter } from './gatewayApi/base';

/** The client: the base plus the methods of every built-in module. */
export interface GatewayApiClient extends GatewayApiBase, ApiModuleMethods<typeof BUILTIN_API_MODULES> {}

class ComposedGatewayApiClient extends GatewayApiBase {}
installApiModules(ComposedGatewayApiClient, BUILTIN_API_MODULES);

export const GatewayApiClient = ComposedGatewayApiClient as unknown as new (
    ...args: ConstructorParameters<typeof GatewayApiBase>
) => GatewayApiClient;

// Default export for convenience
export default GatewayApiClient;
