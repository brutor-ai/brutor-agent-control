// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! RFC 6962 / RFC 9162 Merkle tree primitives over in-memory leaf hashes.
//!
//! * leaf  = SHA-256(0x00 ‖ entry)
//! * node  = SHA-256(0x01 ‖ left ‖ right)
//! * empty = SHA-256("")
//! * split = largest power of two strictly less than n
//!
//! Proof *generation* here is the recursive RFC 6962 definition over a
//! slice of leaf hashes; it is what tests and the offline verifier use.
//! The persistent log (`brutor-ctlog`) generates the same proofs from a
//! node store. Proof *verification* is the iterative RFC 9162 algorithm
//! with the expected-path-length check done before any hashing, and a
//! `MAX_TREE_SIZE` of 2^62 so an int64 verifier agrees accept-for-accept.

use sha2::{Digest, Sha256};

pub type Hash = [u8; 32];

pub const MAX_TREE_SIZE: u64 = 1 << 62;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum MerkleError {
    #[error("leaf index {index} out of range for tree size {tree_size}")]
    IndexOutOfRange { index: u64, tree_size: u64 },
    #[error("tree size {0} exceeds MAX_TREE_SIZE")]
    TreeTooLarge(u64),
    #[error("inclusion path length {got} does not match expected {expected}")]
    PathLength { got: usize, expected: usize },
    #[error("computed root does not match")]
    RootMismatch,
    #[error("consistency proof malformed")]
    Consistency,
}

pub fn leaf_hash(entry: &[u8]) -> Hash {
    let mut h = Sha256::new();
    h.update([0x00u8]);
    h.update(entry);
    h.finalize().into()
}

pub fn node_hash(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update([0x01u8]);
    h.update(left);
    h.update(right);
    h.finalize().into()
}

pub fn empty_root() -> Hash {
    Sha256::digest(b"").into()
}

/// Largest power of two strictly less than `n` (n ≥ 2).
pub fn split_point(n: u64) -> u64 {
    debug_assert!(n >= 2);
    1u64 << (63 - (n - 1).leading_zeros())
}

/// MTH over already-hashed leaves.
pub fn tree_hash(leaves: &[Hash]) -> Hash {
    match leaves.len() {
        0 => empty_root(),
        1 => leaves[0],
        n => {
            let k = split_point(n as u64) as usize;
            node_hash(&tree_hash(&leaves[..k]), &tree_hash(&leaves[k..]))
        }
    }
}

/// RFC 6962 §2.1.1 PATH(m, D[n]).
pub fn inclusion_path(index: u64, leaves: &[Hash]) -> Vec<Hash> {
    fn path(m: u64, d: &[Hash], out: &mut Vec<Hash>) {
        let n = d.len() as u64;
        if n <= 1 {
            return;
        }
        let k = split_point(n);
        if m < k {
            path(m, &d[..k as usize], out);
            out.push(tree_hash(&d[k as usize..]));
        } else {
            path(m - k, &d[k as usize..], out);
            out.push(tree_hash(&d[..k as usize]));
        }
    }
    let mut out = Vec::new();
    path(index, leaves, &mut out);
    out
}

/// Number of siblings an inclusion proof for `index` in a tree of
/// `tree_size` must carry. Checked before any hashing so a truncated
/// or padded path is rejected structurally.
pub fn expected_path_len(tree_size: u64, index: u64) -> usize {
    let mut n = tree_size;
    let mut m = index;
    let mut len = 0usize;
    while n > 1 {
        let k = split_point(n);
        if m < k {
            n = k;
        } else {
            n -= k;
            m -= k;
        }
        len += 1;
    }
    len
}

/// RFC 9162 §2.1.3.2 — iterative inclusion verification.
pub fn root_from_inclusion(
    leaf: &Hash,
    index: u64,
    tree_size: u64,
    path: &[Hash],
) -> Result<Hash, MerkleError> {
    if tree_size > MAX_TREE_SIZE {
        return Err(MerkleError::TreeTooLarge(tree_size));
    }
    if index >= tree_size {
        return Err(MerkleError::IndexOutOfRange { index, tree_size });
    }
    let expected = expected_path_len(tree_size, index);
    if path.len() != expected {
        return Err(MerkleError::PathLength {
            got: path.len(),
            expected,
        });
    }
    let mut fnode = index;
    let mut snode = tree_size - 1;
    let mut r = *leaf;
    for p in path {
        if snode == 0 {
            return Err(MerkleError::PathLength {
                got: path.len(),
                expected,
            });
        }
        if fnode & 1 == 1 || fnode == snode {
            r = node_hash(p, &r);
            while fnode & 1 == 0 && fnode != 0 {
                fnode >>= 1;
                snode >>= 1;
            }
        } else {
            r = node_hash(&r, p);
        }
        fnode >>= 1;
        snode >>= 1;
    }
    if snode != 0 {
        return Err(MerkleError::PathLength {
            got: path.len(),
            expected,
        });
    }
    Ok(r)
}

