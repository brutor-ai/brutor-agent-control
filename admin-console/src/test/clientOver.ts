/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A gateway client for a test that answers requests itself: the methods are
 * the client's own, and each issues its request through the `request` the
 * test hands in. What a component asks of the Control Plane is then read off
 * that double as the path and the options the client sent.
 */

import { GatewayApiClient } from '../lib/gatewayApiClient';

export function clientOver(request: (path: string, options?: RequestInit) => unknown): GatewayApiClient {
    const client = new GatewayApiClient('http://control-plane.test', () => {});
    client.request = request as GatewayApiClient['request'];
    return client;
}
