# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RFC 6962 / RFC 9162 conformance for `app/services/evidence/merkle.py`

Two things are pinned:

1. The published RFC 9162 §2.1.4 vectors — roots and inclusion paths for
   the eight-leaf test tree. If these fail, the hash construction is
   wrong and every proof the Evidence Ledger serves is wrong with it.
2. The stored-node prover equals the recursive definition for every
   tree size up to 20 and every leaf. The core stores nodes with the
   append algorithm; the control plane reads them back with the range
   recursion; this is the test that says the two agree.
"""

from __future__ import annotations

import pytest

from app.services.evidence import merkle

# RFC 9162 §2.1.4 — the byte inputs and the expected results.
LEAVES = [
    b"",
    b"\x00",
    b"\x10",
    b"\x20\x21",
    b"\x30\x31",
    b"\x40\x41\x42\x43",
    b"\x50\x51\x52\x53\x54\x55\x56\x57",
    b"\x60\x61\x62\x63\x64\x65\x66\x67\x68\x69\x6a\x6b\x6c\x6d\x6e\x6f",
]
EMPTY_ROOT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ONE_LEAF_ROOT = "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
EIGHT_LEAF_ROOT = "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"
PATH_0_OF_8 = [
    "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7",
    "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
    "6b47aaf29ee3c2af9af889bc1fb9254dabd31177f16232dd6aab035ca39bf6e4",
]
PATH_5_OF_8 = [
    "bc1a0643b12e4d2d7c77918f44e0f4f79a838b6cf9ec5b5c283e1f4d88599e6b",
    "ca854ea128ed050b41b35ffc1b87b8eb2bde461e9e3b5596ece6b9d5975a0ae0",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
]


def _store(n: int):
    """Node store for the first n RFC leaves, built with the append algorithm."""
    nodes = {}
    for i in range(n):
        merkle.append_leaf(nodes, i, merkle.leaf_hash(LEAVES[i]))
    return nodes, merkle.store_getter(nodes)


class TestPublishedVectors:
    def test_empty_root(self):
        assert merkle.empty_root() == EMPTY_ROOT
        assert merkle.root_hash(lambda l, i: None, 0) == EMPTY_ROOT

    def test_one_leaf_root(self):
        _, get = _store(1)
        assert merkle.root_hash(get, 1) == ONE_LEAF_ROOT
        assert merkle.leaf_hash(LEAVES[0]) == ONE_LEAF_ROOT

    def test_eight_leaf_root(self):
        _, get = _store(8)
        assert merkle.root_hash(get, 8) == EIGHT_LEAF_ROOT

    def test_inclusion_path_leaf_0_of_8(self):
        _, get = _store(8)
        assert merkle.inclusion_path(get, 0, 8) == PATH_0_OF_8

    def test_inclusion_path_leaf_5_of_8(self):
        _, get = _store(8)
        assert merkle.inclusion_path(get, 5, 8) == PATH_5_OF_8

    def test_published_paths_verify(self):
        assert merkle.verify_inclusion(merkle.leaf_hash(LEAVES[0]), 0, 8, PATH_0_OF_8, EIGHT_LEAF_ROOT)
        assert merkle.verify_inclusion(merkle.leaf_hash(LEAVES[5]), 5, 8, PATH_5_OF_8, EIGHT_LEAF_ROOT)

    def test_reference_recursion_agrees_with_the_vectors(self):
        leaves = [merkle.leaf_hash(x) for x in LEAVES]
        assert merkle.reference_root(leaves) == EIGHT_LEAF_ROOT
        assert merkle.reference_path(leaves, 0) == PATH_0_OF_8
        assert merkle.reference_path(leaves, 5) == PATH_5_OF_8


class TestVerifierRejects:
    def test_wrong_leaf(self):
        assert not merkle.verify_inclusion(merkle.leaf_hash(LEAVES[1]), 0, 8, PATH_0_OF_8, EIGHT_LEAF_ROOT)

    def test_wrong_index(self):
        assert not merkle.verify_inclusion(merkle.leaf_hash(LEAVES[0]), 1, 8, PATH_0_OF_8, EIGHT_LEAF_ROOT)

    def test_wrong_root(self):
        assert not merkle.verify_inclusion(merkle.leaf_hash(LEAVES[0]), 0, 8, PATH_0_OF_8, EMPTY_ROOT)

    def test_truncated_and_padded_paths_fail_the_length_check(self):
        leaf = merkle.leaf_hash(LEAVES[0])
        assert not merkle.verify_inclusion(leaf, 0, 8, PATH_0_OF_8[:-1], EIGHT_LEAF_ROOT)
        assert not merkle.verify_inclusion(leaf, 0, 8, PATH_0_OF_8 + [EMPTY_ROOT], EIGHT_LEAF_ROOT)

    def test_index_out_of_range(self):
        assert not merkle.verify_inclusion(merkle.leaf_hash(LEAVES[0]), 8, 8, PATH_0_OF_8, EIGHT_LEAF_ROOT)
        assert not merkle.verify_inclusion(merkle.leaf_hash(LEAVES[0]), 0, 0, [], EMPTY_ROOT)

    def test_prover_refuses_out_of_range(self):
        _, get = _store(8)
        with pytest.raises(merkle.MerkleError):
            merkle.inclusion_path(get, 8, 8)

    def test_prover_reports_a_missing_node(self):
        nodes, _ = _store(8)
        del nodes[(2, 1)]
        with pytest.raises(merkle.MerkleError):
            merkle.inclusion_path(merkle.store_getter(nodes), 0, 8)


class TestStoredNodesEqualTheRecursion:
    """For every n ≤ 20 and every leaf: the node-store prover and the
    recursive definition produce the same path and root, every path
    verifies, and the store holds exactly the complete aligned subtrees."""

    @pytest.mark.parametrize("n", range(1, 21))
    def test_every_size(self, n):
        entries = [f"entry-{i}".encode() for i in range(n)]
        leaves = [merkle.leaf_hash(e) for e in entries]
        nodes = {}
        for i, leaf in enumerate(leaves):
            merkle.append_leaf(nodes, i, leaf)
        get = merkle.store_getter(nodes)

        root = merkle.root_hash(get, n)
        assert root == merkle.reference_root(leaves)

        for i in range(n):
            path = merkle.inclusion_path(get, i, n)
            assert path == merkle.reference_path(leaves, i)
            assert len(path) == merkle.path_length(i, n)
            assert merkle.verify_inclusion(leaves[i], i, n, path, root)

        # The store is exactly the set of complete aligned subtrees.
        expected = {
            (level, index)
            for level in range(n.bit_length() + 1)
            for index in range(n)
            if merkle.node_exists(level, index, n)
        }
        assert set(nodes) == expected

    def test_a_proof_against_an_earlier_size_still_verifies(self):
        entries = [f"entry-{i}".encode() for i in range(13)]
        leaves = [merkle.leaf_hash(e) for e in entries]
        nodes = {}
        for i, leaf in enumerate(leaves):
            merkle.append_leaf(nodes, i, leaf)
        get = merkle.store_getter(nodes)
        for size in range(1, 14):
            root = merkle.root_hash(get, size)
            for i in range(size):
                path = merkle.inclusion_path(get, i, size)
                assert merkle.verify_inclusion(leaves[i], i, size, path, root)
