# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A clock pinned across a local/UTC date boundary, for the rules about a day.

`pin(monkeypatch, *modules, utc=..., local_offset_hours=...)` replaces the
`date` and `datetime` names of each module (and of
`app.services.llm.availability`, whose `_today()` is the gateway's UTC day)
with classes whose `today()` / `now()` read the pinned instant: `now(tz)`
answers in `tz`, `now()` and `today()` answer in the host's local zone, set
`local_offset_hours` from UTC. Instances of the real classes are still
instances of the pinned ones, so `isinstance(value, date)` holds.
"""

from __future__ import annotations

from datetime import date as real_date, datetime as real_datetime, timedelta, timezone


def pin(monkeypatch, *modules, utc: real_datetime, local_offset_hours: int):
    from app.services.llm import availability

    utc = utc.replace(tzinfo=timezone.utc)
    local = utc.replace(tzinfo=None) + timedelta(hours=local_offset_hours)

    class _DateMeta(type):
        def __instancecheck__(cls, instance):
            return isinstance(instance, real_date)

    class _DatetimeMeta(type):
        def __instancecheck__(cls, instance):
            return isinstance(instance, real_datetime)

    class PinnedDate(real_date, metaclass=_DateMeta):
        @classmethod
        def today(cls):
            return local.date()

    class PinnedDatetime(real_datetime, metaclass=_DatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            return utc.astimezone(tz) if tz is not None else local

        @classmethod
        def today(cls):
            return local

        @classmethod
        def utcnow(cls):
            return utc.replace(tzinfo=None)

    for module in (availability, *modules):
        if hasattr(module, "date"):
            monkeypatch.setattr(module, "date", PinnedDate)
        if hasattr(module, "datetime"):
            monkeypatch.setattr(module, "datetime", PinnedDatetime)
    return utc.date(), local.date()
