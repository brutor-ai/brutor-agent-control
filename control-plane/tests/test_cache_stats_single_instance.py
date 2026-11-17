# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cache statistics from a gateway that is one instance.

A Community gateway answers the cache-statistics request with ONE reply that
names no instance: `{correlation_id, layers}`
(`brutor-gateway-core/src/services/system_notify.rs`, `CacheStatsReplyPayload`).
An Enterprise gateway with the Clustering extension answers once per replica,
each reply carrying `replica_id` (held to the Enterprise gateway's recording
by `enterprise/tests/test_cache_stats_single_instance.py`).

The collector required `replica_id`, so it dropped the Community reply as a
"shape mismatch" and a Community cache page said "No live counters".

The rule (`_merge_one_reply`): one collection is EITHER the single reply of a
gateway that names no instance, OR any number of replies from distinct named
replicas. The first reply accepted decides which; a reply of the other kind,
and a second unnamed reply, are dropped. Counting both kinds, or two unnamed
replies, could count one instance twice, and nothing in the replies can show
that it did not.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services.system_ops import caches
from app.services.system_ops.cache_stats_collector import (
    EXPECTED_LAYERS,
    CacheStatsAggregate,
    LayerAggregate,
    _merge_one_reply,
    finish_aggregate,
)
from app.services.system_ops.notify import CacheStatsReplyPayload
from tests.core_repo import core_repo_path

CID = "abc12345"


def _counters(n: int) -> dict:
    return {"hits": n, "misses": n + 1, "entries": n + 2}


def community_reply(n: int = 1, correlation_id: str = CID) -> str:
    """The Community gateway's reply: no `replica_id`, its three layers."""
    return json.dumps({
        "correlation_id": correlation_id,
        "layers": {"policy": _counters(n), "group": _counters(n), "tool": _counters(n)},
    })


def enterprise_reply(replica: str, n: int = 1, correlation_id: str = CID) -> str:
    """The Enterprise fixture's shape: `replica_id` and the four layers."""
    return json.dumps({
        "correlation_id": correlation_id,
        "replica_id": replica,
        "layers": {name: _counters(n) for name in ("group", "policy", "response", "tool")},
    })


class _Collection:
    """What `_collect_sync` keeps while it listens."""

    def __init__(self):
        self.totals = {layer: LayerAggregate() for layer in EXPECTED_LAYERS}
        self.seen: set = set()
        self.reported: set = set()

    def merge(self, payload: str) -> bool:
        return _merge_one_reply(
            payload=payload, correlation_id=CID, layer_totals=self.totals, seen_replicas=self.seen,
            reported_layers=self.reported,
        )

    def aggregate(self) -> CacheStatsAggregate:
        return finish_aggregate(CacheStatsAggregate(), self.totals, self.seen, self.reported)


def test_the_payload_takes_a_reply_that_names_no_instance():
    reply = CacheStatsReplyPayload.model_validate(json.loads(community_reply()))
    assert reply.replica_id is None
    named = CacheStatsReplyPayload.model_validate(json.loads(enterprise_reply("gw-1")))
    assert named.replica_id == "gw-1"
    # Still strict: an empty name is not a name, and nothing else is optional.
    with pytest.raises(Exception):
        CacheStatsReplyPayload.model_validate({"correlation_id": CID, "replica_id": "", "layers": {}})
    with pytest.raises(Exception):
        CacheStatsReplyPayload.model_validate({"correlation_id": CID})


def test_the_single_reply_is_the_one_instance_counted_once():
    c = _Collection()
    assert c.merge(community_reply(5)) is True
    agg = c.aggregate()
    assert agg.responding_replicas == 1
    assert agg.replica_ids == []
    assert agg.single_instance is True
    assert (agg.layers["policy"].hits, agg.layers["policy"].misses, agg.layers["policy"].entries) == (5, 6, 7)
    # The layer this gateway does not have is not reported as zeros.
    assert set(agg.layers) == {"policy", "group", "tool"}


def test_a_second_reply_without_a_name_is_dropped():
    c = _Collection()
    assert c.merge(community_reply(5)) is True
    assert c.merge(community_reply(5)) is False
    agg = c.aggregate()
    assert agg.responding_replicas == 1
    assert agg.layers["tool"].hits == 5


def test_a_named_reply_after_the_single_one_is_dropped():
    c = _Collection()
    assert c.merge(community_reply(5)) is True
    assert c.merge(enterprise_reply("gw-1", 9)) is False
    agg = c.aggregate()
    assert (agg.responding_replicas, agg.replica_ids, agg.single_instance) == (1, [], True)
    assert agg.layers["policy"].hits == 5 and "response" not in agg.layers


