// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the Enterprise-data check (`services::edition_guard`)
//! runs at start, before anything is served and before a background task
//! runs; and the edition it decides from is what was composed.
//!
//! Why a source scan as well as the behaviour tests
//! (`tests/integration/edition_guard_start.rs`): the one build of this tree
//! composes the Enterprise Edition, so `GatewayBuilder::run` never refuses in
//! a test. What the tests drive is `assemble_checked`; this guard holds
//! `build` and `run` to it, in the order that matters. Each rule is proven
//! against the shape it forbids ([`the_guard_catches_the_shapes_it_forbids`]).

#[path = "support/roots.rs"]
mod roots;

use std::fs;

/// The body of `fn name` in `source`: from its opening brace to the matching
/// closing one.
fn body_of<'a>(source: &'a str, name: &str) -> &'a str {
    let at = source
        .find(&format!("fn {name}("))
        .unwrap_or_else(|| panic!("no fn {name}"));
    let open = at + source[at..].find('{').expect("opening brace");
    let mut depth = 0usize;
    for (i, c) in source[open..].char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return &source[open..=open + i];
                }
            }
            _ => {}
        }
    }
    panic!("fn {name}: unbalanced braces");
}

/// Code only: comment lines dropped, so prose that names a call is not one.
fn code(text: &str) -> String {
    text.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// What is wrong with the builder's start sequence; empty when it is right.
fn start_sequence_faults(builder: &str) -> Vec<String> {
    let mut faults = Vec::new();
    let production = builder.split("#[cfg(test)]").next().unwrap_or("");

    // 1. The checked assembly makes the check and passes its refusal on.
    let checked = code(body_of(production, "assemble_checked"));
    let assembled = checked.find("Self::assemble(");
    let enforced = checked.find("edition_guard::enforce_at_start(&gateway.state).await");
    match (assembled, enforced) {
        (Some(a), Some(e)) if a < e => {}
        _ => faults.push(
            "assemble_checked does not assemble and then call edition_guard::enforce_at_start"
                .to_string(),
        ),
    }
    if !checked.contains("if let Err(refusal) =") || !checked.contains("return Err(refusal.into())")
    {
        faults.push("assemble_checked does not return the refusal as the error".to_string());
    }

    // 1b. The extensions' steps: preparation before the assembly (it reads
    //     nothing of the installation's data: the state is not checked
    //     yet), and the step after assembly only after the check. An
    //     extension that reads the installation (a warm-up) does it there.
    let prepared = checked.find("ext.prepare(&mut app_state)");
    let after = checked.find("ext.after_assembly(&gateway.state)");
    match (prepared, assembled, enforced, after) {
        (Some(p), Some(a), Some(e), Some(s)) if p < a && a < e && e < s => {}
        _ => faults.push(
            "assemble_checked does not run prepare, assemble, the edition check and \
             after_assembly in that order"
                .to_string(),
        ),
    }
    if checked.matches("ext.after_assembly(").count() != 1 {
        faults.push("after_assembly is run in more than one place".to_string());
    }
    let build_body = code(body_of(production, "build"));
    for forbidden in ["after_assembly(", ".prepare("] {
        if build_body.contains(forbidden) {
            faults.push(format!(
                "build contains `{forbidden}`: the extensions' steps belong to assemble_checked"
            ));
        }
    }

    // 2. `build` assembles through it and in no other way, before it reads
    //    anything of the installation.
    let build = code(body_of(production, "build"));
    if build
        .matches("Self::assemble_checked(app_state, self.extensions).await?")
        .count()
        != 1
    {
        faults
            .push("build does not assemble through assemble_checked (once, with `?`)".to_string());
    }
    if build.contains("Self::assemble(") {
        faults.push("build assembles without the check".to_string());
    }
    if build.contains("enforce_at_start") {
        faults.push("build makes the check itself instead of through assemble_checked".to_string());
    }
    if let Some(at) = build.find("Self::assemble_checked(") {
        // (The warm-up of the response cache's tenant switches is not
        // in this list: it is the extension's step after assembly; rule
        // 1b holds that step to the same place, after the check.)
        for later in ["FROM llm_model", "warn_on_deployment_drift("] {
            match build.find(later) {
                Some(l) if l > at => {}
                Some(_) => faults.push(format!("build reads `{later}` before the edition check")),
                None => faults.push(format!(
                    "build no longer contains `{later}` (update the guard)"
                )),
            }
        }
    }
    for forbidden in [
        "start_tasks(",
        "TcpListener::bind(",
        "axum::serve(",
        "tokio::spawn(",
    ] {
        if build.contains(forbidden) {
            faults.push(format!(
                "build contains `{forbidden}`: serving and tasks belong to run"
            ));
        }
    }

    // 3. `run` builds first; tasks and listeners only after.
    let run = code(body_of(production, "run"));
    match run.find("self.build().await?") {
        None => faults.push("run does not start with self.build().await?".to_string()),
        Some(built) => {
            for after in [
                "start_tasks(",
                "TcpListener::bind(",
                "axum::serve(",
                "tokio::spawn(",
            ] {
                match run.find(after) {
                    Some(a) if a > built => {}
                    Some(_) => {
                        faults.push(format!("run reaches `{after}` before the build is checked"))
                    }
                    None => faults.push(format!(
                        "run no longer contains `{after}` (update the guard)"
                    )),
                }
            }
        }
    }
    for forbidden in ["Self::assemble(", "assemble_checked(", "enforce_at_start"] {
        if run.contains(forbidden) {
            faults.push(format!(
                "run contains `{forbidden}`: it serves what build returned"
            ));
        }
    }

    // 4. Nothing else in the builder starts tasks or serves.
    if code(production).matches("gateway.start_tasks()").count() != 1 {
        faults.push("tasks are started in more than one place".to_string());
    }
    faults
}

/// What is wrong with how `main` reaches the builder; empty when it is
/// `GatewayBuilder::new(config).telemetry(telemetry)`, any number of
/// `.extension(...)`, then `.run().await`, and nothing else.
fn main_chain_faults(main: &str) -> Vec<String> {
    let flat: String = code(main).split_whitespace().collect();
    let start = "GatewayBuilder::new(config).telemetry(telemetry)";
    let Some(at) = flat.find(start) else {
        return vec![format!("main does not contain `{start}`")];
    };
    let mut rest = &flat[at + start.len()..];
    while let Some(after) = rest.strip_prefix(".extension(") {
        // to the parenthesis that closes the call
        let mut depth = 1usize;
        let mut end = None;
        for (i, c) in after.char_indices() {
            match c {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        end = Some(i);
                        break;
                    }
                }
                _ => {}
            }
        }
        let Some(end) = end else {
            return vec!["an `.extension(` call in main is not closed".to_string()];
        };
        rest = &after[end + 1..];
    }
    if !rest.starts_with(".run().await") {
        let shown: String = rest.chars().take(40).collect();
        return vec![format!(
            "main's builder chain goes on with `{shown}` where `.run().await` belongs"
        )];
    }
    Vec::new()
}

