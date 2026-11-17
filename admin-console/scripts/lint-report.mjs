/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Reads ESLint's JSON report on stdin and prints the findings per rule,
// most frequent first: `npm run lint:report` (either package).
let body = '';
process.stdin.on('data', (chunk) => (body += chunk)).on('end', () => {
    const counts = {};
    for (const file of JSON.parse(body)) {
        for (const message of file.messages) {
            const rule = message.ruleId || 'fatal';
            counts[rule] = (counts[rule] || 0) + 1;
        }
    }
    for (const [rule, count] of Object.entries(counts).sort((a, b) => b[1] - a[1])) {
        console.log(String(count).padStart(6), rule);
    }
});
