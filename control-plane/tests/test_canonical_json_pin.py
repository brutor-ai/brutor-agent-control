# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Cross-repo pin for the discovery/card canonical JSON: the bytes that are
signed here (`app/services/a2a/card_signing.py`), in the Discovery SDK
(`brutor_discovery/event.py`) and in Scout (`src/event.rs`) must be
identical or signatures fail to verify. Same fixture in all three tests.
"""

from app.services.a2a.card_signing import canonical_json

FIXTURE = {"b": 1, "a": "ü", "c": [1.5, {"z": None, "y": True}], "d": ""}
EXPECTED = '{"a":"ü","b":1,"c":[1.5,{"y":true,"z":null}],"d":""}'.encode("utf-8")


def test_canonical_bytes_match_the_sdk_and_scout():
    assert canonical_json(FIXTURE) == EXPECTED
