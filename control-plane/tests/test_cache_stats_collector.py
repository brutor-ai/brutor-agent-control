# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Control-plane cache stats collector tests.

These tests exercise the merge / aggregation logic directly. The
psycopg2 LISTEN socket loop is integration-tested separately against
a real DB in the trial bundle; here we cover the pure-Python
correctness paths that don't need a Postgres process.
"""

import json

import pytest

from app.services.system_ops.cache_stats_collector import (
    CacheStatsAggregate,
    EXPECTED_LAYERS,
    LayerAggregate,
    _merge_one_reply,
)


def _build_reply(
    *,
    correlation_id: str,
    replica_id: str,
    policy_hits: int = 0,
    policy_misses: int = 0,
    policy_entries: int = 0,
    group_hits: int = 0,
    group_misses: int = 0,
    group_entries: int = 0,
) -> str:
    return json.dumps(
        {
            "correlation_id": correlation_id,
            "replica_id": replica_id,
            "layers": {
                "policy": {
                    "hits": policy_hits,
                    "misses": policy_misses,
                    "entries": policy_entries,
                },
                "group": {
                    "hits": group_hits,
                    "misses": group_misses,
                    "entries": group_entries,
                },
            },
        }
    )


def _empty_totals() -> dict:
    return {layer: LayerAggregate() for layer in EXPECTED_LAYERS}


class TestMergeOneReply:
    def test_first_reply_accepted_and_counted(self):
        totals = _empty_totals()
        seen = set()
        payload = _build_reply(
            correlation_id="abc12345",
            replica_id="gw-1",
            policy_hits=10,
            policy_misses=2,
            policy_entries=8,
        )
        ok = _merge_one_reply(
            payload=payload,
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is True
        assert totals["policy"].hits == 10
        assert totals["policy"].misses == 2
        assert totals["policy"].entries == 8
        assert "gw-1" in seen

    def test_wrong_correlation_id_dropped(self):
        totals = _empty_totals()
        seen = set()
        payload = _build_reply(
            correlation_id="someone-elses-click",
            replica_id="gw-1",
            policy_hits=99,
        )
        ok = _merge_one_reply(
            payload=payload,
            correlation_id="my-click",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is False
        # Nothing leaked through.
        assert totals["policy"].hits == 0
        assert "gw-1" not in seen

    def test_duplicate_replica_id_dropped(self):
        totals = _empty_totals()
        seen = set()
        payload = _build_reply(
            correlation_id="abc12345",
            replica_id="gw-1",
            policy_hits=10,
        )
        # First arrival counts.
        _merge_one_reply(
            payload=payload,
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        # Second arrival from the same replica drops — no double count.
        ok = _merge_one_reply(
            payload=payload,
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is False
        assert totals["policy"].hits == 10

    def test_multiple_replicas_sum_per_layer(self):
        totals = _empty_totals()
        seen = set()
        for replica, hits in (("gw-1", 10), ("gw-2", 5), ("gw-3", 3)):
            payload = _build_reply(
                correlation_id="abc12345",
                replica_id=replica,
                policy_hits=hits,
                policy_entries=hits,
            )
            ok = _merge_one_reply(
                payload=payload,
                correlation_id="abc12345",
                layer_totals=totals,
                seen_replicas=seen,
            )
            assert ok is True
        assert totals["policy"].hits == 18
        assert totals["policy"].entries == 18
        assert seen == {"gw-1", "gw-2", "gw-3"}

    def test_malformed_json_dropped(self):
        totals = _empty_totals()
        seen = set()
        ok = _merge_one_reply(
            payload="{not-valid-json",
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is False
        assert totals["policy"].hits == 0

    def test_payload_missing_required_field_dropped(self):
        totals = _empty_totals()
        seen = set()
        # No `layers`. (A reply without `replica_id` is no longer malformed:
        # it is the one reply of a single-instance gateway, see
        # tests/test_cache_stats_single_instance.py.)
        payload = json.dumps(
            {
                "correlation_id": "abc12345",
                "replica_id": "gw-1",
            }
        )
        ok = _merge_one_reply(
            payload=payload,
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is False

    def test_unknown_layer_in_reply_silently_ignored(self):
        """Replica reports a future layer the control plane doesn't
        know yet (version skew). Known layers still merge."""
        totals = _empty_totals()
        seen = set()
        payload = json.dumps(
            {
                "correlation_id": "abc12345",
                "replica_id": "gw-1",
                "layers": {
                    "policy": {"hits": 1, "misses": 0, "entries": 0},
                    # Future layer the control plane doesn't track:
                    "embeddings_warm": {
                        "hits": 999,
                        "misses": 999,
                        "entries": 999,
                    },
                },
            }
        )
        ok = _merge_one_reply(
            payload=payload,
            correlation_id="abc12345",
            layer_totals=totals,
            seen_replicas=seen,
        )
        assert ok is True
        assert totals["policy"].hits == 1
        # The unknown layer never appears in the aggregate.
        assert "embeddings_warm" not in totals


class TestAggregate:
    def test_empty_aggregate_has_zero_responding_replicas(self):
        agg = CacheStatsAggregate()
        assert agg.responding_replicas == 0
        assert agg.replica_ids == []
        assert agg.layers == {}
        assert agg.timed_out is False  # default — only set when polling times out

    def test_layer_aggregate_default_zero(self):
        la = LayerAggregate()
        assert la.hits == 0
        assert la.misses == 0
        assert la.entries == 0


class TestExpectedLayers:
    def test_semantic_layer_intentionally_absent(self):
        # Semantic stats come from proxy_logs, not the NOTIFY fan-out.
        # Adding semantic here would shadow that path and confuse the
        # aggregator's "did anyone reply" check.
        assert "semantic" not in EXPECTED_LAYERS

    def test_layer_names_match_admin_ui_wire_contract(self):
        # Stable wire strings shared with gateway-core's
        # handle_cache_stats_request and the Admin UI's CacheLayer enum.
        assert EXPECTED_LAYERS == ("policy", "group", "tool", "response")
