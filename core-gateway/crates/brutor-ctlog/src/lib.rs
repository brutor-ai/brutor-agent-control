// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Append-only RFC 6962 Merkle log over a pluggable node store.
//!
//! The gateway keeps one log per tenant. Leaves are
//! appended in `seq` order; every interior node that becomes complete is
//! written once and never changes, so a node store can be a Postgres
//! table with `(log_id, level, index)` as the key. Roots, inclusion
//! paths and consistency proofs are all derived from stored nodes in
//! O(log n) lookups — no leaf scan.
//!
//! Node addressing: `(level, index)` names the hash of leaves
//! `[index·2^level, (index+1)·2^level)`. A node exists iff that range is
//! full, i.e. `(index+1)·2^level ≤ size`. The right-hand partial ranges
//! that RFC 6962's recursion produces are folded on the fly from full
//! nodes, exactly as the recursive definition prescribes.
//!
//! The signed tree head is a COSE_Sign1 over a canonical CBOR map
//! carrying `log_id, tree_size, root, prev_size, prev_root, issued_at,
//! consistency_proof` with CWT `iss = log_id` and `sub = "<log_id>#<size>"`.

use brutor_cose::CoseError;
use brutor_cose::cbor::{self, Value, as_bytes, as_text, as_u64, bytes, int, text};
use brutor_cose::merkle::{self, Hash, MerkleError};
use brutor_cose::sign1::{self, CoseSign1, cwt, label};
use ed25519_dalek::{SigningKey, VerifyingKey};
use std::collections::HashMap;

pub const TREE_HEAD_CONTENT_TYPE: &str = "application/vnd.brutor.tree-head+cbor";

#[derive(Debug, thiserror::Error)]
pub enum LogError {
    #[error("node ({0},{1}) missing from store")]
    MissingNode(u32, u64),
    #[error("merkle: {0}")]
    Merkle(#[from] MerkleError),
    #[error("cose: {0}")]
    Cose(#[from] CoseError),
    #[error("tree head malformed: {0}")]
    Head(String),
    #[error("tree size {0} exceeds MAX_TREE_SIZE")]
    TooLarge(u64),
}

/// Storage for completed nodes. Implementations must be append-only.
pub trait NodeStore {
    fn get(&self, level: u32, index: u64) -> Option<Hash>;
    fn put(&mut self, level: u32, index: u64, hash: Hash);
}

#[derive(Default, Debug, Clone)]
pub struct MemoryStore(pub HashMap<(u32, u64), Hash>);

impl NodeStore for MemoryStore {
    fn get(&self, level: u32, index: u64) -> Option<Hash> {
        self.0.get(&(level, index)).copied()
    }
    fn put(&mut self, level: u32, index: u64, hash: Hash) {
        self.0.insert((level, index), hash);
    }
}

/// The nodes a log of `size` leaves needs loaded before it can append or
/// compute its root: one full subtree per set bit of `size` — the
/// "peaks". `(level, index)` pairs, tallest first.
pub fn peaks_for(size: u64) -> Vec<(u32, u64)> {
    let mut out = Vec::new();
    for level in (0..64u32).rev() {
        if size & (1u64 << level) != 0 {
            out.push((level, (size >> level) - 1));
        }
    }
    out
}

/// The stored nodes `MTH(D[a:b])` reads, in the order the RFC 6962
/// recursion visits them. Lets an async caller preload exactly what a
/// proof needs before running the sync algorithm against a memory store.
pub fn nodes_for_range(a: u64, b: u64, out: &mut Vec<(u32, u64)>) {
    if a >= b {
        return;
    }
    let n = b - a;
    if n.is_power_of_two() && a.is_multiple_of(n) {
        let level = n.trailing_zeros();
        out.push((level, a >> level));
        return;
    }
    let k = merkle::split_point(n);
    nodes_for_range(a, a + k, out);
    nodes_for_range(a + k, b, out);
}

/// Nodes an inclusion path for `index` in a tree of `tree_size` needs.
pub fn inclusion_path_nodes(index: u64, tree_size: u64) -> Vec<(u32, u64)> {
    let mut out = Vec::new();
    let (mut a, mut b, mut m) = (0u64, tree_size, index);
    while b - a > 1 {
        let k = merkle::split_point(b - a);
        if m < k {
            nodes_for_range(a + k, b, &mut out);
            b = a + k;
        } else {
            nodes_for_range(a, a + k, &mut out);
            a += k;
            m -= k;
        }
    }
    out
}

/// Nodes a consistency proof from `first` to `second` needs.
pub fn consistency_proof_nodes(first: u64, second: u64) -> Vec<(u32, u64)> {
    fn sub(m: u64, a: u64, b: u64, complete: bool, out: &mut Vec<(u32, u64)>) {
        let n = b - a;
        if m == n {
            if !complete {
                nodes_for_range(a, b, out);
            }
            return;
        }
        let k = merkle::split_point(n);
        if m <= k {
            sub(m, a, a + k, complete, out);
            nodes_for_range(a + k, b, out);
        } else {
            sub(m - k, a + k, b, false, out);
            nodes_for_range(a, a + k, out);
        }
    }
    let mut out = Vec::new();
    if first == 0 || first >= second {
        return out;
    }
    sub(first, 0, second, true, &mut out);
    out
}

/// A store that records every `put` so a caller can persist exactly the
/// nodes an append created, while reads fall through to a pre-seeded
/// inner store (typically the peaks loaded from the database).
pub struct RecordingStore<S: NodeStore> {
    pub inner: S,
    pub puts: Vec<(u32, u64, Hash)>,
}

impl<S: NodeStore> RecordingStore<S> {
    pub fn new(inner: S) -> Self {
        Self {
            inner,
            puts: Vec::new(),
        }
    }
}

impl<S: NodeStore> NodeStore for RecordingStore<S> {
    fn get(&self, level: u32, index: u64) -> Option<Hash> {
        self.inner.get(level, index)
    }
    fn put(&mut self, level: u32, index: u64, hash: Hash) {
        self.puts.push((level, index, hash));
        self.inner.put(level, index, hash);
    }
}

/// A view over a node store for a log of `size` leaves.
pub struct Log<'a, S: NodeStore> {
    store: &'a mut S,
    size: u64,
}

impl<'a, S: NodeStore> Log<'a, S> {
    pub fn open(store: &'a mut S, size: u64) -> Self {
        Self { store, size }
    }

