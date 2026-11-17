/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The expression reference lists exactly the facts the evaluator binds.
 *
 * The source of truth is `RUN_FACT_KEYS` in the gateway core
 * (`src/services/run_ledger.rs`). When the core is beside this repository
 * (`../brutor-gateway-core`, or `../core-gateway` in the public repository;
 * scripts/sibling-repositories.mjs), the lists must match exactly: a fact
 * added there and not documented here, or documented here and not bound
 * there, fails. In a standalone checkout of this repo that comparison is
 * skipped and only the local invariants run.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { siblingForTest } from '../../../scripts/sibling-repositories.mjs';
import { CHECK_EXAMPLES, RUN_FACT_GROUPS, RUN_FACT_KEYS } from '../runFacts';

const CORE_ROOT = siblingForTest('core-gateway', 'run facts reference matches the core evaluator’s RUN_FACT_KEYS exactly');

function coreKeys(core: string): string[] {
  const src = readFileSync(resolve(core, 'src/services/run_ledger.rs'), 'utf8');
  const start = src.indexOf('pub const RUN_FACT_KEYS: &[&str] = &[');
  const body = src.slice(start, src.indexOf('];', start));
  return [...body.matchAll(/"([a-z0-9_]+)"/g)].map((m) => m[1]);
}

describe('run facts reference', () => {
  it('has no duplicate keys and every fact is described', () => {
    expect(new Set(RUN_FACT_KEYS).size).toBe(RUN_FACT_KEYS.length);
    for (const g of RUN_FACT_GROUPS) {
      for (const f of g.facts) expect(f.description.length).toBeGreaterThan(5);
    }
  });

  it.skipIf(CORE_ROOT === null)('matches the core evaluator’s RUN_FACT_KEYS exactly', () => {
    expect([...RUN_FACT_KEYS].sort()).toEqual(coreKeys(CORE_ROOT ?? '').sort());
  });

  it('examples only read documented facts', () => {
    const undocumented = CHECK_EXAMPLES.flatMap((ex) =>
      [...ex.expression.matchAll(/\brun\.([a-z0-9_]+)/g)]
        .map((m) => m[1])
        .filter((key) => !RUN_FACT_KEYS.includes(key))
        .map((key) => `${ex.title}: run.${key}`),
    );
    expect(undocumented).toEqual([]);
  });
});
