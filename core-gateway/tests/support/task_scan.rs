// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The scanners of `task_registry_guard.rs`, shared with the Enterprise
//! workspace's guard of the tasks its extensions contribute
//! (`enterprise/tests/extension_tasks_guard.rs`).

/// The lines of `src` that start something in the background: a spawn call
/// outside a comment.
pub fn spawn_sites(src: &str) -> Vec<&str> {
    src.lines()
        .map(str::trim)
        .filter(|line| !line.starts_with("//"))
        .filter(|line| {
            line.contains("tokio::spawn(")
                || line.contains(".spawn(")
                || line.contains("::spawn(")
                || line.contains("::spawn_")
        })
        .collect()
}

/// The body of each `fn name(ctx: &TaskContext<'_>)` in `src`.
pub fn task_functions(src: &str) -> Vec<(&str, &str)> {
    let mut found = Vec::new();
    for chunk in src.split("\nfn ").skip(1) {
        let Some((name, rest)) = chunk.split_once('(') else {
            continue;
        };
        if !(rest.starts_with("ctx: &TaskContext<'_>)")
            || rest.starts_with("_ctx: &TaskContext<'_>)"))
        {
            continue;
        }
        let body = rest
            .split_once("\n}\n")
            .map(|(body, _)| body)
            .unwrap_or(rest);
        found.push((name, body));
    }
    found
}
