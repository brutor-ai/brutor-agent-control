# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# ==============================================================================
# Brutor Agent Control — Skill Runner (multi-stage Rust build + hardened runtime)
#
# Stage 1: cargo-chef plans the dep graph for caching
# Stage 2: cargo-chef cooks deps, then build the binary
# Stage 3: minimal Debian runtime with pinned language interpreters,
#          non-root user, no proxy secrets, no shell.
#
# The threat model is THREAT-MODEL.md in this repository — read it before
# changing any of the hardening flags below.
# ==============================================================================

FROM rust:1.88-slim AS chef
RUN apt-get update && \
    apt-get install -y --no-install-recommends pkg-config libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*
RUN cargo install cargo-chef --locked

# The third-party licences generator (scripts/third-party-licenses.sh), pinned.
RUN cargo install --locked --features cli cargo-about@0.9.3
WORKDIR /build

# ------------------------------------------------------------------------------
# Planner: analyse dependencies
# ------------------------------------------------------------------------------
FROM chef AS planner
COPY Cargo.toml Cargo.lock* ./
COPY src/ src/
RUN cargo chef prepare --recipe-path recipe.json

# ------------------------------------------------------------------------------
# Builder: build deps (cached) then application
# ------------------------------------------------------------------------------
FROM chef AS builder
COPY --from=planner /build/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY Cargo.toml Cargo.lock* ./
COPY src/ src/
RUN cargo build --release --bin brutor-skill-runner

# Third-party licences (shipped in each image): generated here from
# Cargo.lock, offline, from the crates this build used; the build fails when
# a crate's licence is outside about.toml's accepted list or cannot be read,
# and when the committed copy is not current.
COPY about.toml about.hbs THIRD_PARTY_LICENSES ./
COPY scripts/third-party-licenses.sh scripts/
RUN scripts/third-party-licenses.sh --check --out /build/THIRD_PARTY_LICENSES.generated

# ------------------------------------------------------------------------------
# Runtime: Debian slim + pinned interpreters.
#
# Why slim Debian and not distroless: we *must* ship a real shell
# + python3 + node + tsx + bash so the skill scripts can run.
# Distroless can't host all four. Slim Debian + apt-get pin + drop
# privilege gets us most of the way without a custom base image.
#
# Why these specific packages (and no others):
#   python3        — language interpreter (PYTHON skills)
#   nodejs         — JS runtime (JAVASCRIPT skills)
#   npm            — installs `tsx` globally at build time; TYPESCRIPT
#                    skills run `tsx` directly (never `npx`, which can
#                    reach for the registry; the runner has no network)
#   bash           — BASH skills
#   ca-certificates — TLS roots so skills' HTTPS calls work (if the
#                    egress policy permits them)
#   wget           — used by compose's HEALTHCHECK (must be small;
#                    busybox alternative is fine if we ever switch)
#
# Pinned via apt-pin in CI; manual `--no-install-recommends` here
# keeps the image small. `rm -rf /var/lib/apt/lists/*` is mandatory
# for both layer size and to avoid leaking apt indexes that an
# attacker could grep for vulnerability data.
# ------------------------------------------------------------------------------
FROM debian:bookworm-slim

# Metadata (https://github.com/opencontainers/image-spec/blob/main/annotations.md).
# The image is `ghcr.io/brutor-ai/brutor-agent-control-skill-runner`, one
# build that both editions run; the name, title, licence and source are one
# list, `scripts/image-names.txt`, and `tests/image_names_guard.rs` holds
# this file to it. The version label (`org.opencontainers.image.version`) is
# given by the build that knows it.
LABEL org.opencontainers.image.title="Brutor Agent Control Skill Runner (Community Edition)"
LABEL org.opencontainers.image.description="Brutor Agent Control Skill Runner, Community Edition: the sandbox that runs Agent Skill scripts for the Core Gateway"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"
LABEL org.opencontainers.image.vendor="Brutor AI Ltd"
LABEL ai.brutor.edition="community"
# Where the third-party licences file is (and /app/THIRD_PARTY_LICENSES).
LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        bash \
        python3 \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/* \
    # Pre-cache `tsx` so the first TypeScript skill doesn't pay a
    # network round-trip. Restricted to a known version pinned with
    # the rest of the image; locks bumps to the release pipeline.
    && npm install -g tsx@4 \
    # Strip npm's per-user cache so it can't accidentally re-populate
    # under the non-root user later.
    && rm -rf /root/.npm

WORKDIR /app

COPY --from=builder /build/target/release/brutor-skill-runner /app/brutor-skill-runner
COPY --from=builder /build/THIRD_PARTY_LICENSES.generated /usr/share/licenses/brutor/THIRD_PARTY_LICENSES
COPY --from=builder /build/THIRD_PARTY_LICENSES.generated /app/THIRD_PARTY_LICENSES

# Non-root user. UID 10001 is conventional for non-system service
# accounts and won't collide with any host UID the user might mount.
# `--shell /usr/sbin/nologin` ensures even an interactive-style
# `docker exec` attack can't drop to a shell as this user.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin runner && \
    chown -R runner:runner /app

USER runner

# Log filter; operators can override RUST_LOG in compose for verbose
# debug. BRUTOR_LOG_JSON_ENABLED=true switches the lines to JSON.
ENV RUST_LOG=brutor_skill_runner=info,tower_http=info

# The port we listen on. MUST match BRUTOR_SKILL_RUNNER_LISTEN_ADDR + the
# compose service definition. Picked to NOT collide with the internal
# ports of the other services (8200, 8201).
EXPOSE 8210

# Compose drives readiness via a separate HEALTHCHECK; the Dockerfile
# version is a backup for non-compose runs.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8210/health || exit 1

ENTRYPOINT ["/app/brutor-skill-runner"]