#[test]
fn the_check_is_made_before_anything_is_served_or_started() {
    let builder = fs::read_to_string("src/builder.rs").unwrap();
    let faults = start_sequence_faults(&builder);
    assert!(faults.is_empty(), "{faults:#?}");
    // And `main` reaches the builder through `run` only: one chain from
    // `new` to `run`, with nothing between but the extensions it composes.
    let main = fs::read_to_string("src/main.rs").unwrap();
    let faults = main_chain_faults(&main);
    assert!(faults.is_empty(), "{faults:#?}");
    for good in [
        // what `main` was before the licence became an extension: still right
        "    GatewayBuilder::new(config).telemetry(telemetry).run().await\n",
        "    GatewayBuilder::new(config)\n        .telemetry(telemetry)\n        .extension(A::new())\n        .extension(B::from_env())\n        .run()\n        .await\n",
    ] {
        assert!(main_chain_faults(good).is_empty(), "{good}");
    }
    for bad in [
        "    let gateway = GatewayBuilder::new(config).telemetry(telemetry).build().await?;\n",
        "    GatewayBuilder::new(config).telemetry(telemetry).extension(A::new());\n",
        "    GatewayBuilder::new(config)\n        .telemetry(telemetry)\n        .extension(A::new())\n        .skip_checks()\n        .run()\n        .await\n",
        "    let b = GatewayBuilder::new(config).telemetry(telemetry);\n    b.run().await\n",
    ] {
        assert!(!main_chain_faults(bad).is_empty(), "not caught: {bad}");
    }
    for forbidden in [".build()", "assemble("] {
        assert!(
            !code(&main).contains(forbidden),
            "main contains `{forbidden}`"
        );
    }
}

