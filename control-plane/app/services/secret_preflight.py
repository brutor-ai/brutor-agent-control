# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Boot-time secret-consistency preflight.

Every trial download mints a UNIQUE ``ENCRYPTION_KEY`` / ``JWT_SECRET`` /
``BRUTOR_GATEWAY_SERVICE_API_KEY`` (the trial download service stamps fresh
``secrets.token_urlsafe`` values into the bundle's ``.env``). If an
operator reuses a Postgres **volume** from a previous install while
running a new download, the persisted rows were encrypted/hashed under
the *old* secrets and silently stop validating under the new ones:

  * stored ``llm_model.api_key`` ciphertext → HMAC verification fails →
    the proxy forwards no key → upstream returns 401.
  * the seeded ``BRUTOR_GATEWAY_SERVICE_API_KEY`` row (planted into
    ``api_keys`` on first DB init, create-once) no longer matches the new
    env value → every internal control→proxy call returns
    ``401 Authentication required``.

Today that surfaces only as a flurry of per-request HMAC errors and
opaque 401s — hard to connect to the real cause. This preflight runs the
two cheap checks once at startup and, on mismatch, emits a single loud,
actionable line so the operator immediately knows to wipe the volume
(``docker compose down -v``) or restore the original secrets.

Non-fatal by design: the gateway still boots (an operator may be
mid-migration), but the cause is now in the first page of logs.
"""

from __future__ import annotations

from app.constants.proxy import gateway_service_api_key

import hashlib

from sqlalchemy.orm import Session

# Placeholder values shipped in the committed bundle .env — a real
# download replaces these. If the env still holds a placeholder we skip
# the corresponding check (nothing meaningful to verify yet).
_PLACEHOLDER_MARKERS = ("your-", "change-this", "changeme")

_REMEDIATION = (
    "This usually means you reused a Postgres volume from a PREVIOUS "
    "install while running a NEW trial download — each download mints "
    "unique ENCRYPTION_KEY / JWT_SECRET / BRUTOR_GATEWAY_SERVICE_API_KEY, so "
    "data encrypted/hashed under the old secrets cannot validate under "
    "the new ones. Fix: either wipe the stale volume "
    "(`docker compose down -v` then `up`) to re-seed under the current "
    ".env, OR restore the ORIGINAL secrets from your previous download's "
    ".env into this one."
)


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    return (not v) or any(m in v for m in _PLACEHOLDER_MARKERS)


def run_secret_consistency_preflight(db: Session, encryption_service, logger) -> None:
    """Verify the runtime secrets match the persisted database.

    Two independent, cheap checks; each failure logs one actionable ERROR.
    Never raises — a preflight must not crash startup.
    """
    try:
        _check_encryption_key(db, encryption_service, logger)
    except Exception as e:  # noqa: BLE001 — never crash startup
        logger.debug("ENCRYPTION_KEY preflight skipped", context={"error": str(e)})

    try:
        _check_health_check_api_key(db, logger)
    except Exception as e:  # noqa: BLE001
        logger.debug("BRUTOR_GATEWAY_SERVICE_API_KEY preflight skipped", context={"error": str(e)})


def _check_encryption_key(db: Session, encryption_service, logger) -> None:
    """Decrypt one stored model api_key; HMAC failure ⇒ ENCRYPTION_KEY
    no longer matches what encrypted the database."""
    if encryption_service is None:
        return
    from app.database.models.llm_model import LLModel

    row = (
        db.query(LLModel.id, LLModel.api_key)
        .filter(LLModel.api_key.isnot(None), LLModel.api_key != "")
        .first()
    )
    if row is None:
        # No encrypted credentials yet (fresh DB / keys not entered) —
        # nothing to verify; the first real decrypt will reveal a mismatch.
        return

    model_id, ciphertext = row
    try:
        encryption_service.decrypt(ciphertext)
        logger.info("Secret preflight: ENCRYPTION_KEY matches the database")
    except Exception:
        logger.error(
            "ENCRYPTION_KEY does not match the database — a stored model "
            "API key failed to decrypt (HMAC verification failed). Every "
            "encrypted credential (model/provider/OAuth keys) will fail, "
            "so upstream LLM calls return 401. " + _REMEDIATION,
            context={"probe_model_id": model_id},
        )


def _check_health_check_api_key(db: Session, logger) -> None:
    """Confirm BRUTOR_GATEWAY_SERVICE_API_KEY hashes to a live api_keys row.
    A miss ⇒ the control→proxy internal calls (health checks, embeddings,
    cache invalidation) will all 401."""
    key = gateway_service_api_key()
    if _is_placeholder(key):
        # Not configured (or still the placeholder) — internal health
        # checks aren't expected to authenticate; skip quietly.
        return

    from app.database.models.api_key import APIKey

    key_hash = hashlib.sha256(key.encode()).hexdigest()
    match = (
        db.query(APIKey.id)
        .filter(
            APIKey.key_hash == key_hash,
            APIKey.enabled.is_(True),
            APIKey.revoked.is_(False),
        )
        .first()
    )
    if match is None:
        logger.error(
            "BRUTOR_GATEWAY_SERVICE_API_KEY does not match any active row in "
            "the api_keys table — the seed plants this key on FIRST DB "
            "init only (create-once), so a stale volume + a new key never "
            "reconcile. Internal control→proxy calls (model health checks, "
            "embeddings, cache invalidation) will return "
            "401 Authentication required. " + _REMEDIATION,
        )
    else:
        logger.info("Secret preflight: BRUTOR_GATEWAY_SERVICE_API_KEY resolves to an api_keys row")
