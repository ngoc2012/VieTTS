# Architecture — Distributed Compute Cluster

A deep explanation of how this system works, why each piece exists, and what happens
when things fail. Companion to `README.md` (how to run it) and
`../distributed-skeleton.md` (original design review).

---

## 1. What this system is

A task-distribution cluster: one **main** machine and N **worker** machines spread
CPU-heavy jobs (e.g. TTS inference) across all of them. Any machine can be told at
startup whether it is main or worker. The main node balances work but also executes
work itself.

The core decision: **we did not build the distribution layer — we assembled it.**
Queueing, load balancing, worker discovery, failure recovery, retries, and the
dashboard are all mature features of Celery + Redis + Flower. The only custom code
is the task body, a thin job store, and a submit client (~120 lines total). Every
line we did not write is a line that cannot have a bug in it.

```
                 ┌──────────────────── MAIN ────────────────────┐
                 │                                              │
                 │  Redis :6379                                 │
                 │  ├── task queue        (list "celery")       │
                 │  ├── unacked registry  (in-flight tasks)     │
                 │  ├── result backend    (celery-task-meta-*)  │
                 │  └── job store         (hash "jobs")         │
                 │                                              │
  submit.py ───▶ │  Flower :5555   (dashboard, read-only)       │
                 │  worker-on-main (celery worker, conc=2)      │
                 └───────▲──────────────────────▲───────────────┘
                         │ TCP (Redis protocol) │
                  ┌──────┴─────┐         ┌──────┴─────┐
                  │  WORKER 1  │   ...   │  WORKER N  │
                  │ celery     │         │ celery     │
                  │ stateless  │         │ stateless  │
                  └────────────┘         └────────────┘
```

---

## 2. Components and their exact roles

### Redis (on main) — the heart

Redis plays **four roles at once**, all on the one instance:

| Role | Redis structure | What it does |
|---|---|---|
| **Broker** (task queue) | List `celery` | `submit.py` LPUSHes serialized tasks; workers BRPOP them. FIFO, blocking pop = zero polling latency. |
| **Unacked registry** | Sorted set + hash (`unacked*`) | Every task a worker picked up but has not finished. This is the crash-recovery ledger (see §5). |
| **Result backend** | Keys `celery-task-meta-<id>` | Task return values, so `submit.py` can `result.get()`. TTL 1h (`result_expires`). |
| **Job store** ("the DB") | Hash `jobs` | Application-level results written by `db.py`, keyed by `job_id`. Survives past result expiry. |

Durability: `--appendonly yes` (AOF). Every write is appended to disk, so a Redis
restart replays the log and the queue + job store survive. Without AOF, a main
reboot would silently drop every queued task.

### Celery workers — the muscle

Each worker machine runs one `celery worker` process with a configurable
`--concurrency` (process pool). Workers are **stateless**: they hold no data that
matters after the process exits. Everything durable lives in Redis on main. This
single property is what makes the whole system simple — a worker can be killed,
cloned, or replaced at any moment with zero ceremony.

`MAIN_IP` is the only thing a worker needs to know, passed at startup — satisfying
the original requirement "main's address is a parameter of the worker start command".

### Worker-on-main — main works too

Main runs an ordinary worker process (`worker-on-main` in compose). It connects to
Redis over localhost and competes for tasks like any other worker. There is no
special "fallback" code path: the scheduling mechanism in §4 naturally sends tasks
to whoever is idle, main included. Its `--concurrency=2` is deliberately small so
main keeps CPU headroom for Redis and Flower.

### Flower — the dashboard

Flower subscribes to Celery's event stream (workers emit events on task start,
success, failure, heartbeat) and renders it at `:5555`: live worker list, tasks
in flight, per-task runtimes, retries, failures. It is **read-only observation**
— killing Flower affects nothing but visibility. This satisfies "a simple
dashboard on main to manage active workers" with zero custom code.

---

## 3. Life of a task

What actually happens when you run `python submit.py 1`:

```
 1. submit.py calls heavy_compute.delay(job_id, payload)
 2. Celery serializes {task, id, args} to JSON, LPUSH onto Redis list "celery"
 3. Every idle worker process is blocked on BRPOP of that list.
    Exactly one worker wins the pop — this is the load balancer.
    There is no scheduler process; Redis's atomic pop IS the scheduling.
 4. The winning worker moves the message into the unacked registry
    (because task_acks_late=True, the ack is deferred).
 5. Worker executes heavy_compute():
       do_work(payload)          ← the actual compute
       save_job(job_id, result)  ← upsert into hash "jobs"
 6. Worker writes the return value to celery-task-meta-<id> (result backend).
 7. Worker ACKs: the message is deleted from the unacked registry.
    Only now is the task "done" from the queue's perspective.
 8. submit.py's result.get() polls the result backend, returns the value.
```

