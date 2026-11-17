// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: an MCP server's stored static token is read in ONE place.
//!
//! `mcp_servers.auth_token` holds a ciphertext. `McpServer::static_token`
//! decrypts it (`EncryptionService::decrypt`) and is an error for anything
//! that is not a ciphertext the gateway can open, which the caller refuses.
//! A reader that takes the field as it is sends whatever is stored to the
//! MCP server as the bearer: a ciphertext written under another
//! `ENCRYPTION_KEY`, an envelope the gateway cannot unwrap, or a value
//! that was never encrypted. `open_stored`, which returns a value that is
//! not ciphertext as it is, is not a way to read this column.
//!
//! The accessor is also where the token's use is decided: it is a bearer
//! token, read and sent for a server whose `auth_type` is `bearer` and for
//! no other.
//!
//! The scan reads the production code of both editions. A struct literal
//! (`auth_token: None`) sets the field and is not a read.

#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::Path;

/// The one file that may read the field: the accessor's.
const ACCESSOR: &str = "src/models/db.rs";

/// The lines of `code` that read the field of a value (`x.auth_token`).
fn field_reads(code: &str) -> Vec<String> {
    code.lines()
        .map(str::trim)
        .filter(|l| !l.starts_with("//"))
        .filter(|l| l.contains(".auth_token"))
        .map(str::to_string)
        .collect()
}

fn scan(dir: &Path, root: &Path, found: &mut Vec<String>, read: &mut usize) {
    for entry in fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            scan(&path, root, found, read);
        } else if path.extension().is_some_and(|e| e == "rs") {
            let rel = path.strip_prefix(root).expect("under the root");
            let src = fs::read_to_string(&path).expect("read");
            *read += 1;
            if rel == Path::new(ACCESSOR) {
                continue;
            }
            for line in field_reads(&src) {
                found.push(format!("{}: {line}", rel.display()));
            }
        }
    }
}

#[test]
fn the_static_token_is_read_through_its_accessor_only() {
    let community = roots::tree("community").expect("the Community root");
    let mut found = Vec::new();
    let mut read = 0;
    scan(&community.join("src"), &community, &mut found, &mut read);
    assert!(read > 200, "the scan read only {read} Community files");
    if let Some(enterprise) = roots::tree("enterprise") {
        let before = read;
        scan(&enterprise.join("src"), &enterprise, &mut found, &mut read);
        assert!(
            read - before > 20,
            "the scan read only {} Enterprise files",
            read - before
        );
    }
    assert!(
        found.is_empty(),
        "`auth_token` is read outside `McpServer::static_token` (use the accessor: it opens \
         the stored value and refuses a ciphertext that does not decrypt):\n{}",
        found.join("\n")
    );

    // The accessor itself opens the stored value and reads it nowhere else.
    let accessor = fs::read_to_string(community.join(ACCESSOR)).expect("read the accessor's file");
    assert_eq!(
        field_reads(&accessor).len(),
        1,
        "`{ACCESSOR}` reads `auth_token` in more than one place"
    );
    let at = accessor
        .find("pub fn static_token(")
        .expect("`McpServer::static_token` is gone; update this guard");
    let body = &accessor[at..];
    let body = &body[..body.find("\n    }\n").expect("end of the accessor")];
    assert!(
        body.contains(".auth_token")
            && body.contains("encryption.decrypt(stored)")
            && !body.contains("open_stored("),
        "`McpServer::static_token` must decrypt the stored value with `decrypt`, which \
         refuses a value that is not ciphertext (`open_stored` returns it as the token)"
    );
    // The token is a bearer token: the accessor asks what the server's
    // `auth_type` is before it reads the stored value, so a `none` or
    // `oauth` server is never sent one.
    assert!(
        asks_the_auth_type_first(body),
        "`McpServer::static_token` must return no token for a server whose `auth_type` is \
         not `bearer`, before it reads `auth_token`"
    );
}

/// Whether an accessor body decides on `auth_type` before it reads the
/// stored token.
fn asks_the_auth_type_first(body: &str) -> bool {
    match (
        body.find("self.auth_type.as_deref() != Some(STATIC_TOKEN_AUTH_TYPE)"),
        body.find(".auth_token"),
    ) {
        (Some(asked), Some(read)) => asked < read,
        _ => false,
    }
}

#[test]
fn the_auth_type_check_is_recognised_only_before_the_read() {
    let asked = "if self.auth_type.as_deref() != Some(STATIC_TOKEN_AUTH_TYPE) { return Ok(None); }";
    let read = "match self.auth_token.as_deref() {";
    assert!(asks_the_auth_type_first(&format!("{asked}\n{read}")));
    assert!(!asks_the_auth_type_first(&format!("{read}\n{asked}")));
    assert!(!asks_the_auth_type_first(read));
}

/// The scan finds the shapes it forbids and leaves a struct literal alone.
#[test]
fn the_scan_finds_a_direct_read() {
    for read in [
        "if let Some(ref token) = server.auth_token {",
        "req = req.bearer_auth(server.auth_token.as_deref().unwrap_or_default());",
        "&& let Some(t) = &s.auth_token",
    ] {
        assert_eq!(field_reads(read).len(), 1, "{read}");
    }
    for not_a_read in [
        "auth_token: None,",
        "auth_token: auth_token.map(String::from),",
        "pub auth_token: Option<String>,",
        "// server.auth_token is opened by the accessor",
    ] {
        assert!(field_reads(not_a_read).is_empty(), "{not_a_read}");
    }
}

/// No stored secret is read by a function that returns the stored value as
/// the secret when it is not ciphertext. There was one
/// (`EncryptionService::open_stored`); a stored MCP token, a model's API key
/// and cloud credentials and a guardrail provider's secrets went to the
/// upstream as they were stored. Every reader decrypts, and what does not
/// decrypt is refused.
#[test]
fn no_stored_secret_is_read_by_a_reader_that_passes_clear_text_through() {
    let community = roots::tree("community").expect("the Community root");
    let mut found = Vec::new();
    let mut stack = vec![community.join("src")];
    if let Some(enterprise) = roots::tree("enterprise") {
        stack.push(enterprise.join("src"));
    }
    let mut read = 0;
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
            let path = entry.expect("entry").path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                read += 1;
                let src = fs::read_to_string(&path).expect("read");
                for line in pass_through_readers(&src) {
                    found.push(format!("{}: {line}", path.display()));
                }
            }
        }
    }
    assert!(read > 200, "the scan read only {read} files");
    assert!(
        found.is_empty(),
        "a stored secret is read with a decrypt-or-leave reader (use \
         `EncryptionService::decrypt` and refuse what does not decrypt):\n{}",
        found.join("\n")
    );
}

/// The lines of `code` that define or call a decrypt-or-leave reader.
fn pass_through_readers(code: &str) -> Vec<String> {
    code.lines()
        .map(str::trim)
        .filter(|l| !l.starts_with("//"))
        .filter(|l| l.contains("open_stored(") || l.contains("decrypt_or_leave("))
        .map(str::to_string)
        .collect()
}

#[test]
fn the_pass_through_scan_finds_a_reader_and_a_call() {
    for bad in [
        "pub fn open_stored(&self, stored: &str) -> Result<String, EncryptionError> {",
        "match encryption.open_stored(stored) {",
    ] {
        assert_eq!(pass_through_readers(bad).len(), 1, "{bad}");
    }
    for good in [
        "match encryption.decrypt(stored) {",
        "/// `open_stored(` returned a stored value as the secret.",
    ] {
        assert!(pass_through_readers(good).is_empty(), "{good}");
    }
}
