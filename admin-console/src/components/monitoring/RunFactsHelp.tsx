/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The expression field's reference: every `run.*` fact (type, meaning,
 * known values), the allowed functions, and worked examples. Clicking a
 * fact inserts it; "Use" on an example fills the expression and output.
 */

import { useMemo, useState } from 'react';

import {
  CHECK_EXAMPLES,
  CHECK_FUNCTIONS,
  CheckExample,
  RUN_FACT_GROUPS,
} from '../../lib/runFacts';

interface RunFactsHelpProps {
  onInsert: (text: string) => void;
  onUseExample: (example: CheckExample) => void;
}

type Tab = 'facts' | 'examples' | 'functions';

export default function RunFactsHelp({ onInsert, onUseExample }: RunFactsHelpProps) {
  const [tab, setTab] = useState<Tab>('facts');
  const [query, setQuery] = useState('');

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return RUN_FACT_GROUPS;
    return RUN_FACT_GROUPS.map((g) => ({
      ...g,
      facts: g.facts.filter(
        (f) =>
          f.key.includes(q) ||
          f.description.toLowerCase().includes(q) ||
          (f.values ?? []).some((v) => v.includes(q)),
      ),
    })).filter((g) => g.facts.length > 0);
  }, [query]);

  const tabClass = (t: Tab) =>
    `px-2.5 py-1 text-xs rounded-md ${
      tab === t
        ? 'bg-primary-600 text-white'
        : 'text-secondary hover:bg-surface-raised'
    }`;

  return (
    <div
      className="mt-2 rounded-lg border border-default bg-surface-alt p-3 space-y-2"
      data-testid="run-facts-help"
    >
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={tabClass('facts')} onClick={() => setTab('facts')}>
          Facts
        </button>
        <button type="button" className={tabClass('examples')} onClick={() => setTab('examples')}>
          Examples
        </button>
        <button type="button" className={tabClass('functions')} onClick={() => setTab('functions')}>
          Operators &amp; functions
        </button>
        {tab === 'facts' && (
          <input
            className="ml-auto w-48 px-2 py-1 text-xs rounded-md border border-default bg-surface text-default"
            placeholder="Filter facts…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            data-testid="run-facts-filter"
          />
        )}
      </div>

      <div className="max-h-72 overflow-y-auto pr-1">
        {tab === 'facts' && (
          <div className="space-y-3">
            <p className="text-xs text-secondary">
              One object, <code>run</code>, describes a finished run. Every
              fact is always present: a missing value reads as{' '}
              <code>&quot;&quot;</code>, <code>0</code>, <code>false</code> or{' '}
              <code>[]</code>. Click a fact to insert it.
            </p>
            {groups.map((g) => (
              <div key={g.title}>
                <p className="text-[11px] font-semibold uppercase tracking-wider text-muted mb-1">
                  {g.title}
                </p>
                <ul className="space-y-1">
                  {g.facts.map((f) => (
                    <li key={f.key} className="text-xs" data-testid="run-fact">
                      <button
                        type="button"
                        className="font-mono text-primary-700 hover:underline"
                        onClick={() => onInsert(`run.${f.key}`)}
                        title="Insert into the expression"
                      >
                        run.{f.key}
                      </button>{' '}
                      <span className="text-muted">{f.type}</span>
                      <span className="text-secondary"> — {f.description}</span>
                      {f.values && (
                        <span className="block text-muted pl-3">
                          values:{' '}
                          {f.values.map((v, i) => (
                            <span key={v}>
                              {i > 0 && ', '}
                              <code>&quot;{v}&quot;</code>
                            </span>
                          ))}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {groups.length === 0 && (
              <p className="text-xs text-secondary">No fact matches “{query}”.</p>
            )}
          </div>
        )}

        {tab === 'examples' && (
          <ul className="space-y-2">
            {CHECK_EXAMPLES.map((ex) => (
              <li key={ex.title} className="text-xs" data-testid="check-example">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-default">{ex.title}</span>
                  <span className="text-muted">→ {ex.output}</span>
                  <button
                    type="button"
                    className="ml-auto px-2 py-0.5 rounded-md border border-default hover:bg-surface-raised"
                    onClick={() => onUseExample(ex)}
                  >
                    Use
                  </button>
                </div>
                <code className="block mt-0.5 font-mono text-secondary break-all">
                  {ex.expression}
                </code>
              </li>
            ))}
          </ul>
        )}

        {tab === 'functions' && (
          <div className="space-y-2 text-xs">
            <p className="text-secondary">
              Comparison <code>== != &lt; &lt;= &gt; &gt;=</code>, logic{' '}
              <code>&amp;&amp; || !</code>, arithmetic <code>+ - * / %</code>,
              the conditional <code>cond ? a : b</code> and lists{' '}
              <code>[&quot;a&quot;, &quot;b&quot;]</code>. Only these functions
              compile; anything else is refused by Compile-check.
            </p>
            <ul className="space-y-1">
              {CHECK_FUNCTIONS.map((fn) => (
                <li key={fn.name}>
                  <span className="font-mono font-medium text-default">{fn.name}</span>
                  <code className="block font-mono text-secondary break-all pl-3">
                    {fn.example}
                  </code>
                </li>
              ))}
            </ul>
            <p className="text-secondary">
              Output types: <code>boolean</code> (true raises the finding),{' '}
              <code>count</code> (an integer), <code>score</code> (a number
              from 0 to 1), <code>classification</code> (a string label).
              Mixing <code>int</code> and <code>double</code> needs a
              conversion, e.g. <code>double(run.error_count)</code>.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
