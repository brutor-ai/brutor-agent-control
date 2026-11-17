# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Wire contract: `POST /v1/internal/approvals/decide` on the gateway core

The control plane asks the core to decide a tool approval request on an
administrator's behalf. The core answers 200 with an
`outcome`; these are its values, and the two decisions the control plane may
send. Mirrored in the core (`routes/approvals.rs`, `decide_outcome`); a
value the control plane does not know is refused, never guessed at.
"""

#: The decisions (request member `decision`).
APPROVE = "approve"
REJECT = "reject"
DECISIONS = (APPROVE, REJECT)

#: Outcomes (response member `outcome`).
APPROVED = "approved"
REJECTED = "rejected"
#: Approve arrived past the deadline: the core expired the request and sealed it.
EXPIRED = "expired"
#: The request was not pending; the response's `status` says what it is.
ALREADY_RESOLVED = "already_resolved"
#: It stopped being pending between the read and the write (a concurrent decision).
NO_LONGER_PENDING = "no_longer_pending"
NOT_FOUND = "not_found"

DECIDED = (APPROVED, REJECTED)
OUTCOMES = (APPROVED, REJECTED, EXPIRED, ALREADY_RESOLVED, NO_LONGER_PENDING, NOT_FOUND)

#: What each decision yields when it is made.
DECISION_OUTCOME = {APPROVE: APPROVED, REJECT: REJECTED}