def test_an_unnamed_reply_after_named_ones_is_dropped():
    c = _Collection()
    assert c.merge(enterprise_reply("gw-1", 2)) is True
    assert c.merge(community_reply(5)) is False
    assert c.merge(enterprise_reply("gw-2", 3)) is True
    agg = c.aggregate()
    assert (agg.responding_replicas, agg.replica_ids, agg.single_instance) == (2, ["gw-1", "gw-2"], False)
    assert agg.layers["policy"].hits == 5  # 2 + 3, the unnamed 5 not added


def test_per_replica_merging_is_what_it_was():
    """The Enterprise shape: distinct replicas sum, a repeat is dropped, all
    four layers are there."""
    c = _Collection()
    assert c.merge(enterprise_reply("gw-1", 1)) is True
    assert c.merge(enterprise_reply("gw-2", 10)) is True
    assert c.merge(enterprise_reply("gw-1", 100)) is False
    agg = c.aggregate()
    assert (agg.responding_replicas, agg.replica_ids, agg.single_instance) == (2, ["gw-1", "gw-2"], False)
    assert set(agg.layers) == set(EXPECTED_LAYERS)
    for name in EXPECTED_LAYERS:
        layer = agg.layers[name]
        assert (layer.hits, layer.misses, layer.entries) == (11, 13, 15), name


def test_a_reply_for_another_request_does_not_decide_the_kind():
    c = _Collection()
    assert c.merge(community_reply(5, correlation_id="other000")) is False
    assert c.merge(enterprise_reply("gw-1", 2)) is True
    assert c.aggregate().replica_ids == ["gw-1"]


def test_nothing_accepted_is_no_layers():
    agg = _Collection().aggregate()
    assert (agg.responding_replicas, agg.replica_ids, agg.layers, agg.single_instance) == (0, [], {}, False)


def test_the_community_shape_is_the_gateways():
    """The Community reply above, against the gateway's own source. (The
    Enterprise reply is held to the Enterprise gateway's recording by the
    Enterprise half, `enterprise/tests/test_cache_stats_single_instance.py`:
    the core's Community tree has no recording of it.)"""
    core = core_repo_path()
    source = (core / "src" / "services" / "system_notify.rs").read_text(encoding="utf-8")
    start = source.index("pub struct CacheStatsReplyPayload {")
    struct = source[start:source.index("}", start)]
    fields = [line.split(":")[0].replace("pub ", "").strip() for line in struct.splitlines() if line.strip().startswith("pub ") and ":" in line]
    assert fields == ["correlation_id", "layers"]
    assert sorted(json.loads(community_reply())) == fields


# ---------------------------------------------------------------------------
# What the Caches page is told
# ---------------------------------------------------------------------------


def _rows(monkeypatch, aggregate: CacheStatsAggregate) -> dict:
    async def fake_collect(timeout_ms: int = 500):
        return aggregate

    import app.services.system_ops.cache_stats_collector as collector

    monkeypatch.setattr(collector, "collect_cache_stats", fake_collect)
    monkeypatch.setattr(caches, "_semantic_stats", lambda db, tenant_id: {"hit_count": 0, "miss_count": 0})
    monkeypatch.setattr(caches, "_last_flush_per_layer", lambda db, tenant_id: {})
    rows = asyncio.run(caches.list_cache_stats(db=None, tenant_id="t1"))
    return {row["layer"]: row for row in rows}


def test_the_single_instance_is_shown_without_replica_names(monkeypatch):
    c = _Collection()
    c.merge(community_reply(5))
    rows = _rows(monkeypatch, c.aggregate())
    for layer in ("policy", "group", "tool"):
        row = rows[layer]
        assert (row["hit_count"], row["miss_count"], row["item_count_approx"]) == (5, 6, 7), layer
        assert "replica" not in row["detail"].lower(), row["detail"]
        assert "No live counters" not in row["detail"]
    # The layer this gateway has no counters for says so; it is neither
    # zeros "aggregated" nor a fan-out that "timed out".
    response = rows["response"]
    assert response["item_count_approx"] is None
    assert "timed out" not in response["detail"] and "replica" not in response["detail"].lower()
    assert response["detail"] == "The gateway reports no counters for this layer."


def test_replicas_are_named_as_before(monkeypatch):
    c = _Collection()
    c.merge(enterprise_reply("gw-1", 1))
    c.merge(enterprise_reply("gw-2", 10))
    rows = _rows(monkeypatch, c.aggregate())
    for layer in EXPECTED_LAYERS:
        assert rows[layer]["detail"] == "Aggregated across 2 replica(s): gw-1, gw-2", layer
        assert rows[layer]["hit_count"] == 11


def test_no_answer_is_said_as_before(monkeypatch):
    rows = _rows(monkeypatch, CacheStatsAggregate(timed_out=True))
    for layer in EXPECTED_LAYERS:
        assert rows[layer]["detail"] == (
            "No live counters — gateway-core fan-out timed out. 0 replica(s) responding (none). "
            "Flush still works via NOTIFY."
        ), layer
