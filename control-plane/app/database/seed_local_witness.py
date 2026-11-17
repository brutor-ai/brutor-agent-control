# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Seed: the deployment's own witness

The trial bundle ships `brutor-witness` (packaged capsule-anchor, an RFC 9943
transparency service) next to the gateway. This seed registers it as a
**platform** witness flagged `same_operator=True`, so every tenant's tree
heads are registered with it and a head it alone receipts grades
`self_witnessed` — ordering is proven, independence is not claimed.

Driven by environment, inert when unset:

- `BRUTOR_LOCAL_WITNESS_URL` — the witness base URL (the core POSTs
  `{url}/transparency/register-statement`). Unset ⇒ nothing is seeded.
- `BRUTOR_LOCAL_WITNESS_PINNED_KEY` — optional raw Ed25519 public key (64 hex)
  to pin. When unset the key is read once from the witness's own
  `GET {url}/anchor/authority-pubkey` over the deployment's internal network
  (trust on first use). That is honest for *this* witness only: it is run by
  the same operator from the same compose file, which is exactly what
  `same_operator` records. An independent witness must always be pinned out of
  band through the admin API.
- `BRUTOR_LOCAL_WITNESS_OPERATOR` — optional operator label.

Create-once (see the seeds-are-create-once rule): looked up by the stable id
`LOCAL_WITNESS_ID`; an existing row is never touched, so an operator's
relabel, re-pin or disable in the Admin Console survives restarts. To retire
it, disable the row (deleting it lets the next boot re-create it while the
URL is still set) or unset `BRUTOR_LOCAL_WITNESS_URL`.

The pin fetch cannot block boot: the witness may still be starting when the
control plane seeds, so a missing key is retried on a daemon thread for up to
`FETCH_DEADLINE_SECS`, and the next boot tries again if that runs out.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional
from app.env_vars import (
    BRUTOR_LOCAL_WITNESS_OPERATOR,
    BRUTOR_LOCAL_WITNESS_PINNED_KEY,
    BRUTOR_LOCAL_WITNESS_URL,
)

logger = logging.getLogger(__name__)

LOCAL_WITNESS_ID = "wit-local-brutor-witness"
ENV_URL = BRUTOR_LOCAL_WITNESS_URL.name
ENV_PINNED_KEY = BRUTOR_LOCAL_WITNESS_PINNED_KEY.name
ENV_OPERATOR = BRUTOR_LOCAL_WITNESS_OPERATOR.name
DEFAULT_OPERATOR = BRUTOR_LOCAL_WITNESS_OPERATOR.default
PUBKEY_PATH = "/anchor/authority-pubkey"
FETCH_DEADLINE_SECS = 15 * 60
FETCH_INTERVAL_SECS = 5.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_thread_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def _configured_url() -> str:
    return (BRUTOR_LOCAL_WITNESS_URL.string() or "").rstrip("/")


def _normalise_key(raw: Optional[str]) -> Optional[str]:
    key = (raw or "").strip().lower()
    return key if _HEX64.match(key) else None


def fetch_pinned_key(url: str, timeout: float = 5.0) -> Optional[str]:
    """The witness's raw Ed25519 public key (hex), or None if unreachable."""
    import httpx

    try:
        resp = httpx.get(f"{url}{PUBKEY_PATH}", timeout=timeout)
        if resp.status_code != 200:
            return None
        body = resp.json()
    except Exception:  # noqa: BLE001 — unreachable/garbled means "not yet"
        return None
    if not isinstance(body, dict):
        return None
    return _normalise_key(body.get("pubkey_hex"))


def _insert(url: str, pinned_key: str) -> bool:
    """Create the row if absent. Returns True if this call created it."""
    from app.database import SessionLocal
    from app.database.models.resource_mixins import SYSTEM_ACTOR, acting_as
    from app.database.models.witness import SCOPE_PLATFORM, Witness

    operator = BRUTOR_LOCAL_WITNESS_OPERATOR.require()
    db = SessionLocal()
    try:
        with acting_as(SYSTEM_ACTOR):
            if db.get(Witness, LOCAL_WITNESS_ID) is not None:
                return False
            db.add(
                Witness(
                    id=LOCAL_WITNESS_ID,
                    scope=SCOPE_PLATFORM,
                    tenant_id=None,
                    kind="scitt",
                    url=url,
                    operator_label=operator,
                    pinned_key=pinned_key,
                    enabled=True,
                    same_operator=True,
                )
            )
            db.commit()
        logger.info(
            "Seeded the local platform witness %s (%s, same_operator, pinned %s…)",
            LOCAL_WITNESS_ID,
            url,
            pinned_key[:16],
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a concurrent worker may have won
        db.rollback()
        if db.get(Witness, LOCAL_WITNESS_ID) is None:
            logger.warning("Failed to seed the local witness (non-fatal): %s", exc)
        return False
    finally:
        db.close()


def _exists() -> bool:
    from app.database import SessionLocal
    from app.database.models.witness import Witness

    db = SessionLocal()
    try:
        return db.get(Witness, LOCAL_WITNESS_ID) is not None
    finally:
        db.close()


def _fetch_then_insert(url: str, deadline_s: float, interval_s: float) -> None:
    started = time.monotonic()
    while True:
        key = fetch_pinned_key(url)
        if key:
            _insert(url, key)
            return
        if time.monotonic() - started > deadline_s:
            logger.warning(
                "Local witness at %s did not answer %s within %.0fs; it is not "
                "registered yet and will be retried on the next control-plane boot. "
                "Tree heads stay standalone until then.",
                url,
                PUBKEY_PATH,
                deadline_s,
            )
            return
        time.sleep(interval_s)


def seed_local_witness(
    *,
    background: bool = True,
    deadline_s: float = FETCH_DEADLINE_SECS,
    interval_s: float = FETCH_INTERVAL_SECS,
) -> Optional[threading.Thread]:
    """Register the deployment's own witness if configured and absent.

    Returns the background thread when the pin has to be fetched (so a test
    can join it), otherwise None.
    """
    global _thread
    url = _configured_url()
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        logger.warning("%s must be an http(s) URL; got %r — not seeding", ENV_URL, url)
        return None
    if _exists():
        return None

    raw_pin = BRUTOR_LOCAL_WITNESS_PINNED_KEY.string()
    if raw_pin and raw_pin.strip():
        key = _normalise_key(raw_pin)
        if key is None:
            logger.warning(
                "%s is not a 64-hex Ed25519 public key — not seeding the local witness",
                ENV_PINNED_KEY,
            )
            return None
        _insert(url, key)
        return None

    if not background:
        _fetch_then_insert(url, deadline_s, interval_s)
        return None
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(
            target=_fetch_then_insert,
            args=(url, deadline_s, interval_s),
            daemon=True,
            name="seed-local-witness",
        )
        _thread.start()
        return _thread


__all__ = [
    "LOCAL_WITNESS_ID",
    "ENV_URL",
    "ENV_PINNED_KEY",
    "ENV_OPERATOR",
    "fetch_pinned_key",
    "seed_local_witness",
]
