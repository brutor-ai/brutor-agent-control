/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// What `console.warn` and `console.error` may print: a sentence (a string or
// a template) and the kind of a failure (`errorKind(value)`,
// `errorStatus(value)`, src/lib/narrow.ts).
//
// A caught value is not printed whole, and neither is its message, its stack
// or a rendering of it: the message of a failed request repeats what the
// other side answered, and that answer can hold a credential or what a user
// or a tool wrote. The user reads the sentence in the page; the browser's
// console gets the kind (`GatewayAPIError 403`).
//
// Two findings:
//   caught — an argument refers to a value bound by `catch (e)`, or by the
//            rejection handler of `.catch(e => …)` / `.then(_, e => …)`,
//            outside `errorKind(…)` / `errorStatus(…)`.
//   value  — an argument is neither a literal, a template, nor one of those
//            two calls: an identifier, an object, a field of something.
//
// The rule reads scopes, not types, so it runs without a TypeScript program.

const KIND_CALLS = new Set(['errorKind', 'errorStatus']);
const WRITERS = new Set(['warn', 'error']);

/** `errorKind(…)` or `errorStatus(…)`. */
function isKindCall(node) {
  return node.type === 'CallExpression' && node.callee.type === 'Identifier' && KIND_CALLS.has(node.callee.name);
}

/** True when `fn` is the rejection handler of `.catch(fn)` or `.then(_, fn)`. */
function isRejectionHandler(fn) {
  const call = fn.parent;
  if (!call || call.type !== 'CallExpression' || call.callee.type !== 'MemberExpression' || call.callee.computed) return false;
  const method = call.callee.property.name;
  return (method === 'catch' && call.arguments[0] === fn) || (method === 'then' && call.arguments[1] === fn);
}

/** True when `variable` holds a caught value. */
function isCaught(variable) {
  return variable.defs.some((def) => {
    if (def.type === 'CatchClause') return true;
    if (def.type !== 'Parameter') return false;
    const fn = def.node;
    return (fn.type === 'ArrowFunctionExpression' || fn.type === 'FunctionExpression') && isRejectionHandler(fn) && fn.params[0] === def.name;
  });
}

/** The variable `identifier` refers to, from the scope it is written in outwards. */
function resolve(sourceCode, identifier) {
  for (let scope = sourceCode.getScope(identifier); scope; scope = scope.upper) {
    const variable = scope.set.get(identifier.name);
    if (variable) return variable;
  }
  return undefined;
}

/** True for an identifier that names a property or a key, not a variable. */
function isName(node) {
  const parent = node.parent;
  if (parent.type === 'MemberExpression') return parent.property === node && !parent.computed;
  // The value of a shorthand property is its own node, and is the variable.
  if (parent.type === 'Property') return parent.key === node && parent.value !== node && !parent.computed;
  return false;
}

/** @type {import('eslint').Rule.RuleModule} */
const failureKind = {
  meta: {
    type: 'problem',
    docs: {
      description: 'console.warn and console.error print a sentence and the kind of a failure, never a caught value or an object',
    },
    schema: [],
    messages: {
      caught:
        "'{{name}}' is a caught value: print its kind with errorKind({{name}}) (src/lib/narrow.ts), never the value, its message or a rendering of it. What the other side answered is shown to the user in the page.",
      value:
        'console.{{writer}} prints a sentence (a string or a template) and the kind of a failure (errorKind(value), errorStatus(value)). Put a scalar in the template; do not print an object or a value whole.',
    },
  },
  create(context) {
    const sourceCode = context.sourceCode;

    /** Every reference to a caught value under `node`, outside the two kind calls. */
    function caughtReferences(node, found = []) {
      if (isKindCall(node)) return found;
      if (node.type === 'Identifier') {
        if (!isName(node)) {
          const variable = resolve(sourceCode, node);
          if (variable && isCaught(variable)) found.push(node);
        }
        return found;
      }
      for (const key of sourceCode.visitorKeys[node.type] ?? []) {
        const child = node[key];
        if (Array.isArray(child)) child.forEach((item) => item && caughtReferences(item, found));
        else if (child && typeof child.type === 'string') caughtReferences(child, found);
      }
      return found;
    }

    return {
      CallExpression(node) {
        const callee = node.callee;
        if (callee.type !== 'MemberExpression' || callee.computed) return;
        if (callee.object.type !== 'Identifier' || callee.object.name !== 'console') return;
        if (!WRITERS.has(callee.property.name)) return;

        for (const argument of node.arguments) {
          const caught = caughtReferences(argument);
          if (caught.length > 0) {
            for (const reference of caught) context.report({ node: reference, messageId: 'caught', data: { name: reference.name } });
            continue;
          }
          if (argument.type === 'Literal' || argument.type === 'TemplateLiteral' || isKindCall(argument)) continue;
          context.report({ node: argument, messageId: 'value', data: { writer: callee.property.name } });
        }
      },
    };
  },
};

export default {
  meta: { name: 'brutor-console' },
  rules: { 'failure-kind': failureKind },
};