pub fn verify_inclusion(
    leaf: &Hash,
    index: u64,
    tree_size: u64,
    path: &[Hash],
    root: &Hash,
) -> Result<(), MerkleError> {
    let computed = root_from_inclusion(leaf, index, tree_size, path)?;
    if &computed == root {
        Ok(())
    } else {
        Err(MerkleError::RootMismatch)
    }
}

/// RFC 6962 §2.1.2 PROOF(m, D[n]) — consistency between the tree of the
/// first `m` leaves and the tree of all `n` leaves.
pub fn consistency_proof(m: u64, leaves: &[Hash]) -> Vec<Hash> {
    fn subproof(m: u64, d: &[Hash], b: bool, out: &mut Vec<Hash>) {
        let n = d.len() as u64;
        if m == n {
            if !b {
                out.push(tree_hash(d));
            }
            return;
        }
        let k = split_point(n);
        if m <= k {
            subproof(m, &d[..k as usize], b, out);
            out.push(tree_hash(&d[k as usize..]));
        } else {
            subproof(m - k, &d[k as usize..], false, out);
            out.push(tree_hash(&d[..k as usize]));
        }
    }
    let n = leaves.len() as u64;
    if m == 0 || m > n {
        return Vec::new();
    }
    let mut out = Vec::new();
    subproof(m, leaves, true, &mut out);
    out
}

