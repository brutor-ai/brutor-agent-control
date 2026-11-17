# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Brutor Agent Control, control plane — COMMUNITY Edition image
#
#     docker build -t control-community .
#
# The build context (`.dockerignore`) has no `enterprise/` directory: this
# image cannot contain Enterprise code. It installs the Community package's
# dependencies only (`pyproject.toml`), ships the sources as they are
# (no bytecode-only step), installs no cloud CLI, and runs
# the Community application factory. It needs no licence key.
#
# The Enterprise Edition's image is `enterprise/Dockerfile`.
# =============================================================================

# =============================================================================
# Stage 1: Builder - Install dependencies
# =============================================================================
FROM python:3.13-slim AS builder

WORKDIR /build

# Build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# The package definition (and the README it names) and the lock. No source
# yet, so this layer changes only with the dependencies.
COPY pyproject.toml README.md constraints.txt ./

# Every package comes from the lock (`constraints.txt`): the environment the
# image ships is exactly that list.
RUN pip install --no-cache-dir -c constraints.txt --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -e . -c constraints.txt --no-binary psycopg2

# The PostgreSQL driver is compiled here against Debian's libpq (libpq-dev,
# gcc), never taken from a wheel (`--no-binary psycopg2`): the
# psycopg2-binary wheel bundles libpq, OpenSSL and Kerberos without their
# licence texts. At run time it links the image's libpq5 and what that
# depends on, Debian packages whose copyright files ship in
# /usr/share/doc. The build fails if it links anything bundled.
RUN ldd /usr/local/lib/python3.13/site-packages/psycopg2/_psycopg*.so | grep -Eq 'libpq.so.5 => /(usr/)?lib/' && \
    ! ls -d /usr/local/lib/python3.13/site-packages/psycopg2*.libs 2>/dev/null

# The third-party licences: every package of that environment, which ships
# unmodified, with its licence and full licence texts. pip-licenses
# (pinned, with hashes) runs from a virtual environment of its own, which the
# final stage does not copy. The build fails on an unknown or disallowed
# licence, or on an environment that is not exactly the lock.
COPY scripts/third_party_licenses.py scripts/third_party_licenses.toml scripts/third_party_licenses_tool.txt scripts/
COPY scripts/licence-texts/ scripts/licence-texts/
RUN python -m venv /opt/pip-licenses && \
    /opt/pip-licenses/bin/pip install --no-cache-dir --no-deps --require-hashes -r scripts/third_party_licenses_tool.txt && \
    python scripts/third_party_licenses.py generate community --site-packages /usr/local/lib/python3.13/site-packages --pip-licenses /opt/pip-licenses/bin/pip-licenses --out /build/THIRD_PARTY_LICENSES

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.13-slim

# Platform version baked into the image. Read by /health and stamped into
# schema_version.app_version on boot (app/database/__init__.py). CI passes
# the release tag as `--build-arg BRUTOR_VERSION=<semver>`; a local build
# reports "dev". The compose environment may override it at run time.
ARG BRUTOR_VERSION=dev
ENV BRUTOR_VERSION=${BRUTOR_VERSION}

# The image is `ghcr.io/brutor-ai/brutor-agent-control-control-plane`; the
# names and these labels are one list, `scripts/image-names.txt`, and
# `tests/test_image_names.py` holds this file to it. The version and revision
# labels (`org.opencontainers.image.version`, `.revision`) are given by the
# build that knows them.
LABEL org.opencontainers.image.title="Brutor Agent Control Control Plane (Community Edition)"
LABEL org.opencontainers.image.description="Brutor Agent Control Control Plane, Community Edition: the admin API behind the Admin Console, owner of the database schema"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"
LABEL org.opencontainers.image.vendor="Brutor AI Ltd"
LABEL ai.brutor.edition="community"
LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime dependencies
#   - libpq5    — Postgres client libs
#   - curl      — the health check
RUN apt-get update && apt-get install -y \
    libpq5 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r brutor && \
    useradd -r -g brutor -u 1000 -m -s /bin/bash brutor

WORKDIR /app

# Python dependencies from the builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# The application: sources, as they are in the repository.
COPY --chown=brutor:brutor . .

# The third-party licences the build generated, where the label says and
# beside the application.
COPY --from=builder /build/THIRD_PARTY_LICENSES /usr/share/licenses/brutor/THIRD_PARTY_LICENSES
COPY --from=builder /build/THIRD_PARTY_LICENSES /app/THIRD_PARTY_LICENSES

# Install the package where it is: its dependencies came from the builder;
# this points the package and its console script
# (`brutor-agent-control-plane`) at /app.
RUN pip install --no-cache-dir --no-deps -e .

RUN mkdir -p /app/logs /app/data && \
    chown -R brutor:brutor /app

USER brutor

# Must match `gunicorn.conf.py` (BRUTOR_CONTROL_PLANE_LISTEN_ADDR default 0.0.0.0:5050).
EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:5050/health || exit 1

# The edition is the application factory named here: Community.
CMD ["gunicorn", "app.app:create_app", "--config", "gunicorn.conf.py"]