Steps 5–7 ordering matters: the job store write (5) happens **before** the ack (7).
So a crash between them re-runs a task whose output already exists — which is why
the job store uses upsert-by-`job_id` (§6).

---

## 4. Scheduling: why idle workers are always preferred

The original design wanted "if an idle worker exists, prefer it". Two config lines
make this emergent rather than coded:

### `worker_prefetch_multiplier = 1`

By default Celery workers prefetch `4 × concurrency` messages to reduce round-trips.
Prefetching is fatal for long tasks: a busy worker can hold 8 queued tasks hostage
in its local buffer while another worker sits idle. Setting the multiplier to 1
means **a worker only ever holds tasks it is actively executing**. The next queued
task stays in Redis until some worker — any worker — has a free slot and pops it.

### `task_acks_late = True`

Besides its reliability role (§5), late acking interacts with prefetch: a task is
not "claimed" until a process is genuinely free to run it.

**Result:** the fastest / least-loaded machine naturally pops the most tasks.
A 16-core worker drains tasks 8× faster than main's 2-slot worker, without any
weight configuration. Perfect least-busy balancing, implemented by a blocking pop.

The trade-off: one Redis round-trip per task (~1 ms on LAN). Irrelevant when tasks
run for seconds. If tasks were sub-millisecond, this would be the wrong design —
but then you would not need a distributed cluster.

---

## 5. Failure analysis

The mechanisms above were chosen specifically for how they fail. Walkthrough of
every failure mode:

### Worker dies mid-task (OOM, power loss, `kill -9`)

The task message is still in the **unacked registry** (never acked). Redis notices
the dead connection; after the visibility timeout the message is restored to the
queue and another worker pops it. `task_reject_on_worker_lost=True` makes this
immediate when the broker sees the connection drop.

Consequence: **at-least-once delivery**. A task can run twice (worker finished the
work, crashed before acking). This is a deliberate trade — the alternative
(ack-early, at-most-once) silently loses tasks on crashes. Losing work is worse
than repeating idempotent work. Hence the idempotency contract in §6.

### Worker has a network blip

Celery's broker connection retries with exponential backoff
(`broker_connection_retry_on_startup=True` covers the startup case too). The worker
rejoins automatically; any task it held unacked gets redelivered meanwhile. No
operator action, no state to clean up — because workers are stateless.

### Task hangs or runs away

`task_soft_time_limit=540` raises an exception inside the task (a chance to clean
up); `task_time_limit=600` SIGKILLs the process. The pool replaces the process,
the unacked message is redelivered elsewhere. Combined with `max_retries=3` +
`retry_backoff`, a poisoned task fails permanently after 3 attempts instead of
looping forever.

### Main (Redis) restarts

AOF replays: queue, unacked registry, and job store come back. Workers reconnect
with backoff and resume. Tasks in flight during the outage complete on the worker
and are acked/written after reconnect.

### Main's disk dies

The one genuine data-loss scenario: queued-but-unfinished tasks and the job store
are gone. Mitigation is ordinary backup of the Redis volume. This is the accepted
cost of having a single main — see §8 for the upgrade path.

### Split brain / conflicting writes

Cannot happen. There is exactly one queue and one job store, both in one Redis.
Nobody replicates anything, so nothing can diverge. This is not an accident — it
is the entire point of §7.

---

## 6. The idempotency contract

At-least-once delivery pushes one obligation onto task authors, and it is the only
contract in the system:

> **A task re-run with the same `job_id` and payload must be harmless.**

Concretely in this codebase:

- `do_work()` is deterministic: same payload → same output.
- `save_job()` is an upsert (`HSET` by `job_id`): the second run overwrites the
  first with identical data.
- Side effects must follow the same rule. For TTS: write the wav to a path derived
  from `job_id` (overwrite-same-content is harmless). Never "append to a list" or
  "charge a credit" inside a task without checking `job_id` first.

If a future task genuinely cannot be idempotent, guard it with `SETNX job:<id>:started`
— but reach for idempotent design first; it is simpler and has no race window.

