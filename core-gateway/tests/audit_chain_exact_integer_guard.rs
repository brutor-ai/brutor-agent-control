// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: a row is sealed only after its JSON columns were made exact.
//!
//! Why it matters: the row hash covers six JSON columns as RFC 8785 text,
//! which prints an integer above 2^53 − 1 as the nearest double. Two such
//! integers within one double's rounding hash the same, so an edit between
//! them verifies. `audit_chain::make_sealed_json_exact` stores such an
//! integer as its digits before the row is hashed. That only holds while
//! (1) the one function that stamps a row calls it before hashing, and
//! (2) nothing else in production writes a row hash.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

/// A file without its test section.
fn production(src: &str) -> &str {
    src.split("#[cfg(test)]").next().unwrap_or(src)
}

fn rust_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).expect("read dir").flatten() {
        let path = entry.path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

/// The body of `stamp_with`, where a row gets its hash.
fn stamp_with_body(code: &str) -> &str {
    let at = code
        .find("pub fn stamp_with(")
        .expect("audit_chain.rs no longer has stamp_with");
    let rest = &code[at..];
    let end = rest.find("\n}\n").expect("end of stamp_with");
    &rest[..end]
}

#[test]
fn the_stamp_makes_sealed_json_exact_before_it_hashes() {
    let src = fs::read_to_string("src/services/audit_chain.rs").expect("read audit_chain.rs");
    let body = stamp_with_body(production(&src));
    let exact = body
        .find("make_sealed_json_exact(row)")
        .expect("stamp_with no longer makes the sealed JSON columns exact");
    let hash = body
        .find("hash_row(row")
        .expect("stamp_with no longer hashes the row");
    assert!(
        exact < hash,
        "stamp_with hashes the row before making its sealed JSON columns exact: \
         what is stored would not be what was hashed"
    );
}

/// The guard above proves itself against the shape it forbids.
#[test]
fn the_guard_sees_a_stamp_that_hashes_first() {
    let bad = "pub fn stamp_with(rows: &mut [Row]) {\n    let hash = hash_row(row, &prev);\n    \
               make_sealed_json_exact(row);\n}\n";
    let body = stamp_with_body(bad);
    assert!(body.find("make_sealed_json_exact(row)") > body.find("hash_row(row"));
}

#[test]
fn nothing_else_in_production_writes_a_row_hash() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 50, "the scan found no source tree");
    let mut writers = Vec::new();
    for path in files {
        let src = fs::read_to_string(&path).expect("read source");
        if production(&src)
            .lines()
            .any(|l| l.contains(".row_hash = Some("))
        {
            writers.push(path.to_string_lossy().replace('\\', "/"));
        }
    }
    assert_eq!(
        writers,
        vec!["src/services/audit_chain.rs".to_string()],
        "a row hash is written outside audit_chain::stamp_with: that row's sealed JSON \
         columns are not made exact before it is hashed"
    );
}
