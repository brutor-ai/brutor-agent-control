/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Static extraction of the endpoints `GatewayApiClient` calls.
 *
 * Used by the route-contract test to prove every endpoint the admin UI talks
 * to actually exists on the Control Plane. A regex alone can't do this: the
 * generic parameter on `this.request<...>(...)` frequently contains `>` (e.g.
 * `Record<string, any>`, `Array<{ a: number }>`), so the opening paren has to
 * be found by balancing angle brackets rather than by matching a pattern.
 */

export interface ExtractedRoute {
  method: string;
  /** Path with `${...}` interpolations normalised to `{param}`. */
  path: string;
  line: number;
}

export interface ExtractionResult {
  routes: ExtractedRoute[];
  /**
   * Call sites whose first argument isn't a literal (a prebuilt `url`
   * variable, say). Surfaced rather than silently skipped so the test can
   * report how much of the surface it is actually able to check.
   */
  unresolved: number;
}

const CALL = 'this.request';

function skipWs(src: string, i: number): number {
  while (i < src.length && /\s/.test(src[i])) i++;
  return i;
}

/** Skip a balanced `<...>` generic argument list. Returns the index after it. */
function skipGeneric(src: string, i: number): number {
  if (src[i] !== '<') return i;
  let depth = 0;
  while (i < src.length) {
    if (src[i] === '<') depth++;
    else if (src[i] === '>') {
      depth--;
      if (depth === 0) return i + 1;
    }
    i++;
  }
  return i;
}

/**
 * Read a string or template literal starting at `i`. Returns null when the
 * argument isn't a literal. `${expr}` becomes `{param}`; brace depth is
 * tracked so `${a ? {x:1} : {y:2}}` doesn't terminate early.
 */
function readLiteral(src: string, i: number): string | null {
  const quote = src[i];
  if (quote !== "'" && quote !== '"' && quote !== '`') return null;
  i++;
  let out = '';
  while (i < src.length) {
    const c = src[i];
    if (c === '\\') {
      out += src[i + 1] ?? '';
      i += 2;
      continue;
    }
    if (c === quote) return out;
    if (quote === '`' && c === '$' && src[i + 1] === '{') {
      let depth = 1;
      i += 2;
      while (i < src.length && depth > 0) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') depth--;
        i++;
      }
      out += '{param}';
      continue;
    }
    out += c;
    i++;
  }
  return null;
}

/** Find the index just past the `(...)` that starts at `open`. */
function endOfCall(src: string, open: number): number {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {
      depth--;
      if (depth === 0) return i;
    }
  }
  return src.length;
}

export function extractClientRoutes(source: string): ExtractionResult {
  const routes: ExtractedRoute[] = [];
  let unresolved = 0;

  let idx = source.indexOf(CALL);
  while (idx !== -1) {
    let i = skipWs(source, idx + CALL.length);
    i = skipGeneric(source, i);
    i = skipWs(source, i);

    if (source[i] === '(') {
      const open = i;
      const close = endOfCall(source, open);
      const literal = readLiteral(source, skipWs(source, open + 1));

      if (literal === null) {
        unresolved++;
      } else {
        // Drop any query string — the spec keys on path only.
        let path = literal.split('?')[0];
        // Many calls append a prebuilt query string: `` `/v1/admin/x${qs}` ``.
        // That interpolation became `{param}` above, but it is not a path
        // segment. A real path parameter always follows a `/`, so a trailing
        // `{param}` glued onto the previous segment is a query builder.
        path = path.replace(/(?<!\/)\{param\}$/, '');
        // `method: 'POST'` anywhere in this call's arguments; GET is the
        // client's default when the option is omitted.
        const body = source.slice(open, close);
        const m = /method:\s*['"`]([A-Za-z]+)['"`]/.exec(body);
        routes.push({
          method: (m?.[1] ?? 'GET').toUpperCase(),
          path,
          line: source.slice(0, idx).split('\n').length,
        });
      }
    }
    idx = source.indexOf(CALL, idx + CALL.length);
  }

  return { routes, unresolved };
}

/** Collapse every path parameter to `{param}` so both sides compare equal. */
export function normalisePath(path: string): string {
  return path.replace(/\{[^}]*\}/g, '{param}');
}
