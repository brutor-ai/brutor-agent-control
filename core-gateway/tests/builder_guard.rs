// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the gateway is assembled by `GatewayBuilder` and in
//! no other way.
//!
//! Why it matters: the builder is where a second distribution's extensions
//! are composed in. A route mounted, a task started, an invalidation
//! registry built or a server bound anywhere else would be outside that
//! composition — present in this binary and absent from the extended one, or
//! the other way round.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

#[test]
fn main_hands_over_to_the_builder_and_assembles_nothing_itself() {
    let main = fs::read_to_string("src/main.rs").unwrap();
    assert_eq!(
        main.matches("GatewayBuilder::new(config)").count(),
        1,
        "main builds the gateway once, through the builder"
    );
    for forbidden in [
        "AppState::new(",
        "axum::serve(",
        "app::router(",
        "start_all(",
        "PgPoolOptions",
        "connect_redis(",
        "tokio::spawn(",
    ] {
        assert!(
            !main.contains(forbidden),
            "main contains `{forbidden}`; the assembly belongs in src/builder.rs"
        );
    }
}

#[test]
fn the_listener_consults_the_composed_registry_not_the_builtin_one() {
    // The built-in registry is the default of a state built by hand
    // (`AppState::new`) and the starting point of the builder's composition.
    // The listener reads the composed one from the state, so an extension's
    // handlers are consulted.
    let tasks = fs::read_to_string("src/tasks.rs").unwrap();
    assert!(
        tasks.contains("handlers: state.invalidation.clone(),"),
        "the invalidation listener takes its handlers from AppState.invalidation"
    );
    assert!(
        !tasks.contains("InvalidationRegistry::builtin()"),
        "the listener must not rebuild the built-in registry"
    );
    let mut builders = Vec::new();
    for path in [
        "src/builder.rs",
        "src/services/mod.rs",
        "src/services/cache/invalidation.rs",
    ] {
        let n = fs::read_to_string(path)
            .unwrap()
            .matches("InvalidationRegistry::builtin()")
            .count();
        builders.push((path, n));
    }
    assert_eq!(
        builders,
        vec![
            ("src/builder.rs", 1),
            ("src/services/mod.rs", 1),
            ("src/services/cache/invalidation.rs", 0),
        ],
        "the built-in registry is constructed by the builder and by AppState::new only"
    );
    // nowhere else under src/
    let mut elsewhere = Vec::new();
    fn walk(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        for e in fs::read_dir(dir).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                walk(&p, out);
            } else if p.extension().is_some_and(|x| x == "rs") {
                out.push(p);
            }
        }
    }
    let mut files = Vec::new();
    walk(std::path::Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        walk(&enterprise.join("src"), &mut files);
    }
    for f in files {
        let rel = f.to_string_lossy().replace('\\', "/");
        if [
            "src/builder.rs",
            "src/services/mod.rs",
            "src/services/cache/invalidation.rs",
        ]
        .contains(&rel.as_str())
        {
            continue;
        }
        let text = fs::read_to_string(&f).unwrap();
        // production code only: a test module may build the registry for itself
        let text = text.split("#[cfg(test)]").next().unwrap_or("");
        for (i, line) in text.lines().enumerate() {
            if line.trim_start().starts_with("//") {
                continue;
            }
            if line.contains("InvalidationRegistry::builtin()") {
                elsewhere.push(format!("{rel}:{}", i + 1));
            }
        }
    }
    assert!(
        elsewhere.is_empty(),
        "built-in registry constructed outside the builder: {elsewhere:?}"
    );
}

#[test]
fn the_builder_composes_every_kind_of_contribution() {
    let builder = fs::read_to_string("src/builder.rs").unwrap();
    for (what, marker) in [
        ("interface version", "ext.interface_version()"),
        ("check before start", "ext.preflight(config)"),
        ("preparation", "ext.prepare(&mut app_state)"),
        ("state", "ext.install_state(&mut app_state)"),
        ("step after assembly", "ext.after_assembly(&gateway.state)"),
        ("routes", "e.routes()"),
        ("tasks", "ext.tasks()"),
        ("invalidation handlers", "ext.invalidation_handlers()"),
        ("openapi", "ext.openapi()"),
    ] {
        assert!(
            builder.contains(marker),
            "the builder does not compose an extension's {what}"
        );
    }
    assert_eq!(
        builder.matches("tasks::BACKGROUND_TASKS.to_vec()").count(),
        1
    );
    assert_eq!(builder.matches("app::router_with(").count(), 1);
}
