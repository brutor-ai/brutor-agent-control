#!/bin/sh
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Brutor Agent Control Admin Console: container entrypoint
#
# 1. Writes the console's configuration from the container's environment.
# 2. Starts nginx.
#
# The variables are defined in src/envRegistry.json; this script is shell and
# cannot read it, so src/__tests__/runtimeConfigEntrypoint.test.ts holds the
# names and the defaults below to the registry. No other name is read.
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# 1. The console's configuration
#
# env-config.js is loaded by index.html before the application. It sets
# window._env_, which src/env.ts reads under the variables' own names.
# -----------------------------------------------------------------------------

echo "Brutor Agent Control Admin Console: writing the configuration"

cat > /usr/share/nginx/html/env-config.js << CONFIG
// The container's configuration, written at start from its environment.
// Do not edit: the next start writes it again.
window._env_ = {
  BRUTOR_CONTROL_PLANE_PUBLIC_URL: "${BRUTOR_CONTROL_PLANE_PUBLIC_URL:-http://localhost:5050}",
  BRUTOR_GATEWAY_PUBLIC_URL: "${BRUTOR_GATEWAY_PUBLIC_URL:-http://localhost:8100}",
  BRUTOR_DEBUG_ENABLED: "${BRUTOR_DEBUG_ENABLED:-false}"
};
CONFIG

# The two addresses are not secrets: the browser is sent them.
echo "  Control Plane: ${BRUTOR_CONTROL_PLANE_PUBLIC_URL:-http://localhost:5050}"
echo "  Core Gateway:  ${BRUTOR_GATEWAY_PUBLIC_URL:-http://localhost:8100}"

if [ -z "${BRUTOR_CONTROL_PLANE_PUBLIC_URL}" ]; then
    echo "  BRUTOR_CONTROL_PLANE_PUBLIC_URL is not set: the console calls the default address"
fi
if [ -z "${BRUTOR_GATEWAY_PUBLIC_URL}" ]; then
    echo "  BRUTOR_GATEWAY_PUBLIC_URL is not set: the console shows the default address"
fi

# -----------------------------------------------------------------------------
# 2. nginx
# -----------------------------------------------------------------------------

exec nginx -g "daemon off;"
