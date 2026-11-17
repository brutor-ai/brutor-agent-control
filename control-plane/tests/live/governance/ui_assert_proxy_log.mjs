/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * UI assertion helper for the live governance suite.
 *
 * Opens the Admin Console -> Logs & Audit -> Proxy Logs, expands the most-recent
 * row (the call the test just made), and prints — as one JSON line on stdout —
 * the rendered detail so a pytest test can assert the CONSOLE shows the same
 * governed outcome the API asserted (status, compliance tags, correlation id,
 * error text). Reuses the Playwright admin harness from brutor-video-gen.
 *
 *   node ui_assert_proxy_log.mjs
 *
 * Env:
 *   BRUTOR_VIDEOGEN   path to brutor-video-gen (default: repo sibling)
 *   BRUTOR_HEADFUL=1  run headed (debugging)
 * Output (stdout, last line): {"ok":true,"correlation_ids":[...],"text":"..."}
 * Any failure -> non-zero exit + {"ok":false,"error":"..."} on stdout.
 */

import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const VIDEOGEN = process.env.BRUTOR_VIDEOGEN
  || path.resolve(new URL('.', import.meta.url).pathname, '../../../../brutor-video-gen');

async function main() {
  if (!existsSync(VIDEOGEN)) throw new Error(`brutor-video-gen not found at ${VIDEOGEN} (set BRUTOR_VIDEOGEN)`);
  const pwMod = await import(path.join(VIDEOGEN, 'node_modules/playwright/index.js'));
  const pw = pwMod.default ?? pwMod;                 // CJS default-export interop
  const yaml = await import(path.join(VIDEOGEN, 'node_modules/js-yaml/dist/js-yaml.mjs'));
  const admin = await import(path.join(VIDEOGEN, 'src/scenes/admin.mjs'));
  const cfg = yaml.load(readFileSync(path.join(VIDEOGEN, 'config.yaml'), 'utf8'));

  const browser = await pw.chromium.launch({ headless: !process.env.BRUTOR_HEADFUL });
  const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
  try {
    await admin.adminLogin(page, cfg);
    await admin.adminGoto(page, cfg, { nav: ['Logs & Audit', 'Proxy Logs'], waitText: 'Proxy Logs' });
    await page.waitForTimeout(1500);
    await admin.adminClickRow(page, { nth: 0 });      // expand the newest row
    await page.waitForTimeout(1500);

    // Grab the rendered detail. The expanded row / detail panel carries the
    // correlation id, status, target URL, compliance chips and error — we
    // return the page text and the correlation ids present so pytest can assert
    // the specific governed row it just created is on screen with the right
    // status + tag.
    const result = await page.evaluate(() => {
      const text = document.body.innerText || '';
      const corrs = Array.from(
        text.matchAll(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g),
      ).map((m) => m[0]);
      return { text, correlation_ids: Array.from(new Set(corrs)) };
    });
    process.stdout.write(JSON.stringify({ ok: true, ...result }) + '\n');
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }) + '\n');
  process.exit(1);
});
