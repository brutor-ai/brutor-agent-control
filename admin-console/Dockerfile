# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Brutor Agent Control Admin Console, Community Edition - Production Dockerfile
# Multi-stage build for optimized React/Vite application
#
# This image is the Community console: src/ and nothing else. The build
# context does not need enterprise/ and .dockerignore leaves it out when it
# is there; the lockfile's workspace entry for it is skipped by `npm ci` when
# the directory is absent, so none of its packages (the obfuscator, the
# policy editor) is installed. The Enterprise image is built by
# enterprise/Dockerfile from the same context.
# =============================================================================

# =============================================================================
# Stage 1: Build - Compile TypeScript and bundle with Vite
# =============================================================================
# Node 22 (LTS "Jod"), matching .nvmrc and the CI workflows, so the shipped
# artifact is built on the same Node the checks run on. The type-aware
# ESLint config documents Node >= 20.19 as a hard floor.
FROM node:22-alpine AS builder

# Set working directory
WORKDIR /app

# Copy package files
COPY package.json package-lock.json* ./

# Install dependencies
# `npm ci` installs exactly what package-lock.json records and checks the
# peer dependencies: no flag switches that check off.
RUN npm ci --prefer-offline --no-audit --loglevel error

# Copy application source
COPY . .

# The Control Plane address embedded in the build (src/envRegistry.json).
# A container is told the real one at start (BRUTOR_CONTROL_PLANE_PUBLIC_URL,
# docker-entrypoint.sh), which wins over this.
ARG VITE_CONTROL_PLANE_PUBLIC_URL=http://localhost:5050

ENV VITE_CONTROL_PLANE_PUBLIC_URL=${VITE_CONTROL_PLANE_PUBLIC_URL}

# Lift Node's heap ceiling. Default is ~1.5 GB; tsc + vite on this
# codebase peak above that on the GitHub native ARM runners (7 GB
# machine) and the runner OOM-kills the build, surfacing as a
# silent "Error: The operation was canceled." in BuildKit.
ENV NODE_OPTIONS=--max-old-space-size=4096

# Build the application — split into separate steps so a failure
# makes it obvious which half died (a combined `tsc && vite build`
# hides the failing stage). Output will be in /app/dist.
RUN npx tsc -p tsconfig.json
RUN npx vite build

# The third-party licences. The build wrote /app/dist/THIRD_PARTY_LICENSES
# from the packages of the modules it bundled (scripts/third-party-licenses.mjs),
# each at its package-lock.json version, and stopped on an unknown or
# disallowed licence (scripts/third-party-licenses.json). The image is not
# built when that file is not the committed THIRD_PARTY_LICENSES.
RUN node scripts/third-party-licenses.mjs check /app/dist/THIRD_PARTY_LICENSES THIRD_PARTY_LICENSES

# The Community build is not obfuscated, so it can be read: fail the image
# if a control plane route under an Enterprise prefix or the id of an
# Enterprise navigation entry was bundled.
RUN node scripts/scan-community-bundle.mjs dist

# Verify build output exists
RUN ls -la /app/dist && \
    test -f /app/dist/index.html || (echo "Build failed - index.html not found" && exit 1)

# =============================================================================
# Stage 2: Production - Serve with nginx
# =============================================================================
FROM nginx:1.25-alpine

# What the image says about itself (OCI image annotations). The name, title,
# licence and source are one list, `scripts/image-names.txt`, and
# `src/__tests__/imageNames.test.ts` holds this file to it. The version and
# revision labels are given by the build that knows them
# (.github/workflows/docker-build.yml).
LABEL org.opencontainers.image.title="Brutor Agent Control Admin Console (Community Edition)"
LABEL org.opencontainers.image.description="Brutor Agent Control Admin Console, Community Edition: the management console of the governance platform"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"
LABEL org.opencontainers.image.vendor="Brutor AI Ltd"
# Which edition this image is; release tooling reads it.
LABEL ai.brutor.edition="community"
# Where the third-party licences are (also served beside the application).
LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"

# Install curl for health checks
RUN apk add --no-cache curl

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf
COPY nginx-security-headers.conf /etc/nginx/security-headers.conf

# Copy built assets from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# The third-party licences: beside the application (in the copy above,
# served as /THIRD_PARTY_LICENSES) and where the label points.
COPY --from=builder /app/dist/THIRD_PARTY_LICENSES /usr/share/licenses/brutor/THIRD_PARTY_LICENSES

# The entrypoint: writes the console's configuration from the container's
# environment, starts nginx.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Create non-root user for nginx
RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chown -R nginx:nginx /var/cache/nginx && \
    chown -R nginx:nginx /var/log/nginx && \
    chown -R nginx:nginx /etc/nginx/conf.d

# Touch log files and set permissions
RUN touch /var/run/nginx.pid && \
    chown -R nginx:nginx /var/run/nginx.pid

# Switch to non-root user
USER nginx

# Expose port
EXPOSE 3002

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:3002/ || exit 1

# Writes env-config.js, then starts nginx.
ENTRYPOINT ["/docker-entrypoint.sh"]