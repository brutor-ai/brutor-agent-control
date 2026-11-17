# Getting started: the Skill Runner from source

This guide runs the Skill Runner on your machine, sends it one script by
hand, and then puts it behind a Core Gateway that runs from source. For what
skills are and how an agent uses them, read
<https://docs.brutor.ai/agent-skills/>; for the sandbox as an operator sees
it, <https://docs.brutor.ai/agent-skills/sandbox/>.

Run from source like this for development only. The isolation the product
relies on comes from how the packages run the container (read-only file
system, no capabilities, an internal network): see the README and
`THREAT-MODEL.md`. Outside Linux the runner also warns at start that its own
environment is readable by the scripts it runs.

## 1. What you need

- Rust (the `Dockerfile` builds with 1.88).
- The interpreters for the scripts you will run, on `PATH`: `python3`, `bash`,
  `node`, and `tsx` for TypeScript.
- `openssl` and `curl` for the steps below.
- Nothing else for steps 2 and 3: the runner needs no database, no Redis and
  no other service.

## 2. Start it

```bash
export BRUTOR_SKILL_RUNNER_JWT_SECRET="$(openssl rand -hex 32)"
cargo run
```

It listens on `0.0.0.0:8210` (`BRUTOR_SKILL_RUNNER_LISTEN_ADDR` changes that) and
refuses to start if the secret is missing or shorter than 32 characters.

```bash
curl -s http://localhost:8210/health
# {"service":"brutor-skill-runner","status":"ok","version":"…"}
```

If port 8210 is taken, something else is already running a runner: the local
development infrastructure in `brutor-gateway-control/docker-compose.yml`
publishes its `skill-runner` container on that port.

## 3. First run, by hand

The runner accepts a request only with a token signed with the shared secret
whose `exec` claim equals the request's `execution_id`. In the same shell (it
needs `BRUTOR_SKILL_RUNNER_JWT_SECRET`):

```bash
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
EXEC_ID="exec-$(date +%s)"
HEADER=$(printf '{"alg":"HS256","typ":"JWT"}' | b64url)
CLAIMS=$(printf '{"exec":"%s","tnt":"default","skl":"hello","exp":%s}' "$EXEC_ID" "$(( $(date +%s) + 300 ))" | b64url)
SIGNATURE=$(printf '%s.%s' "$HEADER" "$CLAIMS" | openssl dgst -sha256 -hmac "$BRUTOR_SKILL_RUNNER_JWT_SECRET" -binary | b64url)

curl -s http://localhost:8210/v1/runner/run_script \
  -H "Authorization: Bearer $HEADER.$CLAIMS.$SIGNATURE" \
  -H 'Content-Type: application/json' \
  -d '{
    "skill_id": "hello", "skill_name": "hello", "skill_version": "1.0.0",
    "execution_id": "'"$EXEC_ID"'", "tenant_id": "default", "session_id": "first-run",
    "script": {"filename": "hello.py", "language": "PYTHON", "execution_mode": "SANDBOX",
               "script_content": "print(\"hello from the skill runner\")"}
  }'
```

The answer:

```json
{"success":true,"output":"hello from the skill runner","stderr":"","exit_code":0,"duration_ms":85.7,"workspace_files":[]}
```

A wrong secret, an expired token or an `execution_id` that differs from the
token's `exec` claim gets `401` with `{"error":"unauthorized", …}`.
`language` is one of `PYTHON`, `JAVASCRIPT`, `TYPESCRIPT`, `BASH`;
`execution_mode` is `SANDBOX`, `TRUSTED` or `DISABLED`. `curl -s
http://localhost:8210/metrics` now shows the run.

## 4. Behind a Core Gateway from source

In normal use only the Core Gateway calls the runner. Give the gateway the
runner's address and the same secret, in its environment:

```bash
BRUTOR_SKILL_RUNNER_URL=http://localhost:8210
BRUTOR_SKILL_RUNNER_JWT_SECRET=<the value the runner was started with>
```

The gateway's default for `BRUTOR_SKILL_RUNNER_URL` is `http://skill-runner:8210`,
the address inside the packages' Docker network, so from source it has to be
set. Starting the gateway and the rest of the stack is in
`brutor-gateway-core/README.md`. Then create a skill and call it as
<https://docs.brutor.ai/build-an-agent/skill/> describes; the script runs
here, and the runner logs each request.

## 5. Check your change

```bash
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets
```
