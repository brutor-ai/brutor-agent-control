// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The mounted-route table.
//!
//! Every route the assembled router serves, as `METHOD /path` rows, compared
//! with a recording: `tests/fixtures/mounted_routes/enterprise.txt` for the
//! full (Enterprise) assembly and `community.txt` for a Community assembly
//! (a state nothing was installed on, no extensions). Any difference fails,
//! in either direction: a route added, removed, moved to another prefix, or
//! served for another method.
//!
//! Why this exists beside the OpenAPI snapshot and the credential matrix:
//! the derived OpenAPI document lists 22 operations and no Enterprise route,
//! and the matrix's route list is read from the source of `src/routes/`, not
//! from the application. This table is read from the router itself, so it
//! is what holds still while features become extensions: the
//! Enterprise table byte for byte, the Community table changing only where
//! a step says so (each such change is a deliberate re-record, listed in the
//! plan).
//!
//! ## How the table is read
//!
//! axum does not expose a router's routes, but its `Debug` output does: the
//! path of every route id and, per route, which methods have a handler
//! ([`mounted_routes`]). The format belongs to axum (pinned by `Cargo.lock`);
//! [`the_reader_reads_the_shapes_axum_prints`] proves the reader against a
//! router whose routes are known, so a change of format fails there, by
//! name, and not as a fixture difference.
//!
//! A row's method is one of the nine HTTP methods a `MethodRouter` holds,
//! `ANY` for a route that answers every method from one handler
//! (`any(...)`: a method router with no method set and a fallback of its
//! own), or `SERVICE` for a path served by a mounted service
//! (`route_service` / `nest_service`). `HEAD` is listed only where a route
//! sets it: axum answers `HEAD` from the `GET` handler without an entry.
//!
//! Re-record (only on purpose, and say why in the change):
//! `MOUNTED_ROUTES_RECORD=1 cargo nextest run --features db-tests -E 'test(mounted_routes)'`

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use axum::Router;
use sqlx::PgPool;
use sqlx::postgres::PgPoolOptions;

use crate::common;

pub(crate) const FIXTURE_DIR: &str = "tests/fixtures/mounted_routes";

/// The methods of axum's `MethodRouter`, in the order its `Debug` prints them.
pub(crate) const METHODS: [&str; 9] = [
    "get", "head", "delete", "options", "patch", "post", "put", "trace", "connect",
];

/// The text between `open` and the brace that closes it. `open` ends with
/// the opening brace.
pub(crate) fn braced<'a>(text: &'a str, open: &str) -> &'a str {
    let start = text
        .find(open)
        .unwrap_or_else(|| panic!("the router's Debug output has no `{open}`"))
        + open.len();
    let mut depth = 1usize;
    let mut in_string = false;
    let mut escaped = false;
    for (i, c) in text[start..].char_indices() {
        if in_string {
            match c {
                _ if escaped => escaped = false,
                '\\' => escaped = true,
                '"' => in_string = false,
                _ => {}
            }
            continue;
        }
        match c {
            '"' => in_string = true,
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return &text[start..start + i];
                }
            }
            _ => {}
        }
    }
    panic!("`{open}` is not closed");
}

/// `(id, rest)` for every `RouteId(id): rest` entry of a Debug-printed map.
pub(crate) fn entries(map: &str) -> Vec<(u32, &str)> {
    let mut out = Vec::new();
    let starts: Vec<usize> = map.match_indices("RouteId(").map(|(i, _)| i).collect();
    for (n, at) in starts.iter().enumerate() {
        let end = starts.get(n + 1).copied().unwrap_or(map.len());
        let entry = &map[at + "RouteId(".len()..end];
        let (id, rest) = entry.split_once("): ").expect("`RouteId(n): value`");
        out.push((id.parse().expect("a numeric route id"), rest));
    }
    out
}

