// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! SQL analyzer (sqlparser).
//!
//! Returns statement category(ies), tables read/written, subquery
//! depth, and whether the payload was multi-statement.

use serde_json::Value;
use sqlparser::ast::{
    Expr, FromTable, ObjectName, Query, SetExpr, Statement, TableFactor, visit_expressions,
    visit_relations,
};
use sqlparser::dialect::{
    AnsiDialect, Dialect, GenericDialect, MsSqlDialect, MySqlDialect, PostgreSqlDialect,
    SQLiteDialect, SnowflakeDialect,
};
use sqlparser::parser::Parser;
use std::ops::ControlFlow;

use super::{ArgFacts, ArgKindTag};

fn dialect_for(config: Option<&Value>) -> Box<dyn Dialect + Send + Sync> {
    let name = config
        .and_then(|c| c.get("dialect"))
        .and_then(|d| d.as_str())
        .unwrap_or("generic")
        .to_lowercase();
    match name.as_str() {
        "postgres" | "postgresql" => Box::new(PostgreSqlDialect {}),
        "mysql" => Box::new(MySqlDialect {}),
        "sqlite" => Box::new(SQLiteDialect {}),
        "mssql" | "sqlserver" => Box::new(MsSqlDialect {}),
        "snowflake" => Box::new(SnowflakeDialect {}),
        "ansi" => Box::new(AnsiDialect {}),
        _ => Box::new(GenericDialect {}),
    }
}

pub fn analyze(input: &str, config: Option<&Value>) -> ArgFacts {
    let mut facts = ArgFacts {
        kind: ArgKindTag::Sql,
        raw_excerpt: super::truncate_excerpt(input),
        ..Default::default()
    };

    let trimmed = input.trim();
    if trimmed.is_empty() {
        facts.sql_parse_error = true;
        return facts;
    }

    let dialect = dialect_for(config);
    let statements = match Parser::parse_sql(&*dialect, trimmed) {
        Ok(s) => s,
        Err(_e) => {
            facts.sql_parse_error = true;
            return facts;
        }
    };

    facts.sql_multi_statement = statements.len() > 1;

    for stmt in &statements {
        // Statement type label
        facts
            .sql_statement_types
            .push(statement_label(stmt).to_string());

        // Tables touched (read vs write)
        collect_tables(
            stmt,
            &mut facts.sql_tables_read,
            &mut facts.sql_tables_written,
        );

        // Subquery depth — walk Query nodes recursively
        let depth = match stmt {
            Statement::Query(q) => query_subquery_depth(q.as_ref(), 0),
            _ => {
                // INSERT … SELECT etc. — look at inner queries
                let mut max = 0u32;
                let _: ControlFlow<()> = visit_expressions(stmt, |expr: &Expr| {
                    if let Expr::Subquery(q) = expr {
                        let d = query_subquery_depth(q.as_ref(), 1);
                        if d > max {
                            max = d;
                        }
                    }
                    ControlFlow::Continue(())
                });
                max
            }
        };
        if depth > facts.sql_subquery_depth {
            facts.sql_subquery_depth = depth;
        }
    }

    // Dedup tables (visitor may yield duplicates)
    facts.sql_tables_read.sort();
    facts.sql_tables_read.dedup();
    facts.sql_tables_written.sort();
    facts.sql_tables_written.dedup();

    facts
}

fn statement_label(stmt: &Statement) -> &'static str {
    match stmt {
        Statement::Query(_) => "SELECT",
        Statement::Insert(_) => "INSERT",
        Statement::Update { .. } => "UPDATE",
        Statement::Delete(_) => "DELETE",
        Statement::Merge { .. } => "MERGE",
        Statement::CreateTable { .. }
        | Statement::CreateIndex { .. }
        | Statement::CreateView { .. }
        | Statement::CreateSchema { .. }
        | Statement::CreateDatabase { .. } => "DDL_CREATE",
        Statement::Drop { .. } => "DDL_DROP",
        Statement::AlterTable { .. } | Statement::AlterIndex { .. } => "DDL_ALTER",
        Statement::Truncate { .. } => "TRUNCATE",
        Statement::Grant { .. } | Statement::Revoke { .. } => "PERMISSION",
        Statement::Copy { .. } => "COPY",
        Statement::SetVariable { .. } | Statement::SetTimeZone { .. } => "SET",
        Statement::ShowVariable { .. } => "SHOW",
        Statement::Commit { .. }
        | Statement::Rollback { .. }
        | Statement::StartTransaction { .. } => "TX",
        Statement::Explain { .. } => "EXPLAIN",
        _ => "UNKNOWN",
    }
}