/// RFC 9162 §2.1.4.2 — iterative consistency verification.
pub fn verify_consistency(
    first_size: u64,
    first_root: &Hash,
    second_size: u64,
    second_root: &Hash,
    proof: &[Hash],
) -> Result<(), MerkleError> {
    if second_size > MAX_TREE_SIZE {
        return Err(MerkleError::TreeTooLarge(second_size));
    }
    if first_size == 0 || first_size > second_size {
        return Err(MerkleError::Consistency);
    }
    if first_size == second_size {
        return if proof.is_empty() && first_root == second_root {
            Ok(())
        } else {
            Err(MerkleError::Consistency)
        };
    }
    if proof.is_empty() {
        return Err(MerkleError::Consistency);
    }
    let mut path: Vec<Hash> = Vec::with_capacity(proof.len() + 1);
    if first_size.is_power_of_two() {
        path.push(*first_root);
    }
    path.extend_from_slice(proof);

    let mut fnode = first_size - 1;
    let mut snode = second_size - 1;
    while fnode & 1 == 1 {
        fnode >>= 1;
        snode >>= 1;
    }
    let mut fr = path[0];
    let mut sr = path[0];
    for c in &path[1..] {
        if snode == 0 {
            return Err(MerkleError::Consistency);
        }
        if fnode & 1 == 1 || fnode == snode {
            fr = node_hash(c, &fr);
            sr = node_hash(c, &sr);
            while fnode & 1 == 0 && fnode != 0 {
                fnode >>= 1;
                snode >>= 1;
            }
        } else {
            sr = node_hash(&sr, c);
        }
        fnode >>= 1;
        snode >>= 1;
    }
    if snode == 0 && &fr == first_root && &sr == second_root {
        Ok(())
    } else {
        Err(MerkleError::Consistency)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leaves(n: usize) -> Vec<Hash> {
        (0..n).map(|i| leaf_hash(&[i as u8])).collect()
    }

    // RFC 6962 §2.1 / RFC 9162 test vectors (empty tree and the 8-leaf
    // tree over the inputs "", 0x00, 0x10, 0x2021, 0x30313233, ...).
    fn rfc_leaves() -> Vec<Hash> {
        let inputs: [&[u8]; 8] = [
            &[],
            &[0x00],
            &[0x10],
            &[0x20, 0x21],
            &[0x30, 0x31],
            &[0x40, 0x41, 0x42, 0x43],
            &[0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57],
            &[
                0x60, 0x61, 0x62, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x6b, 0x6c, 0x6d,
                0x6e, 0x6f,
            ],
        ];
        inputs.iter().map(|i| leaf_hash(i)).collect()
    }

    #[test]
    fn rfc_9162_published_roots() {
        assert_eq!(
            hex::encode(empty_root()),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        let l = rfc_leaves();
        assert_eq!(
            hex::encode(tree_hash(&l[..1])),
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
        );
        assert_eq!(
            hex::encode(tree_hash(&l[..8])),
            "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"
        );
    }

    #[test]
    fn rfc_9162_published_inclusion_proofs() {
        let l = rfc_leaves();
        let want_0_8 = [
            "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7",
            "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
            "6b47aaf29ee3c2af9af889bc1fb9254dabd31177f16232dd6aab035ca39bf6e4",
        ];
        let want_5_8 = [
            "bc1a0643b12e4d2d7c77918f44e0f4f79a838b6cf9ec5b5c283e1f4d88599e6b",
            "ca854ea128ed050b41b35ffc1b87b8eb2bde461e9e3b5596ece6b9d5975a0ae0",
            "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
        ];
        assert_eq!(
            inclusion_path(0, &l)
                .iter()
                .map(hex::encode)
                .collect::<Vec<_>>(),
            want_0_8
        );
        assert_eq!(
            inclusion_path(5, &l)
                .iter()
                .map(hex::encode)
                .collect::<Vec<_>>(),
            want_5_8
        );
        let root = tree_hash(&l);
        assert!(verify_inclusion(&l[0], 0, 8, &inclusion_path(0, &l), &root).is_ok());
        assert!(verify_inclusion(&l[5], 5, 8, &inclusion_path(5, &l), &root).is_ok());
        for n in 1..=8u64 {
            for i in 0..n {
                let p = inclusion_path(i, &l[..n as usize]);
                assert_eq!(p.len(), expected_path_len(n, i));
                assert!(
                    verify_inclusion(&l[i as usize], i, n, &p, &tree_hash(&l[..n as usize]))
                        .is_ok()
                );
            }
        }
    }

    #[test]
    fn rfc_9162_published_consistency_proofs() {
        let l = rfc_leaves();
        let want_1_8 = [
            "96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7",
            "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
            "6b47aaf29ee3c2af9af889bc1fb9254dabd31177f16232dd6aab035ca39bf6e4",
        ];
        let want_6_8 = [
            "0ebc5d3437fbe2db158b9f126a1d118e308181031d0a949f8dededebc558ef6a",
            "ca854ea128ed050b41b35ffc1b87b8eb2bde461e9e3b5596ece6b9d5975a0ae0",
            "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
        ];
        let want_2_5 = [
            "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
            "bc1a0643b12e4d2d7c77918f44e0f4f79a838b6cf9ec5b5c283e1f4d88599e6b",
        ];
        assert_eq!(
            consistency_proof(1, &l)
                .iter()
                .map(hex::encode)
                .collect::<Vec<_>>(),
            want_1_8
        );
        assert_eq!(
            consistency_proof(6, &l)
                .iter()
                .map(hex::encode)
                .collect::<Vec<_>>(),
            want_6_8
        );
        assert_eq!(
            consistency_proof(2, &l[..5])
                .iter()
                .map(hex::encode)
                .collect::<Vec<_>>(),
            want_2_5
        );
        for m in 1..=8u64 {
            for n in m..=8u64 {
                let p = consistency_proof(m, &l[..n as usize]);
                assert!(
                    verify_consistency(
                        m,
                        &tree_hash(&l[..m as usize]),
                        n,
                        &tree_hash(&l[..n as usize]),
                        &p
                    )
                    .is_ok(),
                    "consistency {m}->{n}"
                );
            }
        }
    }

    #[test]
    fn tampered_path_and_wrong_length_rejected() {
        let l = leaves(9);
        let root = tree_hash(&l);
        let mut p = inclusion_path(4, &l);
        p[0][0] ^= 1;
        assert_eq!(
            verify_inclusion(&l[4], 4, 9, &p, &root),
            Err(MerkleError::RootMismatch)
        );
        let short = &p[..p.len() - 1];
        assert!(matches!(
            verify_inclusion(&l[4], 4, 9, short, &root),
            Err(MerkleError::PathLength { .. })
        ));
        assert!(matches!(
            verify_inclusion(&l[4], 12, 9, &p, &root),
            Err(MerkleError::IndexOutOfRange { .. })
        ));
    }
}
