# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RFC 6962 Merkle trees over a stored node set

The tenant log is an append-only binary Merkle tree in the RFC 6962 shape:

    leaf  = SHA-256(0x00 ‖ entry)
    node  = SHA-256(0x01 ‖ left ‖ right)
    MTH(D[n]) : k = largest power of two < n;
                node(MTH(D[0:k]), MTH(D[k:n]))
    MTH({})   = SHA-256("")

The core appends leaves and stores every complete aligned subtree hash as
a `(level, index)` node. A node exists iff `(index + 1) * 2**level <=
tree_size`. That set is enough to serve any range hash — the recursion
above only ever asks for the hash of a range that is either one stored
node or splits at a power of two into ranges that are — so inclusion
proofs and roots are computed from the store without ever re-reading a
statement. The RFC 9162 §2.1.3.2 verification algorithm is included so the
control plane can check a proof it just served, and so tests can pin the
prover and the verifier against each other and against the published
vectors.

Hashes travel as lowercase hex strings, which is how the core stores them.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Dict, List, Mapping, Optional, Tuple

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

#: How a caller lends this module its node store: `(level, index) -> hex`.
NodeGetter = Callable[[int, int], Optional[str]]


class MerkleError(ValueError):
    """The node store cannot answer — a node is missing or the ask is out of range."""


def store_getter(nodes: Mapping[Tuple[int, int], str]) -> NodeGetter:
    """A NodeGetter over an in-memory `{(level, index): hex}` store.

    Not `dict.get` — that takes `(key, default)`, so `nodes.get(0, 0)` would
    answer the default rather than node (0, 0).
    """
    return lambda level, index: nodes.get((level, index))


def leaf_hash(entry: bytes) -> str:
    return hashlib.sha256(LEAF_PREFIX + entry).hexdigest()


def node_hash(left_hex: str, right_hex: str) -> str:
    return hashlib.sha256(
        NODE_PREFIX + bytes.fromhex(left_hex) + bytes.fromhex(right_hex)
    ).hexdigest()


def empty_root() -> str:
    return hashlib.sha256(b"").hexdigest()


def _largest_power_of_two_below(n: int) -> int:
    """k with k < n and k the largest power of two — RFC 6962's split."""
    if n < 2:
        raise MerkleError(f"no power of two below {n}")
    return 1 << ((n - 1).bit_length() - 1)


def node_exists(level: int, index: int, tree_size: int) -> bool:
    """A stored node covers a complete aligned subtree of the current tree."""
    return (index + 1) * (1 << level) <= tree_size


# ---------------------------------------------------------------------------
# The append algorithm — what the core runs; used by tests and to size the
# node set a log of `n` leaves must have.
# ---------------------------------------------------------------------------

def append_leaf(nodes: Dict[Tuple[int, int], str], index: int, leaf_hex: str) -> None:
    """Append leaf `index` to a node store, folding completed subtrees up.

    put (0, i); while i is odd: parent = node(get(level, i-1), h);
    level += 1; i >>= 1; put. Exactly the core's algorithm, so the Python
    store and the Postgres store are the same set.
    """
    level, i, h = 0, index, leaf_hex
    nodes[(level, i)] = h
    while i & 1:
        left = nodes.get((level, i - 1))
        if left is None:
            raise MerkleError(f"missing left sibling at level {level} index {i - 1}")
        h = node_hash(left, h)
        level += 1
        i >>= 1
        nodes[(level, i)] = h


# ---------------------------------------------------------------------------
# Range hashes and proofs from the store
# ---------------------------------------------------------------------------

def range_hash(get: NodeGetter, start: int, end: int) -> str:
    """MTH(D[start:end]) from stored nodes.

    Every range the RFC 6962 recursion produces is either a complete
    aligned subtree (one stored node) or splits at the largest power of
    two below its size into a stored left node and a smaller right range.
    """
    size = end - start
    if size <= 0:
        raise MerkleError(f"empty range [{start}, {end})")
    if size & (size - 1) == 0 and start % size == 0:
        level = size.bit_length() - 1
        h = get(level, start >> level)
        if h is None:
            raise MerkleError(f"missing node at level {level} index {start >> level}")
        return h
    k = _largest_power_of_two_below(size)
    return node_hash(range_hash(get, start, start + k), range_hash(get, start + k, end))


