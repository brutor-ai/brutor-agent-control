# Skill runner — threat model

The runner executes skill scripts (Python, JavaScript, TypeScript, Bash)
that tenants write. Treat every script as hostile. This document is the
attack-surface walk that `src/lib.rs` and `src/executor.rs` refer to.
The runner's isolation must not stop at the container: the network and
the cloud identity around it are part of the boundary. The controls
below marked in bold are the ones that cover that; what remains open is
under "Residual risks".

## Adversary

The author of a tenant skill, or anyone who can get a skill script
executed (a tenant admin, or a prompt that steers an agent into running a
TRUSTED skill with chosen arguments). They control the script's code and
its inputs. They do not control the runner binary, the image or the
deployment.

## Assets

| Asset | Where it lives | What an attacker does with it |
|---|---|---|
| Quota, budget and admission counters | Redis | Reset them: every usage limit and the run caps stop holding |
| Platform data | Postgres, object storage (media), the vector store (knowledge bases) | Read or alter other tenants' data |
| The Core Gateway's internal routes | `core:8100` | Call internal APIs from inside the network |
| Cloud credentials | The identity the runner's workload runs under (an instance, task or pod role), via the metadata endpoint | Whatever that role allows |
| `BRUTOR_SKILL_RUNNER_JWT_SECRET` | The runner's environment | Forge execute tokens and run code as any tenant |
| Other executions | Same container: `/tmp`, process table | Read another run's scratch files or interfere with it |

## Trust boundaries

1. **Core → runner.** Core signs an HS256 token per call, bound to one
   `execution_id` and valid for minutes. The runner only verifies; it
   never signs. Only core can reach the runner (network, below).
2. **Runner → script.** The script is untrusted. The boundary is the
   subprocess plus the container plus the network around the container.
   The runner's environment-clearing and timeouts are resource controls,
   not a security boundary on their own (see `executor.rs`, "What SANDBOX
   does and does NOT guarantee").

## Controls

| Control | Where | Stops |
|---|---|---|
| Runner holds no platform secrets: no DB, Redis, encryption or provider keys | design (`lib.rs`) | Nothing to steal in-process beyond the runner's own key |
| Subprocess environment cleared, fixed allowlist | `executor.rs` `build_subprocess_env` | Secrets in env reaching the script directly |
| **Runner process non-dumpable** (`prctl(PR_SET_DUMPABLE, 0)` at startup) | `main.rs` `deny_same_uid_inspection` | A same-UID script reading `/proc/1/environ` (the JWT secret; any cloud credential reference the platform puts there) or ptracing the runner. Startup fails if the call fails. Linux only. |
| Own process group, killed on timeout | `executor.rs` | Runaway and forked processes |
| `tsx` run directly, never `npx` | `executor.rs` | npx fetching packages from a registry |
| Read-only root FS, 64 MiB `/tmp` tmpfs, all capabilities dropped, no-new-privileges, default seccomp, non-root UID 10001, pids 64, 256 MiB, 1 CPU | Dockerfile; the deployment (Compose service or the equivalent container settings) | Persistence, privilege escalation, fork bombs, resource exhaustion |
| **Network: only core can reach the runner, and the runner can reach only core** | the deployment. Docker Compose: a network of its own with `internal: true` whose only members are core and the runner. Elsewhere: a security group or network policy for the runner alone, inbound on the runner's port from core only | Reaching Redis, Postgres, object storage, the vector store, model servers, the host and the internet |
| **A cloud identity of its own with no permissions, and no remote exec into the container** | the deployment (cloud only) | Cloud credentials from the metadata endpoint being worth anything |

## Egress policy

None by default. No shipped skill needs the network: a skill gets its
inputs inline, and anything governed (LLM, MCP, knowledge bases) goes
through core, not from the script.

A deployment whose own skills must call public APIs gives the runner a
**separate** egress network (Compose: an extra non-internal bridge
network that only the runner joins, never the network the platform's
services are on). It must not put the runner back on the platform
network.

## Residual risks

- **Egress the hosting service itself needs.** Some managed container
  services pull the image, ship logs and read secrets through the
  container's own network interface. The runner's network rule then has
  to allow that traffic, and a script can reach whatever the rule allows
  (without cloud credentials, given the control above). Narrow the rule
  to private endpoints for exactly those services.
- **Shared kernel.** Container isolation is not a VM boundary; a kernel
  exploit escapes it. gVisor or Firecracker would close this.
- **Executions share a container.** Concurrent runs, including from
  different tenants, share the network namespace and the `/tmp` tmpfs
  (separate per-run directories, same UID). A per-run UID or a per-run
  sandbox (nsjail) would separate them.
- **Limits the hosting service does not offer.** Some managed container
  services have no pids limit or custom seccomp profile; the limits in
  the Controls table hold only where the runtime applies them.
- **A platform service without authentication** (a Redis without a
  password, for example) is protected from the runner by the network
  alone. Give each one credentials of its own.

## Proving it

- Compose: `docker compose config --format json` shows the runner's
  networks are exactly `[skill-runner-net]`, that network is
  `internal: true`, and its only other member is `core`.
- Live: a BASH skill that tries `redis:6379`, `postgres:5432`,
  `minio:9000` and `1.1.1.1:443` fails on every one; a normal skill still
  returns output through core.
- Runner: a BASH skill running `cat /proc/1/environ` gets
  "Permission denied" in the container.
