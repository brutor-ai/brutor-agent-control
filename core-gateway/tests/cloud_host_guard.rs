// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cloud-host guard.
//!
//! **Hosts.** `format!("https://bedrock-runtime.{region}.amazonaws.com")`
//! with a region that is `x.attacker.example/` is a host under another
//! domain: the SigV4-signed request and the prompt went there. The same for
//! `{location}-aiplatform.googleapis.com`. A host of `amazonaws.com` or
//! `googleapis.com` that has a variable part is built in
//! `src/services/cloud_hosts.rs`, which checks the part, and nowhere else.
//! Forbidden everywhere else in product code: a string literal that names
//! one of the two domains and interpolates (`{`), and a literal that is the
//! tail of such a host (starts with `.amazonaws.com`, `.googleapis.com`,
//! `-aiplatform.googleapis.com`), which is what a concatenation looks like.
//! A literal that is a whole fixed host (`https://www.googleapis.com/…`)
//! builds nothing and is not this guard's.
//!
//! **Credential headers.** No product code names an `x-brutor-aws-`,
//! `x-brutor-azure-` or `x-brutor-gcp-` header except the redaction list:
//! those were the headers a row's cloud credential was copied into and sent
//! upstream under.
//!
//! Scans `src/` (not `src/bin/`) and `enterprise/src/`, with `#[cfg(test)]`
//! modules, test files and comments removed.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

const BUILDER: &str = "src/services/cloud_hosts.rs";
const DOMAINS: &[&str] = &["amazonaws.com", "googleapis.com"];
const TAILS: &[&str] = &[
    ".amazonaws.com",
    ".googleapis.com",
    "-aiplatform.googleapis.com",
];

const CREDENTIAL_HEADERS: &[&str] = &["x-brutor-aws", "x-brutor-azure", "x-brutor-gcp"];
/// The one file that names them: the list of header names whose values are
/// redacted in logs and audit rows.
const REDACTION_LIST: &str = "src/services/proxy_logger.rs";

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            if p != Path::new("src/bin") {
                rust_files(&p, out);
            }
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            if name != "tests.rs" && !name.ends_with("_tests.rs") {
                out.push(p);
            }
        }
    }
}

/// The end of the brace-balanced block that starts at the first `{` of
/// `src` (exclusive), or `src.len()`.
fn block_end(src: &str) -> usize {
    let mut depth = 0usize;
    for (i, c) in src.char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return i + 1;
                }
            }
            _ => {}
        }
    }
    src.len()
}

/// The product code of a file: without its `#[cfg(test)] mod` blocks (code
/// after such a block is product code) and without comment lines.
fn product(src: &str) -> String {
    let mut code = String::new();
    let mut rest = src;
    while let Some(start) = rest
        .find("#[cfg(test)]\nmod ")
        .or_else(|| rest.find("#[cfg(test)]\r\nmod "))
    {
        code.push_str(&rest[..start]);
        let block = &rest[start..];
        rest = &block[block_end(block)..];
    }
    code.push_str(rest);
    code.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The string literals of `code` (plain `"…"` literals, with `\"` escapes;
/// a literal continued over lines with `\` is one literal).
fn literals(code: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut chars = code.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '"' {
            continue;
        }
        let mut lit = String::new();
        while let Some(c) = chars.next() {
            match c {
                '\\' => {
                    if let Some(next) = chars.next() {
                        lit.push('\\');
                        lit.push(next);
                    }
                }
                '"' => break,
                _ => lit.push(c),
            }
        }
        out.push(lit);
    }
    out
}

/// Why `literal` builds a cloud host, if it does.
fn builds_a_host(literal: &str) -> Option<&'static str> {
    if !DOMAINS.iter().any(|d| literal.contains(d)) {
        return None;
    }
    if literal.contains('{') {
        return Some("interpolates into a string that names a cloud domain");
    }
    if TAILS.iter().any(|t| literal.starts_with(t)) {
        return Some("is the tail of a cloud host, to be appended to a variable");
    }
    None
}

