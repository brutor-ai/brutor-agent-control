/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Flat config (ESLint 10).
//
// Linting is TYPE-AWARE: the parser is handed a real TypeScript program, so
// rules can ask the type checker questions instead of pattern-matching syntax.
// That is what makes `no-floating-promises`, `no-misused-promises` and the
// `no-unsafe-*` family possible at all — none of them can run without types.
//
// NOTE ON `--max-warnings` in package.json: that number is a ratchet, not a
// target. Errors are at zero and must stay there. The remaining warnings are
// known debt; the cap is pinned at their count so the build is green but one
// more warning fails it. When you clear warnings, lower the number to
// match — never raise it.
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import vitest from '@vitest/eslint-plugin';
import importX from 'eslint-plugin-import-x';
import { createTypeScriptImportResolver } from 'eslint-import-resolver-typescript';
import prettier from 'eslint-config-prettier';
import brutorStyle from './src/ui-style/eslint/index.js';
import brutorConsole from './scripts/eslint/console-failure-kind.js';

/** The name of the block that holds what `console.warn` and `console.error` print (see the block). */
export const CONSOLE_FAILURE_KIND = 'console/failure-kind';

/** The name of the one block the Enterprise package does not take (see the block). */
export const COMMUNITY_ONLY_BOUNDARY = 'community/no-enterprise-imports';

/**
 * A module path that leads into the Enterprise tree: a relative path that
 * climbs to an `enterprise` directory, or the `@enterprise` alias.
 */