#[test]
fn the_guard_catches_the_shapes_it_forbids() {
    let good = fs::read_to_string("src/builder.rs").unwrap();
    assert!(start_sequence_faults(&good).is_empty());
    let planted: Vec<(&str, String)> = vec![
        (
            "the unchecked assembly",
            good.replace(
                "Self::assemble_checked(app_state, self.extensions).await?",
                "Self::assemble(app_state, self.extensions)?",
            ),
        ),
        (
            "the check dropped",
            good.replace(
                "if let Err(refusal) = services::edition_guard::enforce_at_start(&gateway.state).await {",
                "if let Err(refusal) = Ok::<(), services::edition_guard::Refusal>(()) {",
            ),
        ),
        (
            "the refusal logged and ignored",
            good.replace("            return Err(refusal.into());\n", ""),
        ),
        (
            "tasks before the build",
            good.replace(
                "        let gateway = self.build().await?;\n        let state = gateway.state.clone();",
                "        gateway.start_tasks();\n        let gateway = self.build().await?;\n        let state = gateway.state.clone();",
            ),
        ),
        (
            "the check after the listener",
            good.replace(
                "        tracing::info!(\"Listening on {}\", config.listen_addr);",
                "        tracing::info!(\"Listening on {}\", config.listen_addr);\n        services::edition_guard::enforce_at_start(&state).await?;",
            ),
        ),
        (
            "a read before the check",
            {
                let at = good.find("        // Assembled, and").expect("marker");
                let drift = "        services::evidence::key::warn_on_deployment_drift(&app_state.db).await;\n";
                format!("{}{drift}{}", &good[..at], &good[at..])
            },
        ),
        (
            "the extensions' step after assembly before the check",
            {
                let step = "        for ext in &gateway.installed {\n            ext.after_assembly(&gateway.state)";
                let check = "        if let Err(refusal) = services::edition_guard::enforce_at_start(&gateway.state).await {";
                assert!(good.contains(step) && good.contains(check));
                // move the check to the end of the function: after the step
                let without = good.replacen(
                    "        if let Err(refusal) = services::edition_guard::enforce_at_start(&gateway.state).await {\n            tracing::error!(\"{refusal}\");\n            return Err(refusal.into());\n        }\n",
                    "",
                    1,
                );
                without.replacen(
                    "                })?;\n        }\n        Ok(gateway)\n    }\n\n    /// Build, start the tasks",
                    "                })?;\n        }\n        if let Err(refusal) = services::edition_guard::enforce_at_start(&gateway.state).await {\n            tracing::error!(\"{refusal}\");\n            return Err(refusal.into());\n        }\n        Ok(gateway)\n    }\n\n    /// Build, start the tasks",
                    1,
                )
            },
        ),
        (
            "a warm-up in build",
            good.replace(
                "        let state = &gateway.state;\n",
                "        let state = &gateway.state;\n        for ext in &gateway.installed { ext.after_assembly(state).await?; }\n",
            ),
        ),
    ];
    for (what, source) in planted {
        assert_ne!(source, good, "{what}: the planted shape did not apply");
        assert!(
            !start_sequence_faults(&source).is_empty(),
            "{what}: not caught"
        );
    }
}

fn rust_files(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|x| x == "rs") {
            out.push(path);
        }
    }
}

/// What installs an Enterprise piece: a write to one of the state's hooks,
/// or a registration in one of its registries.
///
/// The Enterprise pieces also install through the registries
/// the extensions use: the extension state, the group cache's
/// loaders, the console's cache layers. Each is an install like the hooks.
const INSTALLS: &[&str] = &[
    "state.response_cache =",
    "state.prompt_cache_policy =",
    "state.context_enricher =",
    ".credential_verifiers .register(",
    ".credential_verifiers.register(",
    ".credential_verifiers .register_admin_type(",
    ".credential_verifiers.register_admin_type(",
    ".tenant_paths .register(",
    ".tenant_paths.register(",
    "state.extensions.insert(",
    "state.extensions .insert(",
    ".group_cache .register_loader(",
    ".group_cache.register_loader(",
    ".cache_layers .register(",
    ".cache_layers.register(",
    // The answer to the statistics request of a composition that
    // runs as several instances.
    ".cache_stats_responder .install(",
    ".cache_stats_responder.install(",
];