---

## 7. Why there is no database replication

The original design: every new worker copies the DB from another idle worker,
and every DB update is broadcast to all active workers. That is **multi-master
replication**, and it fails in ways that are hard to even detect:

1. **Snapshot race** — while a new worker copies the DB (seconds to minutes),
   updates keep flowing to other workers. The copy is stale the moment it
   finishes, and nothing knows by how much.
2. **Missed broadcast** — a worker offline for 2 seconds misses an update and
   diverges *permanently and silently*. There is no sequence number, so no way
   to detect the gap, let alone repair it.
3. **No ordering** — two updates broadcast concurrently can arrive at different
   workers in different orders. Replicas diverge even with zero packet loss.
4. **Write anarchy** — if any worker can originate an update, two workers writing
   the same row concurrently have no conflict resolution at all.

Fixing all four requires a single-writer sequence log (WAL), gap detection,
snapshot-plus-delta catch-up, and ack tracking — roughly a small database engine.

Instead this architecture **deletes the problem**: workers are stateless and the
job store is central. A "new worker joining" needs to copy nothing — it connects
and pops a task. A "DB update" is one `HSET` to one Redis — there is nothing to
broadcast because there are no replicas. Every one of the four failure modes above
becomes structurally impossible.

Cost: every job-store access is a network round-trip (~0.5 ms LAN) instead of a
local read, and main is a single point of failure for data. For compute-bound
tasks lasting seconds, the round-trip is noise; the SPOF is addressed below.

---

## 8. Trade-offs accepted, and upgrade paths

Each simplification is deliberate, with a known ceiling and a known next step.
Do not take an upgrade path until its trigger is actually measured.

| Simplification | Ceiling | Upgrade when triggered |
|---|---|---|
| Single main, no HA | Main down ⇒ no new tasks (running tasks finish; workers reconnect when main returns) | Redis Sentinel (automatic failover) — when downtime cost exceeds the ops cost of 3 Redis nodes |
| Redis hash as job store | No relational queries, no transactions across keys | Postgres on main; only `db.py` changes (3 functions) |
| One queue for all tasks | No priorities; a flood of cheap tasks delays an urgent one | Celery task routing: `task_routes` + per-queue workers |
| JSON round-trip per result | Large payloads (e.g. raw wav bytes) bloat Redis | Store artifacts on shared storage/object store; pass paths through the queue |
| Plain TCP + password auth | Fine on trusted LAN; not on public internet | TLS (`rediss://`) or WireGuard between sites |
| Strictly-equal main worker | Main competes for tasks even when workers are idle-ish | Second `overflow` queue only main consumes; submit there only when all workers busy |

---

## 9. Security model

Current stance: **trusted LAN**.

- Redis requires a password (`--requirepass`); an unauthenticated peer cannot
  enqueue tasks or read the job store. Change `changeme` before any real use.
- Anyone holding the password can submit tasks, i.e. **execute the registered task
  functions** on every worker. The password is therefore a cluster-admin credential
  — treat it like one. (Workers only run functions defined in `tasks.py`; the
  content serializer is JSON, so no pickle code-execution vector.)
- Flower binds to `:5555` with no auth: read-only, but it leaks task names and
  args. Front it with basic auth or keep the port firewalled off untrusted networks.
- Nothing here is internet-safe as-is. Exposing 6379 publicly is the classic way
  Redis instances get owned. Cross-site → WireGuard or `rediss://` with TLS certs.

---

## 10. Mapping back to the original requirements

| Original requirement | Where it lives now |
|---|---|
| Choose main or worker per machine | `docker compose up` (main) vs `celery worker` + `MAIN_IP` (worker) |
| Main balances, also works | Redis atomic pop (§4) + `worker-on-main` service |
| Prefer idle workers | `worker_prefetch_multiplier=1` (§4) |
| Main ↔ worker via sockets | Redis protocol over TCP |
| Main address as startup parameter | `MAIN_IP` env var |
| Main registers connecting workers | Celery heartbeats; visible in Flower and `celery inspect ping` |
| Dashboard for active workers | Flower :5555 |
| New worker copies DB | Eliminated — workers stateless (§7) |
| Broadcast every DB update | Eliminated — single central store (§7) |

The two "eliminated" rows are the most important ones: the requirements were
symptoms of putting state on workers. Remove the state, and the requirements —
along with their hardest failure modes — disappear.