/// Every route `app` serves, as sorted `METHOD /path` rows.
pub(crate) fn mounted_routes(app: &Router) -> Vec<String> {
    let printed = format!("{app:?}");
    // The routes proper; the fallback router that follows is not a surface.
    let path_router = braced(&printed, "path_router: PathRouter {");
    let routes = braced(path_router, "routes: {");
    let paths = braced(path_router, "node: Node { paths: {");

    let mut path_of: BTreeMap<u32, String> = BTreeMap::new();
    for (id, rest) in entries(paths) {
        let quoted = rest.trim_end_matches([',', ' ']);
        let path = quoted
            .strip_prefix('"')
            .and_then(|p| p.strip_suffix('"'))
            .unwrap_or_else(|| panic!("route {id}: the path is not a quoted string: {quoted}"));
        assert!(
            !path.contains('\\'),
            "route {id}: a path with an escape is not read: {path}"
        );
        assert!(
            path_of.insert(id, path.to_string()).is_none(),
            "route id {id} has two paths"
        );
    }

    let mut rows = Vec::new();
    let mut seen = 0;
    for (id, endpoint) in entries(routes) {
        seen += 1;
        let path = path_of
            .get(&id)
            .unwrap_or_else(|| panic!("route {id} has no path"));
        if endpoint.starts_with("Route(") {
            rows.push(format!("SERVICE {path}"));
            continue;
        }
        assert!(
            endpoint.starts_with("MethodRouter(MethodRouter {"),
            "route {id} ({path}): an endpoint of a kind this reader does not know: {endpoint}"
        );
        let mut any_method = false;
        for method in METHODS {
            let field = format!(" {method}: ");
            let at = endpoint
                .find(&field)
                .unwrap_or_else(|| panic!("route {id} ({path}): no `{method}` field"));
            if !endpoint[at + field.len()..].starts_with("None") {
                any_method = true;
                rows.push(format!("{} {path}", method.to_uppercase()));
            }
        }
        let fallback = endpoint
            .find(" fallback: ")
            .map(|at| &endpoint[at + " fallback: ".len()..])
            .unwrap_or_else(|| panic!("route {id} ({path}): no `fallback` field"));
        if !fallback.starts_with("Default(") {
            rows.push(format!("ANY {path}"));
        } else {
            assert!(any_method, "route {id} ({path}) serves no method");
        }
    }
    assert_eq!(
        seen,
        path_of.len(),
        "every route has a path and every path a route"
    );
    // By path, then method.
    rows.sort_by(|a, b| {
        let (am, ap) = a.split_once(' ').expect("row");
        let (bm, bp) = b.split_once(' ').expect("row");
        (ap, am).cmp(&(bp, bm))
    });
    rows
}

/// The recording `name` of the crate at `root`: the Community recording is
/// the Community crate's, the Enterprise one the Enterprise workspace's.
pub(crate) fn fixture_path(root: &Path, name: &str) -> PathBuf {
    root.join(FIXTURE_DIR).join(format!("{name}.txt"))
}

pub(crate) fn recorded(root: &Path, name: &str) -> Vec<String> {
    let path = fixture_path(root, name);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("{}: {e}", path.display()))
        .lines()
        .map(str::to_string)
        .collect()
}

/// Rows only in the recording (`-`) and rows only in the application (`+`).
pub(crate) fn differences(recorded: &[String], actual: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for row in recorded {
        if !actual.contains(row) {
            out.push(format!("- {row}"));
        }
    }
    for row in actual {
        if !recorded.contains(row) {
            out.push(format!("+ {row}"));
        }
    }
    out
}

pub(crate) fn compare_or_record(root: &Path, name: &str, app: &Router) {
    let actual = mounted_routes(app);
    // A floor under both tables (the Community one has 96 rows).
    assert!(
        actual.len() >= 90 && actual.contains(&"GET /health".to_string()),
        "the reader found {} routes: it reads nothing",
        actual.len()
    );
    let text = actual.join("\n") + "\n";
    if std::env::var("MOUNTED_ROUTES_RECORD").is_ok() {
        std::fs::create_dir_all(root.join(FIXTURE_DIR)).expect("fixture directory");
        std::fs::write(fixture_path(root, name), text).expect("write the fixture");
        return;
    }
    let recorded_rows = recorded(root, name);
    let diff = differences(&recorded_rows, &actual);
    assert!(
        diff.is_empty(),
        "the {name} assembly's mounted routes differ from {FIXTURE_DIR}/{name}.txt \
         (- recorded and not mounted, + mounted and not recorded):\n{}",
        diff.join("\n")
    );
    // Byte for byte: order, duplicates and the final newline included.
    let on_disk = std::fs::read_to_string(fixture_path(root, name)).expect("fixture");
    assert_eq!(on_disk, text, "{name}: same rows, not the same file");
}

/// A pool that never connects: building the application reads no database.
pub(crate) fn unconnected_pool() -> PgPool {
    PgPoolOptions::new()
        .connect_lazy("postgresql://placeholder:placeholder@127.0.0.1:1/none")
        .expect("a lazy pool")
}

/// A Community assembly: a state nothing was installed on, no extensions.
#[tokio::test]
async fn mounted_routes_of_the_community_assembly_are_as_recorded() {
    let app = common::community_app(unconnected_pool());
    compare_or_record(common::community_root(), "community", &app);
}

/// The route prefixes of the Enterprise Edition's extensions (the editions
/// manifest, `core_gateway.enterprise_route_prefixes`): the batch API, the
/// public tenant theme, the end-user Portal API and the knowledge-base
/// connector icons. The whole of
/// `/v1/portal` is the Portal's: the two Community routers are not under
/// it (`/v1/approvals`, `/v1/runs`).
pub(crate) const ENTERPRISE_ROUTE_PREFIXES: &[&str] = &[
    "/v1/proxy/batch",
    "/v1/public/tenants",
    "/v1/portal",
    "/v1/portal/auth",
    "/v1/portal/alerts",
    "/v1/portal/evidence",
    "/v1/portal/models",
    "/v1/portal/skills",
    "/v1/portal/servers",
    "/v1/portal/mcp-apps",
    "/v1/portal/notifications",
    "/v1/portal/settings",
    "/v1/portal/knowledgebases",
    "/v1/portal/kb-identities",
    // The knowledge base's connector icons: an Enterprise prefix under the
    // Community one (`/v1/assets`), mounted by the knowledge-base extension.
    "/v1/assets/connector-icons",
];