def root_hash(get: NodeGetter, tree_size: int) -> str:
    if tree_size == 0:
        return empty_root()
    return range_hash(get, 0, tree_size)


def inclusion_path(get: NodeGetter, index: int, tree_size: int) -> List[str]:
    """RFC 6962 §2.1.1 PATH(m, D[n]), served from the store.

    PATH(0, {d0}) = {}
    PATH(m, D[n]) = PATH(m, D[0:k]) : MTH(D[k:n])       for m < k
                  = PATH(m - k, D[k:n]) : MTH(D[0:k])   for m >= k
    """
    if not 0 <= index < tree_size:
        raise MerkleError(f"leaf {index} is not in a tree of size {tree_size}")
    return _path(get, index, 0, tree_size)


def _path(get: NodeGetter, m: int, start: int, end: int) -> List[str]:
    n = end - start
    if n == 1:
        return []
    k = _largest_power_of_two_below(n)
    if m - start < k:
        return _path(get, m, start, start + k) + [range_hash(get, start + k, end)]
    return _path(get, m, start + k, end) + [range_hash(get, start, start + k)]


def path_length(index: int, tree_size: int) -> int:
    """How many siblings PATH(index, D[tree_size]) has — the expected-length
    check a verifier runs before folding anything."""
    if not 0 <= index < tree_size:
        raise MerkleError(f"leaf {index} is not in a tree of size {tree_size}")
    length, m, n = 0, index, tree_size
    while n > 1:
        k = _largest_power_of_two_below(n)
        if m < k:
            n = k
        else:
            m, n = m - k, n - k
        length += 1
    return length


# ---------------------------------------------------------------------------
# Verification — RFC 9162 §2.1.3.2
# ---------------------------------------------------------------------------

def verify_inclusion(
    leaf_hex: str, index: int, tree_size: int, path: List[str], root_hex: str
) -> bool:
    """True iff `path` proves `leaf_hex` is leaf `index` of a tree of
    `tree_size` leaves whose root is `root_hex`.

    The expected-path-length check runs first: a path of the wrong length
    can never fold to the root, and rejecting it up front means the fold
    below never has to reason about a truncated or padded proof.
    """
    if index < 0 or tree_size <= 0 or index >= tree_size:
        return False
    if len(path) != path_length(index, tree_size):
        return False

    fn, sn = index, tree_size - 1
    r = leaf_hex
    for p in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root_hex


# ---------------------------------------------------------------------------
# Reference (recursive, over leaves) — for tests only
# ---------------------------------------------------------------------------

def reference_root(leaves: List[str]) -> str:
    """MTH over leaf hashes by the RFC 6962 recursion, with no store."""
    n = len(leaves)
    if n == 0:
        return empty_root()
    if n == 1:
        return leaves[0]
    k = _largest_power_of_two_below(n)
    return node_hash(reference_root(leaves[:k]), reference_root(leaves[k:]))


def reference_path(leaves: List[str], m: int) -> List[str]:
    n = len(leaves)
    if n == 1:
        return []
    k = _largest_power_of_two_below(n)
    if m < k:
        return reference_path(leaves[:k], m) + [reference_root(leaves[k:])]
    return reference_path(leaves[k:], m - k) + [reference_root(leaves[:k])]


__all__ = [
    "MerkleError",
    "NodeGetter",
    "store_getter",
    "leaf_hash",
    "node_hash",
    "empty_root",
    "node_exists",
    "append_leaf",
    "range_hash",
    "root_hash",
    "inclusion_path",
    "path_length",
    "verify_inclusion",
    "reference_root",
    "reference_path",
]