    pub fn size(&self) -> u64 {
        self.size
    }

    /// Append an already-hashed leaf (see [`merkle::leaf_hash`]). Returns
    /// its zero-based index. Writes every node that becomes complete.
    pub fn append(&mut self, leaf: Hash) -> Result<u64, LogError> {
        if self.size >= merkle::MAX_TREE_SIZE {
            return Err(LogError::TooLarge(self.size + 1));
        }
        let idx = self.size;
        self.store.put(0, idx, leaf);
        let mut level = 0u32;
        let mut i = idx;
        let mut h = leaf;
        while i & 1 == 1 {
            let left = self
                .store
                .get(level, i - 1)
                .ok_or(LogError::MissingNode(level, i - 1))?;
            h = merkle::node_hash(&left, &h);
            level += 1;
            i >>= 1;
            self.store.put(level, i, h);
        }
        self.size += 1;
        Ok(idx)
    }

    fn node(&self, level: u32, index: u64) -> Result<Hash, LogError> {
        self.store
            .get(level, index)
            .ok_or(LogError::MissingNode(level, index))
    }

    /// MTH(D[a:b]) per the RFC 6962 recursion, served from full nodes
    /// wherever the range is an aligned complete subtree.
    fn range_hash(&self, a: u64, b: u64) -> Result<Hash, LogError> {
        debug_assert!(a < b && b <= self.size);
        let n = b - a;
        if n.is_power_of_two() && a.is_multiple_of(n) {
            let level = n.trailing_zeros();
            return self.node(level, a >> level);
        }
        let k = merkle::split_point(n);
        let l = self.range_hash(a, a + k)?;
        let r = self.range_hash(a + k, b)?;
        Ok(merkle::node_hash(&l, &r))
    }

    /// MTH over the first `n` leaves (`n ≤ size`).
    pub fn root_at(&self, n: u64) -> Result<Hash, LogError> {
        if n == 0 {
            return Ok(merkle::empty_root());
        }
        self.range_hash(0, n)
    }

    pub fn root(&self) -> Result<Hash, LogError> {
        self.root_at(self.size)
    }

