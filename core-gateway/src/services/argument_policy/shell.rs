// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Shell command analyzer.
//!
//! Uses shellwords::split (POSIX-style) so quoted strings,
//! escapes, and embedded whitespace are handled correctly.
//! `shell_command` is the first argv token (after basename stripping
//! so `/usr/bin/rm` and `rm` are treated identically). `shell_flags`
//! is anything starting with `-` or `--`.

use std::path::Path;

use super::{ArgFacts, ArgKindTag};

pub fn analyze(input: &str) -> ArgFacts {
    let mut facts = ArgFacts {
        kind: ArgKindTag::Shell,
        raw_excerpt: super::truncate_excerpt(input),
        ..Default::default()
    };

    let trimmed = input.trim();
    if trimmed.is_empty() {
        return facts;
    }

    let argv = match shellwords::split(trimmed) {
        Ok(v) => v,
        Err(_) => {
            // Quoting error. The evaluator treats every shell predicate as
            // holding for input it could not split: `command_not_in`
            // reads `shell_command`, which stays unset here, so an allow-list
            // used to read "no command" as "not outside the list".
            facts.shell_argv = vec![trimmed.to_string()];
            facts.shell_parse_error = true;
            return facts;
        }
    };

    if argv.is_empty() {
        return facts;
    }

    // Strip directory components so /usr/bin/rm == rm.
    let cmd_raw = argv[0].as_str();
    let cmd_basename = Path::new(cmd_raw)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(cmd_raw)
        .to_string();

    facts.shell_command = Some(cmd_basename);

    facts.shell_flags = argv
        .iter()
        .skip(1)
        .filter(|t| t.starts_with('-'))
        .cloned()
        .collect();

    facts.shell_argv = argv;
    facts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simple_command_extracted() {
        let f = analyze("ls -la /tmp");
        assert_eq!(f.shell_command.as_deref(), Some("ls"));
        assert_eq!(f.shell_flags, vec!["-la"]);
        assert_eq!(f.shell_argv, vec!["ls", "-la", "/tmp"]);
    }

    #[test]
    fn absolute_path_command_uses_basename() {
        let f = analyze("/usr/bin/rm -rf /tmp/x");
        assert_eq!(f.shell_command.as_deref(), Some("rm"));
        assert_eq!(f.shell_flags, vec!["-rf"]);
    }

    #[test]
    fn quoted_arguments_preserved_as_single_token() {
        let f = analyze(r#"echo "hello world" foo"#);
        assert_eq!(f.shell_argv, vec!["echo", "hello world", "foo"]);
        assert!(f.shell_flags.is_empty());
    }

    #[test]
    fn long_and_short_flags_both_recognised() {
        let f = analyze("git push --force -v origin main");
        assert!(f.shell_flags.contains(&"--force".to_string()));
        assert!(f.shell_flags.contains(&"-v".to_string()));
    }

    #[test]
    fn no_flags_yields_empty_flag_list() {
        let f = analyze("cat /etc/hostname");
        assert!(f.shell_flags.is_empty());
        assert_eq!(f.shell_command.as_deref(), Some("cat"));
    }

    #[test]
    fn empty_input_yields_no_command() {
        let f = analyze("");
        assert!(f.shell_command.is_none());
    }

    #[test]
    fn whitespace_only_input_yields_no_command() {
        let f = analyze("   \t  \n");
        assert!(f.shell_command.is_none());
    }

    #[test]
    fn unbalanced_quote_falls_back_to_raw_token() {
        let f = analyze(r#"echo "unclosed"#);
        assert_eq!(f.shell_argv.len(), 1);
        assert!(f.shell_command.is_none());
    }

    #[test]
    fn equals_value_flag_recognised_as_flag() {
        let f = analyze("rsync --delete --exclude=*.tmp src/ dst/");
        assert!(f.shell_flags.contains(&"--delete".to_string()));
        assert!(f.shell_flags.contains(&"--exclude=*.tmp".to_string()));
    }
}
