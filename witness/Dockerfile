# syntax=docker/dockerfile:1
# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# =============================================================================
# Brutor Agent Control Witness — the packages' local RFC 9943 transparency
# service.
#
# PROVENANCE
#   Upstream:  capsule-anchor, Action State Group, Inc.
#              https://github.com/action-state-group/capsule-anchor
#   Licence:   Apache-2.0 (upstream LICENSE + NOTICE are copied into the image
#              at /usr/share/licenses/capsule-anchor/, as sections 4(a) and
#              4(d) of the licence require)
#   Pinned:    commit 0e040e819e770c598eb20f6cfe8cbacfea244006 (commit date: 2026-09-17,
#              "feat(public-log): wire the Rekor public-log rail into the
#              witness"). Built UNMODIFIED from that commit; the only Brutor
#              addition is entrypoint.py (creates the witness's own database
#              on an existing Postgres, then execs the upstream console
#              script). The build fails if the checkout is not exactly that
#              commit.
#   Deps:      pinned in constraints.txt (date resolved: 2026-09-19), pip
#              included. Their licences and licence texts, capsule-anchor's
#              too: THIRD_PARTY_LICENSES, generated in the build stage
#              (third_party_licenses.py) and shipped at
#              /usr/share/licenses/brutor/THIRD_PARTY_LICENSES.
#   Driver:    capsule-anchor's `postgres` extra is psycopg[binary], whose
#              wheel bundles libpq, OpenSSL and Kerberos without their
#              licence texts. The image installs capsule-anchor without the
#              extra and psycopg with its C extension (psycopg[c]) instead,
#              compiled in the build stage against Debian's libpq; at run
#              time it links the image's libpq5, a Debian package whose
#              copyright file ships in /usr/share/doc. The build fails if
#              the extension links anything else.
#
# To move the pin: change CAPSULE_ANCHOR_COMMIT, rebuild, re-run
# the evidence smoke test (it proves the receipts still verify under the Core
# Gateway's grade_scitt), refresh constraints.txt from
# `docker run --rm --entrypoint /opt/witness/bin/pip <image> freeze --all`
# (pip included), change the commit in third_party_licenses.toml's `sources`
# too, and run `python third_party_licenses.py regenerate community` in this
# directory.
#
# What the Core Gateway relies on (its src/services/evidence/witness.rs):
#   POST /transparency/register-statement {"signed_statement_b64"}
#     -> {"receipt_b64", "leaf_index", "tree_size", "entry_hash_scheme", ...}
#   The receipt is a COSE_Sign1 (alg EdDSA, vds 1 = RFC 9162 SHA-256) over
#   the detached CT root; leaf = SHA-256(0x00 || SHA-256(Sig_structure of the
#   submitted statement)). The authority key is Ed25519; its raw public key
#   (the value to pin) is served at GET /anchor/authority-pubkey.
#
# Built from witness/ in the public repository
# (https://github.com/brutor-ai/brutor-agent-control) and published as
# ghcr.io/brutor-ai/brutor-agent-control-witness:<platform version>. The
# image is one build, the Community one; the Enterprise package runs it too.
# =============================================================================

ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1 — fetch the pinned upstream source and build a venv.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