    /// RFC 6962 PATH(m, D[n]) from stored nodes.
    pub fn inclusion_path(&self, index: u64, tree_size: u64) -> Result<Vec<Hash>, LogError> {
        if tree_size > self.size || index >= tree_size {
            return Err(MerkleError::IndexOutOfRange { index, tree_size }.into());
        }
        let mut out = Vec::new();
        let (mut a, mut b, mut m) = (0u64, tree_size, index);
        let mut stack: Vec<Hash> = Vec::new();
        while b - a > 1 {
            let k = merkle::split_point(b - a);
            if m < k {
                stack.push(self.range_hash(a + k, b)?);
                b = a + k;
            } else {
                stack.push(self.range_hash(a, a + k)?);
                a += k;
                m -= k;
            }
        }
        // The recursion appends siblings bottom-up; we descended top-down.
        while let Some(h) = stack.pop() {
            out.push(h);
        }
        Ok(out)
    }

    /// RFC 6962 PROOF(m, D[n]) from stored nodes.
    pub fn consistency_proof(&self, first: u64, second: u64) -> Result<Vec<Hash>, LogError> {
        if first == 0 || first > second || second > self.size {
            return Err(MerkleError::Consistency.into());
        }
        if first == second {
            return Ok(vec![]);
        }
        let mut out = Vec::new();
        self.subproof(first, 0, second, true, &mut out)?;
        Ok(out)
    }

    fn subproof(
        &self,
        m: u64,
        a: u64,
        b: u64,
        complete: bool,
        out: &mut Vec<Hash>,
    ) -> Result<(), LogError> {
        let n = b - a;
        if m == n {
            if !complete {
                out.push(self.range_hash(a, b)?);
            }
            return Ok(());
        }
        let k = merkle::split_point(n);
        if m <= k {
            self.subproof(m, a, a + k, complete, out)?;
            out.push(self.range_hash(a + k, b)?);
        } else {
            self.subproof(m - k, a + k, b, false, out)?;
            out.push(self.range_hash(a, a + k)?);
        }
        Ok(())
    }
}

/// The signed tree head payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeHead {
    pub log_id: String,
    pub tree_size: u64,
    pub root: Hash,
    pub prev_size: u64,
    pub prev_root: Option<Hash>,
    /// RFC 3339 UTC.
    pub issued_at: String,
    /// PROOF(prev_size, D[tree_size]); empty for a first head.
    pub consistency_proof: Vec<Hash>,
}

impl TreeHead {
    pub fn to_cbor(&self) -> Result<Vec<u8>, CoseError> {
        let mut entries = vec![
            (text("log_id"), text(&self.log_id)),
            (text("tree_size"), int(self.tree_size as i64)),
            (text("root"), bytes(&self.root)),
            (text("prev_size"), int(self.prev_size as i64)),
            (text("issued_at"), text(&self.issued_at)),
            (
                text("consistency_proof"),
                Value::Array(self.consistency_proof.iter().map(|h| bytes(h)).collect()),
            ),
        ];
        entries.push((
            text("prev_root"),
            match &self.prev_root {
                Some(h) => bytes(h),
                None => bytes(&[]),
            },
        ));
        cbor::encode(&cbor::canonical_map(entries)?)
    }

    pub fn from_cbor(raw: &[u8]) -> Result<Self, LogError> {
        let v = cbor::decode(raw)?;
        cbor::check_unique_keys(&v, "tree_head")?;
        let get = |k: &str| {
            cbor::map_get_text(&v, k).ok_or_else(|| LogError::Head(format!("missing {k}")))
        };
        let hash = |val: &Value, what: &str| -> Result<Hash, LogError> {
            as_bytes(val)
                .and_then(|b| <[u8; 32]>::try_from(b).ok())
                .ok_or_else(|| LogError::Head(format!("{what} not 32 bytes")))
        };
        let log_id = as_text(get("log_id")?)
            .ok_or_else(|| LogError::Head("log_id".into()))?
            .to_owned();
        let tree_size =
            as_u64(get("tree_size")?).ok_or_else(|| LogError::Head("tree_size".into()))?;
        let root = hash(get("root")?, "root")?;
        let prev_size =
            as_u64(get("prev_size")?).ok_or_else(|| LogError::Head("prev_size".into()))?;
        let prev_root = match as_bytes(get("prev_root")?) {
            Some([]) => None,
            Some(b) => {
                Some(<[u8; 32]>::try_from(b).map_err(|_| LogError::Head("prev_root".into()))?)
            }
            None => return Err(LogError::Head("prev_root".into())),
        };
        let issued_at = as_text(get("issued_at")?)
            .ok_or_else(|| LogError::Head("issued_at".into()))?
            .to_owned();
        let consistency_proof = match get("consistency_proof")? {
            Value::Array(items) => items
                .iter()
                .map(|i| hash(i, "proof node"))
                .collect::<Result<Vec<_>, _>>()?,
            _ => return Err(LogError::Head("consistency_proof".into())),
        };
        if prev_size >= tree_size && tree_size != 0 {
            return Err(LogError::Head("prev_size must be < tree_size".into()));
        }
        if (prev_size == 0) != prev_root.is_none() {
            return Err(LogError::Head(
                "prev_root presence must match prev_size".into(),
            ));
        }
        Ok(Self {
            log_id,
            tree_size,
            root,
            prev_size,
            prev_root,
            issued_at,
            consistency_proof,
        })
    }

