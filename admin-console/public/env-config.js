/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// The container's configuration. In an image, docker-entrypoint.sh writes
// this file at start from the container's environment. This copy is what a
// run from source serves: it leaves the configuration unset, so the values
// built in from .env apply (src/env.ts).
window._env_ = undefined;