ARG CAPSULE_ANCHOR_REPO=https://github.com/action-state-group/capsule-anchor.git
ARG CAPSULE_ANCHOR_COMMIT=0e040e819e770c598eb20f6cfe8cbacfea244006

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git fetches the pinned source; gcc, libc6-dev and libpq-dev (pg_config and
# the libpq headers) compile psycopg's C extension. None of them reaches the
# runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates gcc libc6-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git init -q capsule-anchor \
 && cd capsule-anchor \
 && git remote add origin "${CAPSULE_ANCHOR_REPO}" \
 && git fetch -q --depth 1 origin "${CAPSULE_ANCHOR_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${CAPSULE_ANCHOR_COMMIT}" \
 && test -f LICENSE && test -f NOTICE

COPY constraints.txt /src/constraints.txt
RUN python -m venv /opt/witness \
 && /opt/witness/bin/pip install -c /src/constraints.txt --upgrade pip \
 && /opt/witness/bin/pip install -c /src/constraints.txt --no-binary psycopg-c "/src/capsule-anchor" "psycopg[c]"

# The driver's C extension links the system's libpq (no bundled copy beside
# it), psycopg uses it, and no binary driver is installed.
RUN ldd /opt/witness/lib/python3.12/site-packages/psycopg_c/pq*.so | grep -Eq 'libpq.so.5 => /(usr/)?lib/' \
 && ! ls -d /opt/witness/lib/python3.12/site-packages/psycopg_c.libs 2>/dev/null \
 && ! /opt/witness/bin/pip show psycopg-binary >/dev/null 2>&1 \
 && /opt/witness/bin/python -c "import psycopg; assert psycopg.pq.__impl__ == 'c', psycopg.pq.__impl__"

# The third-party licences: every package of /opt/witness, which ships
# unmodified, capsule-anchor included, with its version, its licence and the
# full licence texts. pip-licenses (pinned, with hashes) runs from a virtual
# environment of its own, which the runtime stage does not copy. The build
# fails on an unknown or disallowed licence, or on an environment that is not
# exactly constraints.txt and capsule-anchor.
COPY third_party_licenses.py third_party_licenses.toml third_party_licenses_tool.txt /src/
COPY licence-texts/ /src/licence-texts/
RUN python -m venv /opt/pip-licenses \
 && /opt/pip-licenses/bin/pip install --no-deps --require-hashes -r /src/third_party_licenses_tool.txt \
 && python /src/third_party_licenses.py generate community --site-packages /opt/witness/lib/python3.12/site-packages --pip-licenses /opt/pip-licenses/bin/pip-licenses --python /opt/witness/bin/python --out /src/THIRD_PARTY_LICENSES

# ---------------------------------------------------------------------------
# Stage 2 — runtime.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ARG PYTHON_IMAGE
ARG CAPSULE_ANCHOR_COMMIT=0e040e819e770c598eb20f6cfe8cbacfea244006

# What the image says about itself. The version label
# (`org.opencontainers.image.version`) is given by the build that knows it.
LABEL org.opencontainers.image.title="Brutor Agent Control Witness (Community Edition)"
LABEL org.opencontainers.image.description="Brutor Agent Control Witness: the local RFC 9943 transparency service for the evidence tree heads (capsule-anchor, packaged unmodified)"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.source="https://github.com/brutor-ai/brutor-agent-control"
LABEL org.opencontainers.image.vendor="Brutor AI Ltd"
LABEL ai.brutor.edition="community"
LABEL org.opencontainers.image.base.name="docker.io/library/${PYTHON_IMAGE}"
LABEL ai.brutor.upstream.source="https://github.com/action-state-group/capsule-anchor"
LABEL ai.brutor.upstream.revision="${CAPSULE_ANCHOR_COMMIT}"
LABEL ai.brutor.upstream.licenses="Apache-2.0"
LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/witness/bin:${PATH}" \
    CAPSULE_ANCHOR_HOST=0.0.0.0 \
    CAPSULE_ANCHOR_PORT=8000

# libpq5: the PostgreSQL client library the driver's C extension links.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m -s /bin/false witness

COPY --from=build /opt/witness /opt/witness
COPY --from=build /src/capsule-anchor/LICENSE /src/capsule-anchor/NOTICE /usr/share/licenses/capsule-anchor/
COPY NOTICE /usr/share/licenses/brutor-witness/NOTICE
# The third-party licences the build generated: where the label says, and
# beside the application's environment.
COPY --from=build /src/THIRD_PARTY_LICENSES /usr/share/licenses/brutor/THIRD_PARTY_LICENSES
COPY --from=build /src/THIRD_PARTY_LICENSES /opt/witness/THIRD_PARTY_LICENSES
COPY entrypoint.py /usr/local/bin/brutor-witness-entrypoint

USER witness
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["python", "/usr/local/bin/brutor-witness-entrypoint"]