/// Whether a statement label corresponds to a write effect.
pub fn label_writes(label: &str) -> bool {
    matches!(
        label,
        "INSERT"
            | "UPDATE"
            | "DELETE"
            | "MERGE"
            | "DDL_CREATE"
            | "DDL_DROP"
            | "DDL_ALTER"
            | "TRUNCATE"
            | "PERMISSION"
            | "COPY"
    )
}

fn collect_tables(
    stmt: &Statement,
    tables_read: &mut Vec<String>,
    tables_written: &mut Vec<String>,
) {
    match stmt {
        Statement::Insert(ins) => {
            tables_written.push(ins.table_name.to_string());
            if let Some(src) = &ins.source {
                let _: ControlFlow<()> = visit_relations(src.as_ref(), |r: &ObjectName| {
                    tables_read.push(r.to_string());
                    ControlFlow::Continue(())
                });
            }
        }
        Statement::Update {
            table,
            from,
            selection,
            ..
        } => {
            let _: ControlFlow<()> = visit_relations(table, |r: &ObjectName| {
                tables_written.push(r.to_string());
                ControlFlow::Continue(())
            });
            if let Some(f) = from {
                let _: ControlFlow<()> = visit_relations(f, |r: &ObjectName| {
                    tables_read.push(r.to_string());
                    ControlFlow::Continue(())
                });
            }
            if let Some(sel) = selection {
                let _: ControlFlow<()> = visit_relations(sel, |r: &ObjectName| {
                    tables_read.push(r.to_string());
                    ControlFlow::Continue(())
                });
            }
        }
        Statement::Delete(del) => {
            // FromTable wraps either `WithFromKeyword(Vec<TableWithJoins>)`
            // or `WithoutKeyword(Vec<TableWithJoins>)` — both reach the
            // same set of relations through visit_relations.
            match &del.from {
                FromTable::WithFromKeyword(v) | FromTable::WithoutKeyword(v) => {
                    let _: ControlFlow<()> = visit_relations(v, |r: &ObjectName| {
                        tables_written.push(r.to_string());
                        ControlFlow::Continue(())
                    });
                }
            }
            if let Some(u) = &del.using {
                let _: ControlFlow<()> = visit_relations(u, |r: &ObjectName| {
                    tables_read.push(r.to_string());
                    ControlFlow::Continue(())
                });
            }
            if let Some(sel) = &del.selection {
                let _: ControlFlow<()> = visit_relations(sel, |r: &ObjectName| {
                    tables_read.push(r.to_string());
                    ControlFlow::Continue(())
                });
            }
        }
        Statement::Query(q) => {
            let _: ControlFlow<()> = visit_relations(q.as_ref(), |r: &ObjectName| {
                tables_read.push(r.to_string());
                ControlFlow::Continue(())
            });
        }
        other => {
            let _: ControlFlow<()> = visit_relations(other, |r: &ObjectName| {
                tables_read.push(r.to_string());
                ControlFlow::Continue(())
            });
        }
    }
}