/// Is the row (`METHOD /path`) a route under `prefix`: the prefix itself or
/// a path below it, never a sibling that only starts alike?
pub(crate) fn is_under(row: &str, prefix: &str) -> bool {
    let path = row.split(' ').nth(1).unwrap_or(row);
    path == prefix || path.starts_with(&format!("{prefix}/"))
}

/// The rows of a route table that are under an Enterprise prefix.
pub(crate) fn enterprise_rows(rows: &[String]) -> Vec<String> {
    rows.iter()
        .filter(|row| ENTERPRISE_ROUTE_PREFIXES.iter().any(|p| is_under(row, p)))
        .cloned()
        .collect()
}

/// The Community gateway serves nothing under an Enterprise prefix —
/// the assembled application, and the recording it is compared with.
#[tokio::test]
async fn the_community_route_table_has_no_enterprise_prefix() {
    let app = common::community_app(unconnected_pool());
    let live = mounted_routes(&app);
    assert!(
        live.len() > 50,
        "the Community assembly: {} routes",
        live.len()
    );
    assert_eq!(
        enterprise_rows(&live),
        Vec::<String>::new(),
        "the Community assembly mounts a route under an Enterprise prefix"
    );
    assert_eq!(
        enterprise_rows(&recorded(common::community_root(), "community")),
        Vec::<String>::new(),
        "the Community recording holds a route under an Enterprise prefix"
    );
}

#[test]
fn the_prefix_rule_reads_whole_segments() {
    let rows: Vec<String> = [
        "GET /v1/portal/auth/login",
        "POST /v1/proxy/batch",
        "GET /v1/public/tenants/{id}/theme",
        "GET /v1/portal",
        "GET /v1/assets/connector-icons/{filename}",
        // Community routes that only start alike.
        "GET /v1/assets/mcp-servers/{server_id}/icon",
        "GET /v1/assets/connector-icons-of-nothing",
        "GET /v1/proxy/batches-of-nothing",
        "GET /v1/portals",
        "GET /v1/approvals/{id}/poll",
        "GET /v1/runs/{id}",
        "POST /v1/proxy/llm/chat/completions",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(enterprise_rows(&rows), rows[..5].to_vec());
}

#[test]
fn the_reader_reads_the_shapes_axum_prints() {
    use axum::routing::{any, any_service, delete, get, post};
    async fn h() -> &'static str {
        "x"
    }
    let service = || {
        tower::service_fn(|_: axum::extract::Request| async {
            Ok::<_, std::convert::Infallible>(axum::response::Response::new(
                axum::body::Body::empty(),
            ))
        })
    };
    let nested: Router<()> = Router::new()
        .route("/jobs", post(h))
        .route("/jobs", get(h))
        .route("/jobs/{id}", delete(h).patch(h))
        .route("/", get(h));
    let app: Router = Router::new()
        .route("/health", get(h).head(h))
        .route("/mcp/{server_id}/{*path}", any(h))
        .route("/both", any(h).get(h))
        .route_service("/svc", any_service(service()))
        .nest("/v1/nested", nested)
        .merge(Router::new().route("/merged", post(h).put(h)))
        .fallback(h)
        .layer(tower_http::trace::TraceLayer::new_for_http())
        .with_state(());
    assert_eq!(
        mounted_routes(&app),
        vec![
            "ANY /both",
            "GET /both",
            "GET /health",
            "HEAD /health",
            "ANY /mcp/{server_id}/{*path}",
            "POST /merged",
            "PUT /merged",
            "SERVICE /svc",
            // the root of a nested router is served at the prefix, not at `prefix/`
            "GET /v1/nested",
            "GET /v1/nested/jobs",
            "POST /v1/nested/jobs",
            "DELETE /v1/nested/jobs/{id}",
            "PATCH /v1/nested/jobs/{id}",
        ]
    );
    // Before the state is given, a handler is boxed, not yet a route: read
    // the same.
    let stateless: Router<()> = Router::new().route("/a", get(h)).route("/b", any(h));
    assert_eq!(mounted_routes(&stateless), vec!["GET /a", "ANY /b"]);
    // A removed route, a moved one and a changed method are each a difference.
    let recorded: Vec<String> = ["GET /a", "POST /a", "GET /b"].map(String::from).to_vec();
    let actual: Vec<String> = ["GET /a", "PUT /a", "GET /c"].map(String::from).to_vec();
    assert_eq!(
        differences(&recorded, &actual),
        vec!["- POST /a", "- GET /b", "+ PUT /a", "+ GET /c"]
    );
}