    /// Seal as an RFC 9943 Signed Statement (Full-Content mode).
    pub fn sign(&self, key: &SigningKey) -> Result<Vec<u8>, LogError> {
        let payload = self.to_cbor()?;
        let protected = vec![
            (int(label::CONTENT_TYPE), text(TREE_HEAD_CONTENT_TYPE)),
            (int(label::KID), bytes(key.verifying_key().as_bytes())),
            (
                int(label::CWT_CLAIMS),
                cbor::canonical_map(vec![
                    (int(cwt::ISS), text(&self.log_id)),
                    (
                        int(cwt::SUB),
                        text(&format!("{}#{}", self.log_id, self.tree_size)),
                    ),
                ])?,
            ),
        ];
        Ok(sign1::sign_ed25519(protected, vec![], Some(&payload), key)?.encode()?)
    }

    /// Verify a signed head against a trusted key and re-check the
    /// embedded consistency proof against `prev_root`.
    pub fn verify(raw: &[u8], key: &VerifyingKey) -> Result<(Self, CoseSign1), LogError> {
        let s = CoseSign1::decode(raw)?;
        if s.content_type() != Some(TREE_HEAD_CONTENT_TYPE) {
            return Err(LogError::Head("content type".into()));
        }
        s.verify_ed25519(key, None)?;
        let payload = s.payload.clone().ok_or(CoseError::DetachedPayload)?;
        let head = Self::from_cbor(&payload)?;
        let iss = s.cwt_claim(cwt::ISS).and_then(as_text);
        let sub = s.cwt_claim(cwt::SUB).and_then(as_text);
        if iss != Some(head.log_id.as_str())
            || sub != Some(format!("{}#{}", head.log_id, head.tree_size).as_str())
        {
            return Err(LogError::Head("iss/sub do not match payload".into()));
        }
        if let Some(prev) = &head.prev_root {
            merkle::verify_consistency(
                head.prev_size,
                prev,
                head.tree_size,
                &head.root,
                &head.consistency_proof,
            )?;
        }
        Ok((head, s))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use brutor_cose::merkle::{
        consistency_proof, inclusion_path, leaf_hash, tree_hash, verify_consistency,
        verify_inclusion,
    };

    fn leaves(n: u64) -> Vec<Hash> {
        (0..n).map(|i| leaf_hash(&i.to_be_bytes())).collect()
    }

    #[test]
    fn stored_log_matches_recursive_definition() {
        let l = leaves(37);
        let mut store = MemoryStore::default();
        let mut log = Log::open(&mut store, 0);
        for (i, h) in l.iter().enumerate() {
            assert_eq!(log.append(*h).unwrap(), i as u64);
            let n = i as u64 + 1;
            assert_eq!(
                log.root().unwrap(),
                tree_hash(&l[..n as usize]),
                "root at {n}"
            );
        }
        for n in 1..=37u64 {
            for m in 0..n {
                let p = log.inclusion_path(m, n).unwrap();
                assert_eq!(p, inclusion_path(m, &l[..n as usize]), "path {m} in {n}");
                verify_inclusion(&l[m as usize], m, n, &p, &log.root_at(n).unwrap()).unwrap();
            }
            for f in 1..=n {
                let p = log.consistency_proof(f, n).unwrap();
                assert_eq!(p, consistency_proof(f, &l[..n as usize]), "proof {f}->{n}");
                verify_consistency(f, &log.root_at(f).unwrap(), n, &log.root_at(n).unwrap(), &p)
                    .unwrap();
            }
        }
    }

    #[test]
    fn peaks_are_sufficient_to_append_and_root() {
        // Build a full store, then seed a fresh store with only the peaks and
        // check that appending and root computation need nothing else.
        let l = leaves(40);
        for n in 1..=30u64 {
            let mut full = MemoryStore::default();
            let mut log = Log::open(&mut full, 0);
            for h in &l[..n as usize] {
                log.append(*h).unwrap();
            }
            let expected_root_before = log.root().unwrap();
            let mut seeded = MemoryStore::default();
            for (level, index) in peaks_for(n) {
                seeded.put(level, index, full.get(level, index).expect("peak exists"));
            }
            let mut rec = RecordingStore::new(seeded);
            let mut partial = Log::open(&mut rec, n);
            assert_eq!(
                partial.root().unwrap(),
                expected_root_before,
                "root from peaks at {n}"
            );
            for h in &l[n as usize..n as usize + 5] {
                partial.append(*h).unwrap();
            }
            assert_eq!(
                partial.root().unwrap(),
                tree_hash(&l[..n as usize + 5]),
                "root after appends at {n}"
            );
            // every recorded put is a real node of the full tree
            let mut check = MemoryStore::default();
            let mut check_log = Log::open(&mut check, 0);
            for h in &l[..n as usize + 5] {
                check_log.append(*h).unwrap();
            }
            for (level, index, hash) in &rec.puts {
                assert_eq!(check.get(*level, *index), Some(*hash));
            }
        }
    }

    #[test]
    fn node_address_helpers_are_sufficient() {
        let l = leaves(33);
        let mut full = MemoryStore::default();
        {
            let mut log = Log::open(&mut full, 0);
            for h in &l {
                log.append(*h).unwrap();
            }
        }
        for n in 1..=33u64 {
            for i in 0..n {
                let mut seeded = MemoryStore::default();
                for (lv, ix) in inclusion_path_nodes(i, n) {
                    seeded.put(lv, ix, full.get(lv, ix).expect("node"));
                }
                let partial = Log::open(&mut seeded, n);
                assert_eq!(
                    partial.inclusion_path(i, n).unwrap(),
                    inclusion_path(i, &l[..n as usize])
                );
            }
            for f in 1..n {
                let mut seeded = MemoryStore::default();
                for (lv, ix) in consistency_proof_nodes(f, n) {
                    seeded.put(lv, ix, full.get(lv, ix).expect("node"));
                }
                let partial = Log::open(&mut seeded, n);
                assert_eq!(
                    partial.consistency_proof(f, n).unwrap(),
                    consistency_proof(f, &l[..n as usize])
                );
            }
        }
    }

    #[test]
    fn tree_head_round_trip_and_verify() {
        let l = leaves(12);
        let mut store = MemoryStore::default();
        let mut log = Log::open(&mut store, 0);
        for h in &l[..5] {
            log.append(*h).unwrap();
        }
        let first = TreeHead {
            log_id: "log-a".into(),
            tree_size: 5,
            root: log.root().unwrap(),
            prev_size: 0,
            prev_root: None,
            issued_at: "2026-09-19T10:00:00Z".into(),
            consistency_proof: vec![],
        };
        for h in &l[5..] {
            log.append(*h).unwrap();
        }
        let second = TreeHead {
            log_id: "log-a".into(),
            tree_size: 12,
            root: log.root().unwrap(),
            prev_size: 5,
            prev_root: Some(first.root),
            issued_at: "2026-09-19T10:15:00Z".into(),
            consistency_proof: log.consistency_proof(5, 12).unwrap(),
        };
        let key = SigningKey::from_bytes(&[5u8; 32]);
        let raw1 = first.sign(&key).unwrap();
        let raw2 = second.sign(&key).unwrap();
        let (h1, _) = TreeHead::verify(&raw1, &key.verifying_key()).unwrap();
        assert_eq!(h1, first);
        let (h2, s2) = TreeHead::verify(&raw2, &key.verifying_key()).unwrap();
        assert_eq!(h2, second);
        assert_eq!(s2.cwt_claim(cwt::SUB).and_then(as_text), Some("log-a#12"));

        // A head whose proof does not extend the claimed prev_root is rejected.
        let mut forged = second.clone();
        forged.prev_root = Some([1u8; 32]);
        let raw = forged.sign(&key).unwrap();
        assert!(TreeHead::verify(&raw, &key.verifying_key()).is_err());
        // Wrong key.
        let other = SigningKey::from_bytes(&[6u8; 32]);
        assert!(TreeHead::verify(&raw2, &other.verifying_key()).is_err());
    }
}