/// The file without its unit tests, comment lines dropped and every run of
/// white space one space, so that a call rustfmt broke over lines reads as
/// one.
fn flat_production(text: &str) -> String {
    let production = text.split("#[cfg(test)]\nmod tests").next().unwrap_or("");
    code(production)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Functions that install something on the state and do not say which
/// Enterprise feature they are.
fn silent_installs(rel: &str, text: &str) -> Vec<String> {
    let production = flat_production(text);
    let production = production.as_str();
    let mut silent = Vec::new();
    for needle in INSTALLS {
        let mut from = 0;
        while let Some(found) = production[from..].find(needle) {
            let at = from + found;
            from = at + needle.len();
            // The enclosing function: the last `fn ` before the write.
            let start = production[..at].rfind("fn ").expect("inside a function");
            let name_end = start + production[start..].find('(').expect("fn name");
            let name = &production[start + 3..name_end];
            let body = body_of(&production[start..], name);
            if !body.contains("composition.enterprise_piece(\"") {
                silent.push(format!("{rel}: fn {name} ({needle:?})"));
            }
        }
    }
    silent
}

/// The edition is what is composed: whatever installs an Enterprise piece
/// records its feature in the state's composition in the same function.
/// `AppState::new` (`src/services/mod.rs`) sets the Community defaults and
/// the registries (`credential_verifier.rs`, `tenant_paths.rs`) fill in the
/// Community entries; those are not installs.
#[test]
fn every_enterprise_install_says_so() {
    let mut files = Vec::new();
    rust_files(std::path::Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut silent = Vec::new();
    let mut installs = 0;
    for file in files {
        let rel = file.to_string_lossy().replace('\\', "/");
        if [
            "src/services/mod.rs",
            "src/services/credential_verifier.rs",
            "src/services/tenant_paths.rs",
        ]
        .contains(&rel.as_str())
        {
            continue;
        }
        let text = fs::read_to_string(&file).unwrap();
        let production = flat_production(&text);
        installs += INSTALLS
            .iter()
            .map(|n| production.matches(n).count())
            .sum::<usize>();
        silent.extend(silent_installs(&rel, &text));
    }
    // The installs are the Enterprise workspace's: counted where it is here
    // (`tests/support/roots.rs`); Community code alone has none.
    let least = if roots::tree("enterprise").is_some() {
        5
    } else {
        0
    };
    assert!(
        installs >= least,
        "the scan found {installs} installs: it reads nothing"
    );
    assert!(
        silent.is_empty(),
        "an Enterprise piece is installed without `state.composition.enterprise_piece(..)`, so \
         a process holding it would still count as the Community Edition and refuse to start \
         on its own database:\n{silent:#?}"
    );
}

#[test]
fn the_install_scan_catches_a_silent_install() {
    let silent = silent_installs(
        "src/x.rs",
        "impl X {\n    pub fn install(state: &mut AppState) {\n        state.response_cache = \
         Arc::new(Self);\n    }\n}\n",
    );
    assert_eq!(silent.len(), 1, "{silent:?}");
    let said = silent_installs(
        "src/x.rs",
        "impl X {\n    pub fn install(state: &mut AppState) {\n        state.response_cache = \
         Arc::new(Self);\n        state.composition.enterprise_piece(\"response_cache\");\n    \
         }\n}\n",
    );
    assert!(said.is_empty(), "{said:?}");
    // A comment that names the call is not the call.
    let commented = silent_installs(
        "src/x.rs",
        "fn install(state: &mut AppState) {\n    state.context_enricher = Arc::new(X);\n    // \
         state.composition.enterprise_piece(\"knowledge_base\");\n}\n",
    );
    assert_eq!(commented.len(), 1);
}

/// The Community Edition's check has no way round it: nothing in the module
/// reads the environment or a configuration value.
#[test]
fn the_check_has_no_override() {
    let guard = fs::read_to_string("src/services/edition_guard.rs").unwrap();
    let production = code(guard.split("#[cfg(test)]").next().unwrap_or(""));
    for forbidden in [
        "std::env",
        "env::var",
        "state.config",
        "AppConfig",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
    ] {
        assert!(
            !production.contains(forbidden),
            "edition_guard contains `{forbidden}`: the check takes no override and writes nothing"
        );
    }
    let edition = fs::read_to_string("src/services/edition.rs").unwrap();
    let production = code(edition.split("#[cfg(test)]").next().unwrap_or(""));
    for forbidden in ["std::env", "env::var", "cfg!(", "#[cfg(feature"] {
        assert!(
            !production.contains(forbidden),
            "edition contains `{forbidden}`: the edition is what is composed, not a flag"
        );
    }
}