fn query_subquery_depth(q: &Query, current: u32) -> u32 {
    let mut max = current;
    let depth_here = match q.body.as_ref() {
        SetExpr::Select(select) => {
            let mut d = current;
            for table_with_join in &select.from {
                if let TableFactor::Derived { subquery, .. } = &table_with_join.relation {
                    let sub = query_subquery_depth(subquery.as_ref(), current + 1);
                    if sub > d {
                        d = sub;
                    }
                }
                for join in &table_with_join.joins {
                    if let TableFactor::Derived { subquery, .. } = &join.relation {
                        let sub = query_subquery_depth(subquery.as_ref(), current + 1);
                        if sub > d {
                            d = sub;
                        }
                    }
                }
            }
            d
        }
        SetExpr::Query(inner) => query_subquery_depth(inner.as_ref(), current + 1),
        _ => current,
    };
    if depth_here > max {
        max = depth_here;
    }
    max
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn pg() -> Option<Value> {
        Some(json!({"dialect": "postgresql"}))
    }

    #[test]
    fn select_is_not_a_write() {
        let f = analyze("SELECT * FROM users WHERE id = 1", pg().as_ref());
        assert!(!f.sql_parse_error);
        assert_eq!(f.sql_statement_types, vec!["SELECT"]);
        assert!(!f.sql_statement_types.iter().any(|s| label_writes(s)));
        assert_eq!(f.sql_tables_read, vec!["users"]);
        assert!(f.sql_tables_written.is_empty());
    }

    #[test]
    fn delete_is_a_write_and_records_target_table() {
        let f = analyze("DELETE FROM orders WHERE id = 42", pg().as_ref());
        assert_eq!(f.sql_statement_types, vec!["DELETE"]);
        assert!(f.sql_statement_types.iter().any(|s| label_writes(s)));
        assert_eq!(f.sql_tables_written, vec!["orders"]);
    }

    #[test]
    fn insert_select_records_both_read_and_write() {
        let f = analyze(
            "INSERT INTO audit_log SELECT * FROM source_table",
            pg().as_ref(),
        );
        assert_eq!(f.sql_statement_types, vec!["INSERT"]);
        assert_eq!(f.sql_tables_written, vec!["audit_log"]);
        assert_eq!(f.sql_tables_read, vec!["source_table"]);
    }

    #[test]
    fn update_records_target_as_write() {
        let f = analyze("UPDATE users SET name = 'x' WHERE id = 1", pg().as_ref());
        assert_eq!(f.sql_statement_types, vec!["UPDATE"]);
        assert_eq!(f.sql_tables_written, vec!["users"]);
    }

    #[test]
    fn ddl_create_table_classified_as_write() {
        let f = analyze("CREATE TABLE x (id INT)", pg().as_ref());
        assert!(label_writes(&f.sql_statement_types[0]));
    }

    #[test]
    fn ddl_drop_classified_as_write() {
        let f = analyze("DROP TABLE x", pg().as_ref());
        assert!(label_writes(&f.sql_statement_types[0]));
    }

    #[test]
    fn truncate_classified_as_write() {
        let f = analyze("TRUNCATE TABLE x", pg().as_ref());
        assert!(label_writes(&f.sql_statement_types[0]));
    }

    #[test]
    fn multi_statement_payload_detected() {
        let f = analyze(
            "SELECT 1; DELETE FROM orders WHERE id = 1; SELECT 2",
            pg().as_ref(),
        );
        assert!(f.sql_multi_statement);
        assert_eq!(f.sql_statement_types.len(), 3);
        assert!(f.sql_tables_written.contains(&"orders".to_string()));
    }

    #[test]
    fn subquery_depth_simple_one_level() {
        let f = analyze("SELECT * FROM (SELECT id FROM users) AS u", pg().as_ref());
        assert_eq!(f.sql_subquery_depth, 1);
    }

    #[test]
    fn subquery_depth_nested_three_levels() {
        let f = analyze(
            "SELECT * FROM (SELECT id FROM (SELECT id FROM (SELECT id FROM users) AS a) AS b) AS c",
            pg().as_ref(),
        );
        assert!(f.sql_subquery_depth >= 3);
    }

    #[test]
    fn parse_error_flag_set_on_garbage() {
        let f = analyze("THIS IS NOT SQL AT ALL!!!", pg().as_ref());
        assert!(f.sql_parse_error);
    }

    #[test]
    fn empty_input_is_a_parse_error() {
        let f = analyze("", pg().as_ref());
        assert!(f.sql_parse_error);
    }

    #[test]
    fn whitespace_evasion_does_not_hide_delete() {
        // SQL parser is whitespace-insensitive — this is good.
        let f = analyze("  \n DELETE \t FROM users WHERE id = 1", pg().as_ref());
        assert_eq!(f.sql_statement_types, vec!["DELETE"]);
    }

    #[test]
    fn case_evasion_does_not_hide_delete() {
        let f = analyze("dElEtE FrOm users WHERE id = 1", pg().as_ref());
        assert_eq!(f.sql_statement_types, vec!["DELETE"]);
    }

    #[test]
    fn comment_evasion_does_not_hide_intent() {
        // Comment between keywords — sqlparser handles it.
        let f = analyze(
            "DELETE /* comment */ FROM users WHERE id = 1",
            pg().as_ref(),
        );
        assert_eq!(f.sql_statement_types, vec!["DELETE"]);
    }

    #[test]
    fn quoted_identifier_writes_recorded() {
        let f = analyze(r#"DELETE FROM "Users" WHERE id = 1"#, pg().as_ref());
        assert_eq!(f.sql_statement_types, vec!["DELETE"]);
    }

    #[test]
    fn unsupported_dialect_falls_back_to_generic() {
        let f = analyze("SELECT 1", Some(&json!({"dialect": "totallymadeup"})));
        assert!(!f.sql_parse_error);
    }
}
