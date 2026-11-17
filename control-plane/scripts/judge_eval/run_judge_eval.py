# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Measure a semantic check against the labelled set (cases.jsonl).

Uses the production transcript builder and prompt (`judge.format_transcript`,
`judge.build_prompt`) so the numbers describe what the gateway sends. The
model call goes either straight to OpenAI (--openai, key from OPENAI_API_KEY)
or through a Brutor gateway (--gateway URL --key KEY), the way the judge runs.

Spends real tokens: about 2k per case.

    .venv/bin/python scripts/judge_eval/make_cases.py
    .venv/bin/python scripts/judge_eval/run_judge_eval.py --openai --model gpt-5.2 \
        --out /tmp/judge-eval.json
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import env_vars  # noqa: E402

# The judge module imports the ORM, which wants a database URL at import time.
# Nothing here opens a connection.
env_vars.DATABASE_URL.set_default("postgresql://unused:unused@127.0.0.1:1/unused")

import httpx  # noqa: E402

from app.services.assurance_checks.judge import (  # noqa: E402
    build_prompt,
    format_transcript,
    parse_judge_answer,
)

HERE = Path(__file__).parent


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def call(args, prompt):
    body = {"model": args.model, "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 2000}
    if args.openai:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    else:
        url = f"{args.gateway.rstrip('/')}/v1/proxy/llm/chat/completions"
        headers = {"Authorization": f"Bearer {args.key}"}
        body["temperature"] = 0
    for attempt in range(3):
        r = httpx.post(url, json=body, headers=headers, timeout=120)
        if r.status_code < 500 and r.status_code != 429:
            break
        time.sleep(2 * (attempt + 1))
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"], int((data.get("usage") or {}).get("total_tokens") or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-5.2")
    ap.add_argument("--openai", action="store_true")
    ap.add_argument("--gateway")
    ap.add_argument("--key")
    ap.add_argument("--rubric", default=str(HERE / "rubric.txt"))
    ap.add_argument("--cases", default=str(HERE / "cases.jsonl"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.openai and not (args.gateway and args.key):
        ap.error("--openai, or --gateway with --key")

    rubric = Path(args.rubric).read_text()
    cases = [json.loads(line) for line in Path(args.cases).read_text().splitlines() if line.strip()]
    rows, tokens = [], 0
    for c in cases:
        content, coverage = format_transcript([tuple(h) for h in c["hops"]])
        try:
            answer, used = call(args, build_prompt(rubric, content, "boolean", None))
            value, reason = parse_judge_answer(answer, "boolean", None)
        except Exception as exc:  # noqa: BLE001 — a failed case is reported, not fatal
            value, reason, used = None, f"error: {exc}", 0
        tokens += used
        rows.append({"id": c["id"], "kind": c["kind"], "label": c["label"],
                     "flagged": value, "reason": reason, "coverage": coverage})
        print(f"{c['id']:<22} {c['label']:<10} flagged={value}")

    def count(label, flagged):
        return sum(1 for r in rows if r["label"] == label and r["flagged"] is flagged)

    ok_n = sum(1 for r in rows if r["label"] == "ok" and r["flagged"] is not None)
    bad_n = sum(1 for r in rows if r["label"] == "violation" and r["flagged"] is not None)
    fp, tp = count("ok", True), count("violation", True)
    summary = {
        "model": args.model,
        "cases": len(rows),
        "errors": sum(1 for r in rows if r["flagged"] is None),
        "false_alarms": fp, "normal_runs": ok_n,
        "false_alarm_rate": fp / ok_n if ok_n else None,
        "false_alarm_rate_95ci": wilson(fp, ok_n),
        "caught": tp, "violations": bad_n,
        "detection_rate": tp / bad_n if bad_n else None,
        "detection_rate_95ci": wilson(tp, bad_n),
        "by_kind": {},
        "cross_run_flagged": count("cross_run", True),
        "cross_run_cases": sum(1 for r in rows if r["label"] == "cross_run"),
        "tokens": tokens,
    }
    for r in rows:
        k = summary["by_kind"].setdefault(r["kind"], {"n": 0, "flagged": 0})
        k["n"] += 1
        k["flagged"] += 1 if r["flagged"] else 0
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
