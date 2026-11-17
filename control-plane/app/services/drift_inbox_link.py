# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A drift finding and its Assurance Inbox item are one decision.

The closed loop raises every drift finding into the Inbox under a
fingerprint of system, drift class and metric. A person who closes the finding
on the Drift tab — kind plus note — has reviewed it; leaving the Inbox item
open asked for the same decision twice, and the two surfaces disagreed (the
system's header read "Drift 0 open" while the Inbox listed the finding as open).

This module is the one place the two are tied together:

* `drift_inbox_fingerprint` — the fingerprint both the emitter and the closer
  use, so they cannot drift apart.
* `disposition_inbox_item` — called by `resolve_finding` in the resolution's
  own transaction. It resolves the open Inbox item with the disposition the
  kind maps to, the same note and the same person.

What it does not do:

* An `auto_cleared` finding leaves its item open: nobody reviewed it, and the
  Inbox is where review is owed.
* While another finding with the same fingerprint is still open, the item
  stays open: there is still something to decide.
* Classes that raise their own, finer-grained items (`contract`, `agent`) are
  not touched: their items are per term or per release, not per finding.
* The reverse direction does not exist. An Inbox disposition cannot close a
  drift finding, because a close needs a kind and a kind has an effect on
  detection (an epoch or an incident window).
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.constants.drift_resolution import (
    KIND_ACCEPTED_CHANGE,
    KIND_CAUSE_FIXED,
    KIND_FALSE_POSITIVE,
)
from app.database.models.ai_system_drift import (
    DRIFT_ACKNOWLEDGED,
    DRIFT_ACTIVE,
    DRIFT_AGENT,
    DRIFT_CONTRACT,
    AISystemDriftEvent,
)
from app.database.models.assurance_inbox import (
    DISPOSITION_EXPECTED,
    DISPOSITION_FALSE_POSITIVE,
    DISPOSITION_FIXED,
    INBOX_STATE_RESOLVED,
    AssuranceInboxItem,
)

INBOX_CATEGORY_DRIFT = "drift"

#: Drift classes whose emitter raises its own inbox item (with a finer
#: fingerprint than the drift event's), so the closed loop must not raise one
#: and a resolution has no item of its own to disposition.
SELF_RAISED_DRIFT_CLASSES = frozenset({DRIFT_CONTRACT, DRIFT_AGENT})

#: What a human close says about the finding, in the Inbox's vocabulary.
DISPOSITION_FOR_KIND = {
    KIND_ACCEPTED_CHANGE: DISPOSITION_EXPECTED,
    KIND_CAUSE_FIXED: DISPOSITION_FIXED,
    KIND_FALSE_POSITIVE: DISPOSITION_FALSE_POSITIVE,
}


def drift_inbox_fingerprint(ai_system_id: str, drift_class: str, metric_key: str) -> str:
    """The Inbox fingerprint of a drift finding: recurrences on one metric of
    one system are one item."""
    return f"drift:{ai_system_id}:{drift_class}:{metric_key}"


def disposition_inbox_item(
    db: Session,
    event: AISystemDriftEvent,
    *,
    kind: str,
    by: Optional[str],
    note: str,
) -> Optional[AssuranceInboxItem]:
    """Resolve the Inbox item of a drift finding a person just closed.

    Runs inside the resolution's transaction (flushes nothing, commits
    nothing). Returns the item it resolved, else None: no mapped disposition,
    a self-raised class, no open item, or a sibling finding still open.
    """
    # Imported here: the inbox service imports nothing from drift, but the
    # response scheduler imports both, and this keeps the module graph flat.
    from app.services.assurance_inbox import AssuranceInboxService

    disposition = DISPOSITION_FOR_KIND.get(kind)
    if disposition is None or event.drift_class in SELF_RAISED_DRIFT_CLASSES:
        return None

    siblings_open = (
        db.query(AISystemDriftEvent.id)
        .filter(
            AISystemDriftEvent.tenant_id == event.tenant_id,
            AISystemDriftEvent.ai_system_id == event.ai_system_id,
            AISystemDriftEvent.drift_class == event.drift_class,
            AISystemDriftEvent.metric_key == event.metric_key,
            AISystemDriftEvent.id != event.id,
            AISystemDriftEvent.status.in_((DRIFT_ACTIVE, DRIFT_ACKNOWLEDGED)),
        )
        .first()
    )
    if siblings_open is not None:
        return None

    item = (
        db.query(AssuranceInboxItem)
        .filter(
            AssuranceInboxItem.tenant_id == event.tenant_id,
            AssuranceInboxItem.category == INBOX_CATEGORY_DRIFT,
            AssuranceInboxItem.fingerprint
            == drift_inbox_fingerprint(event.ai_system_id, event.drift_class, event.metric_key),
            AssuranceInboxItem.state != INBOX_STATE_RESOLVED,
        )
        .with_for_update()
        .first()
    )
    if item is None:
        return None

    return AssuranceInboxService().resolve(
        db,
        item,
        admin=by or "unknown",
        disposition=disposition,
        note=note,
        detail={"via": "drift_resolution", "drift_event_id": event.id, "resolution_kind": kind},
    )