const ENTERPRISE_SPECIFIER = /^((\.\.\/)+(.*\/)?enterprise(\/|$)|@enterprise(\/|$))/;
/** Declared by enterprise/package.json and not by this package. */
const ENTERPRISE_ONLY_PACKAGES = ['@monaco-editor/react', 'monaco-editor', 'react-markdown', 'remark-gfm', 'javascript-obfuscator'];
const BOUNDARY_MESSAGE =
  'src/ is the Community Edition and must stand without enterprise/ beside it: nothing under src/ may load from enterprise/. Contribute it through the extension seam (src/app/extensions.ts) instead.';

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'coverage/**',
      'public/**',
      'images/**',
      'node_modules/**',
      // The Enterprise tree is linted like `src/`: only its
      // build output is ignored, as the root's is.
      // The Enterprise package beside this one, where there is one, lints
      // itself with these same rules (enterprise/eslint.config.js).
      'enterprise/**',
      'src/lib/generated/**', // openapi-typescript output; not ours to lint
      'src/ui-style/**',      // vendored from brutor-ui-style; linted in that repo
      'docs/**',
    ],
  },

  js.configs.recommended,
  tseslint.configs.recommendedTypeChecked,

  {
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2024 },
      parserOptions: {
        project: ['./tsconfig.eslint.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    linterOptions: { reportUnusedDisableDirectives: 'error' },
  },

  // v7 keeps the flat presets under `configs.flat`; `configs.recommended` is
  // the eslintrc shape and throws under flat config. `recommended`
  // here is the 16-rule set — the classic two plus the React Compiler
  // checks (set-state-in-effect, purity, immutability, refs).
  reactHooks.configs.flat.recommended,

  {
    plugins: { 'react-refresh': reactRefresh },
    rules: {
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // The `_name` prefix is this codebase's "deliberately unused" marker, and
      // `ignoreRestSiblings` covers the `const { drop, ...rest } = obj` idiom
      // used to strip keys off form state before a request.
      '@typescript-eslint/no-unused-vars': [
        'error',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],
      '@typescript-eslint/no-explicit-any': 'warn',
      // `while (true)` stream pumps are idiomatic; ESLint 9 made this the
      // default. Constant conditions in if/ternary are still errors.
      'no-constant-condition': ['error', { checkLoops: false }],
      'prefer-const': 'error',
      'no-var': 'error',
    },
  },

  // Tests: the type-aware `no-unsafe-*` family fires constantly on mocks and
  // fixtures, where `any` is the point rather than an oversight.
  {
    files: ['**/*.{test,spec}.{ts,tsx}', 'src/test/**/*.{ts,tsx}'],
    ...vitest.configs.recommended,
  },

  // Plain JS (build config, perf scripts): no types to check against.
  {
    files: ['**/*.{js,mjs,cjs}'],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: { globals: { ...globals.node } },
  },



  // ==========================================================================
  // Type-aware debt, ratcheted.
  //
  // `recommendedTypeChecked` sets every rule below to ERROR. The ones listed
  // here have violations, so they are downgraded to WARN and counted by the
  // `--max-warnings` cap: the debt is visible and frozen, and one more
  // violation fails the build. Every type-aware rule NOT listed here is
  // clean and stays a hard error.
  //
  // Burn one down, delete its line, and it becomes a gate. The counts are
  // a snapshot; the top of the list is where the real bugs are, not the bottom.
  // ==========================================================================
  {
    files: ['**/*.{ts,tsx}'],
    rules: {
      // --- Genuine bug classes. Fix these first. -----------------------------
      '@typescript-eslint/no-base-to-string': 'warn',          //  49 "[object Object]"
      '@typescript-eslint/require-await': 'warn',              //  41
      '@typescript-eslint/restrict-template-expressions': 'warn', // 7
      '@typescript-eslint/await-thenable': 'warn',             //   5 awaiting a non-Promise
      '@typescript-eslint/no-unused-expressions': 'warn',      //   2
      '@typescript-eslint/unbound-method': 'warn',             //   1 lost `this`
      '@typescript-eslint/restrict-plus-operands': 'warn',     //   1
      'no-useless-assignment': 'warn',                         //   1 dead store
      '@typescript-eslint/no-unused-vars': [                   //  18 (mostly in tests)
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],

      // --- `any` bleeding through untyped API responses. Bulk debt. ---------
      '@typescript-eslint/no-unsafe-member-access': 'warn',    // 1693
      '@typescript-eslint/no-unsafe-assignment': 'warn',       //  797
      '@typescript-eslint/no-unsafe-argument': 'warn',         //  556
      '@typescript-eslint/no-unsafe-call': 'warn',             //  138
      '@typescript-eslint/no-unsafe-return': 'warn',           //   69
      // CAUTION — this rule's autofix is not safe to apply blind. It removes
      // `as T` that it judges redundant *at the assertion site*, but an
      // assertion inside an object literal often WIDENS a literal into a
      // union ('s3' as 's3' | 'webhook') and so determines the inferred type
      // of the enclosing object. Removing it type-checks locally and breaks
      // the consumer. Running `eslint . --fix` across this repo has broken
      // files exactly this way. Review each fix; never bulk-apply.
      '@typescript-eslint/no-unnecessary-type-assertion': 'warn', // 216
      '@typescript-eslint/no-redundant-type-constituents': 'warn', //  11
    },
  },

  // Test-suite findings, same ratchet.
  {
    files: ['**/*.{test,spec}.{ts,tsx}', 'src/test/**/*.{ts,tsx}', 'enterprise/src/test/**/*.{ts,tsx}'],
    rules: {
      'vitest/valid-expect': 'warn',           // 13 — assertion message silently dropped
      'vitest/no-conditional-expect': 'warn',  //  3
      'vitest/valid-title': 'warn',            //  1
    },
  },


  // Import hygiene. `no-cycle` is the one that matters: a circular import
  // resolves to a partially-initialised module, which fails only at runtime and
  // usually only in the production bundle where module order differs.
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'import-x': importX },
    settings: {
      'import-x/resolver-next': [
        createTypeScriptImportResolver({ project: './tsconfig.eslint.json' }),
      ],
    },
    rules: {
      'import-x/no-cycle': ['error', { maxDepth: 8, ignoreExternal: true }],
      'import-x/no-self-import': 'error',
      'import-x/no-duplicates': 'error',
      'import-x/no-empty-named-blocks': 'error',
    },
  },


  // React Compiler rules (eslint-plugin-react-hooks v7). The findings below
  // are ratcheted like the rest. These are not
  // style: `set-state-in-effect` is the double-render / render-loop smell, and
  // `immutability` / `purity` catch state mutated where React assumes it is not.
  {
    files: ['**/*.{ts,tsx}'],
    rules: {
      'react-hooks/set-state-in-effect': 'warn',         // 200
      'react-hooks/static-components': 'warn',           //  44
      'react-hooks/preserve-manual-memoization': 'warn', //  42
      'react-hooks/immutability': 'warn',                //  36
      'react-hooks/purity': 'warn',                      //  11
      'react-hooks/refs': 'warn',                        //   9
      'react-hooks/use-memo': 'warn',                    //   1
    },
  },

  // Shared style-system rules (brutor-ui-style).
  //
  // `prefer-layout-primitives` is an ERROR and must stay at zero: it catches
  // the construction that only lays out correctly under global rules such as
  // `.h-full.flex { overflow: hidden }` or `.flex-1 { min-height: 0 }`, whose
  // failure mode is content silently clipped with no scrollbar — invisible to
  // tsc, to the rest of ESLint and to jsdom.
  //
  // `no-hardcoded-color` is ratcheted like the other debt: a literal colour
  // cannot follow the theme or the tenant's brand.
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'brutor-style': brutorStyle },
    rules: {
      'brutor-style/prefer-layout-primitives': 'error',
      'brutor-style/no-hardcoded-color': 'warn',
    },
  },

  // The browser's console. Production source writes a diagnostic through
  // `debugLog` (src/lib/debugLog.ts), which is silent unless
  // BRUTOR_DEBUG_ENABLED is on and takes a sentence and scalars only, so no
  // payload, response or credential is written by a line left behind.
  // `console.warn` and `console.error` stay for failures. Tests may write;
  // scripts are not under src/.
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['**/*.{test,spec}.{ts,tsx}', '**/__tests__/**', 'src/test/**'],
    rules: {
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
  // The helper itself: the one `console.log` of the console.
  {
    files: ['src/lib/debugLog.ts'],
    rules: {
      'no-console': ['error', { allow: ['log'] }],
    },
  },

  // What a failure prints. `console.warn` and `console.error` take a sentence
  // and the kind of the failure (`errorKind`, `errorStatus`,
  // src/lib/narrow.ts): the class of the error and its numeric status. A
  // caught value is not printed whole, nor its message, nor an object: the
  // message of a failed request repeats what the other side answered. The
  // rule is scripts/eslint/console-failure-kind.js;
  // `src/lib/__tests__/consoleFailureKind.lint.test.ts` runs it on text.
  {
    name: CONSOLE_FAILURE_KIND,
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['**/*.{test,spec}.{ts,tsx}', '**/__tests__/**', 'src/test/**'],
    plugins: { 'brutor-console': brutorConsole },
    rules: {
      'brutor-console/failure-kind': 'error',
    },
  },

  // The edition boundary: the Community tree stands
  // without the Enterprise tree beside it, so nothing under src/ may load
  // anything from enterprise/ — not by an import or a re-export, a dynamic
  // import, a `require`, an `import.meta.glob` pattern or a `vi.mock` target.
  // There is no allow-list. `src/app/__tests__/editionBoundary.test.ts` holds
  // the same line by reading every string of every file, which also catches
  // a path built in a way no selector here names.
  //
  // This block is the Community package's alone: the Enterprise package
  // takes every other block of this file and drops this one by its name.
  {
    name: COMMUNITY_ONLY_BOUNDARY,
    // The tree, and this package's own configuration files beside it.
    files: ['src/**/*.{ts,tsx,js,jsx,mjs,cjs}', '*.config.{ts,js,mjs,cjs}'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: ENTERPRISE_SPECIFIER.source,
              message: BOUNDARY_MESSAGE,
            },
            {
              // One install serves both packages, so these resolve here
              // although this package does not declare them: they are the
              // Enterprise package's, and are not installed without it.
              regex: `^(${ENTERPRISE_ONLY_PACKAGES.join('|')})(/|$)`,
              message:
                'This package is a dependency of the Enterprise package only (enterprise/package.json): it is not installed for the Community console, and what it is used for is an Enterprise feature.',
            },
          ],
        },
      ],
      'no-restricted-syntax': [
        'error',
        ...[
          'ImportExpression > Literal',
          'ImportExpression > TemplateLiteral > TemplateElement',
          "CallExpression[callee.name='require'] > Literal",
          "CallExpression[callee.name='require'] > TemplateLiteral > TemplateElement",
          "CallExpression[callee.object.type='MetaProperty'] Literal",
          "CallExpression[callee.object.name='vi'] > Literal",
        ].map((where) => ({
          selector: `${where}[${where.endsWith('TemplateElement') ? 'value.raw' : 'value'}=${ENTERPRISE_SPECIFIER}]`,
          message: BOUNDARY_MESSAGE,
        })),
      ],
    },
  },

  // Must stay last: turns off every rule Prettier already decides.
  prettier,
);
