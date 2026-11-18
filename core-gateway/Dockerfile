# syntax=docker/dockerfile:1
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Brutor Agent Control Core Gateway, Community Edition - Production Dockerfile
# Multi-stage build for minimal final image (~30-50MB)
#
# Stage 1 (chef):    Install cargo-chef for dependency caching
# Stage 2 (planner): Prepare the dependency recipe
# Stage 3 (builder): Build dependencies (cached) then application
# Stage 4 (runtime): Minimal Debian image with just the binary
#
# No build secret: the Community gateway has no private dependency.
# The Enterprise image is built by enterprise/Dockerfile.
# =============================================================================

# =============================================================================
# Stage 1: Chef - Install cargo-chef for dependency caching
# =============================================================================
# 1.91+ required by the aws-smithy-* crates in Cargo.lock (1.88 fails
# `cargo chef cook` with "requires rustc 1.91"); keep ≥ the lockfile's MSRV.
# MUST stay on the -bookworm variant: the runtime stage is debian:bookworm-slim
# (glibc 2.36) — a trixie-based builder links against glibc 2.38/2.39 and the
# binary refuses to start ("version `GLIBC_2.39' not found").
#
# Pinned by version AND digest, matching rust-toolchain.toml:
# the compiler that is tested is the one that ships. Bump
# both together.
FROM rust:1.97.1-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3 AS chef

RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN cargo install cargo-chef --locked

# The third-party licences generator (scripts/third-party-licenses.sh), pinned.
RUN cargo install --locked --features cli cargo-about@0.9.3

WORKDIR /build

# =============================================================================
# Stage 2: Planner - Analyze dependencies and create recipe
# =============================================================================
FROM chef AS planner

COPY Cargo.toml ./
COPY Cargo.lock ./
COPY src/ src/
COPY crates/ crates/
COPY benches/ benches/

# Remove local dev overrides (path patches) if present
RUN rm -f .cargo/config.toml

RUN cargo chef prepare --recipe-path recipe.json

# =============================================================================
# Stage 3: Builder - Build dependencies (cached) then application
# =============================================================================
FROM chef AS builder

# Build dependencies first (this layer is cached unless Cargo.toml/lock changes)
COPY --from=planner /build/recipe.json recipe.json
RUN cargo chef cook --release --locked --recipe-path recipe.json

# Now build the application (only this layer rebuilds on src/ changes)
COPY Cargo.toml ./
COPY Cargo.lock ./
COPY src/ src/
COPY crates/ crates/
COPY benches/ benches/

# Remove local dev overrides (path patches) if present
RUN rm -f .cargo/config.toml

# --locked: build exactly the committed Cargo.lock, never a fresh resolution.
RUN cargo build --release --locked --bin brutor-gateway-core

# Third-party licences (shipped in each image): generated here from
# Cargo.lock, offline, from the crates this build used; the build fails when
# a crate's licence is outside about.toml's accepted list (deny.toml's
# allow-list) or cannot be read, and when the committed copy is not current.
COPY about.toml about.hbs THIRD_PARTY_LICENSES ./
COPY scripts/third-party-licenses.sh scripts/
RUN scripts/third-party-licenses.sh --check --out /build/THIRD_PARTY_LICENSES.generated

# =============================================================================
# Stage 4: Runtime - Minimal image with just the binary
# =============================================================================
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

# Metadata (https://github.com/opencontainers/image-spec/blob/main/annotations.md).
# The image is `ghcr.io/brutor-ai/brutor-agent-control-core-gateway`; the
# name, title, licence and source are one list, `scripts/image-names.txt`,
# and `tests/image_names_guard.rs` holds this file to it. The version label
# (`org.opencontainers.image.version`) is given by the build that knows it.
LABEL org.opencontainers.image.title="Brutor Agent Control Core Gateway (Community Edition)"
LABEL org.opencontainers.image.description="Brutor Agent Control Core Gateway, Community Edition: the runtime gateway that governs LLM, MCP, A2A and skill traffic"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"
LABEL org.opencontainers.image.vendor="Brutor AI Ltd"
# Which edition this image is. Release tooling and `scripts/image-smoke.sh`
# read `ai.brutor.edition`; the running binary says the same on
# `GET /v1/version` (from what it is composed of, not from this label).
LABEL ai.brutor.edition="community"
# Where the third-party licences file is (and /app/THIRD_PARTY_LICENSES).
LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"

# Install minimal runtime dependencies
# - ca-certificates: the system root store rustls reads (with its bundled
#   Mozilla roots). Add a private CA here and core trusts it. The binary
#   links no OpenSSL (`deny.toml` bans it).
# - curl: for health checks
# - python3: for agent skill script execution
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    python3-minimal \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r brutor && \
    useradd -r -g brutor -u 1000 -m -s /bin/bash brutor

# Set working directory
WORKDIR /app

# Copy the compiled binary from builder
COPY --from=builder /build/target/release/brutor-gateway-core /app/brutor-gateway-core
COPY --from=builder /build/THIRD_PARTY_LICENSES.generated /usr/share/licenses/brutor/THIRD_PARTY_LICENSES
COPY --from=builder /build/THIRD_PARTY_LICENSES.generated /app/THIRD_PARTY_LICENSES

# Create directories for logs and data
RUN mkdir -p /app/logs /app/data && \
    chown -R brutor:brutor /app

# Switch to non-root user
USER brutor

# Expose default port
EXPOSE 8100

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8100/health || exit 1

# Run the proxy
CMD ["/app/brutor-gateway-core"]