fn host_violations(file: &str, src: &str) -> Vec<String> {
    literals(&product(src))
        .into_iter()
        .filter_map(|lit| builds_a_host(&lit).map(|why| format!("{file}: \"{lit}\" {why}")))
        .collect()
}

fn header_violations(file: &str, src: &str) -> Vec<String> {
    let code = product(src).to_ascii_lowercase();
    CREDENTIAL_HEADERS
        .iter()
        .filter(|h| code.contains(*h))
        .map(|h| format!("{file}: names the credential header `{h}…`"))
        .collect()
}

fn scan(check: fn(&str, &str) -> Vec<String>, exempt: &str) -> Vec<String> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found the sources");
    let mut out = Vec::new();
    for f in files {
        let name = f.to_string_lossy().replace('\\', "/");
        if name == exempt {
            continue;
        }
        out.extend(check(&name, &fs::read_to_string(&f).unwrap_or_default()));
    }
    out
}

#[test]
fn a_cloud_host_is_built_only_by_the_builder() {
    let found = scan(host_violations, BUILDER);
    assert!(
        found.is_empty(),
        "a host of amazonaws.com / googleapis.com is built outside {BUILDER} \
         (use services::cloud_hosts):\n{}",
        found.join("\n")
    );
    // The builder is where they are.
    let builder = fs::read_to_string(BUILDER).expect("the builder");
    assert!(
        !host_violations(BUILDER, &builder).is_empty(),
        "{BUILDER} builds the hosts"
    );
}

#[test]
fn no_code_names_a_cloud_credential_header() {
    let found = scan(header_violations, REDACTION_LIST);
    assert!(
        found.is_empty(),
        "product code names a header a cloud credential was sent under:\n{}",
        found.join("\n")
    );
    let list = fs::read_to_string(REDACTION_LIST).expect("the redaction list");
    for header in CREDENTIAL_HEADERS {
        assert!(
            product(&list).contains(header),
            "{REDACTION_LIST} redacts `{header}-…`"
        );
    }
}

/// The guard sees the shapes it forbids, and not what it allows.
#[test]
fn the_guard_catches_the_shapes_it_forbids() {
    for bad in [
        r#"let base = format!("https://bedrock-runtime.{region}.amazonaws.com");"#,
        r#"*api_url = format!("https://bedrock-runtime.{}.amazonaws.com", region);"#,
        "let url = format!(\n    \"https://{region}-aiplatform.googleapis.com/v1/projects/{project}\"\n);",
        r#"let host = "https://kms.".to_string() + region + ".amazonaws.com";"#,
        r#"host.push_str("-aiplatform.googleapis.com");"#,
        r#"let u = format!("https://generativelanguage.googleapis.com/v1beta/cachedContents?key={}", k);"#,
        // Product code after a file's test module is product code.
        "fn f() {}\n#[cfg(test)]\nmod tests { fn t() { let _ = 1; } }\nfn g(r: &str) -> String { format!(\"https://kms.{r}.amazonaws.com\") }",
    ] {
        assert!(
            !host_violations("x.rs", bad).is_empty(),
            "not caught: {bad}"
        );
    }
    for good in [
        r#"const GOOGLE_ORIGIN: &str = "https://www.googleapis.com";"#,
        r#"let scopes = &["https://www.googleapis.com/auth/cloud-platform"];"#,
        r#"// format!("https://bedrock-runtime.{region}.amazonaws.com")"#,
        "fn f() {}\n#[cfg(test)]\nmod tests { fn t() { let _ = format!(\"https://s3.{r}.amazonaws.com\"); } }",
        r#"let url = format!("{base}/model/{id}/invoke");"#,
    ] {
        assert!(host_violations("x.rs", good).is_empty(), "caught: {good}");
    }
    assert!(
        !header_violations(
            "x.rs",
            r#"headers.push(("x-brutor-aws-secret-access-key".into(), secret.clone()));"#
        )
        .is_empty()
    );
    assert!(header_violations("x.rs", r#"headers.push(("x-amz-date".into(), d));"#).is_empty());
}
