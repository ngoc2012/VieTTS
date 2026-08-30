# Distributed Compute System — Skeleton (Celery + Redis)

Goal: spread compute across machines. One **main** node (broker + dashboard + optional worker),
N **worker** nodes. Built almost entirely from mature tools — the hard parts (durable queue,
retries, worker health, fair scheduling, dashboard) are already solved by Celery/Redis/Flower.

## Requirement → tool mapping

| Your requirement | Solved by | Custom code needed |
|---|---|---|
| Main balances tasks, can also work | Redis broker on main + a Celery worker process running on main | 0 lines |
| Prefer idle workers | `worker_prefetch_multiplier = 1` + `task_acks_late = True` — idle workers pull next task, busy ones never hoard | 1 line config |
| Main ↔ worker over sockets | Redis protocol (TCP) | 0 lines |
| Worker gets main address at startup | `--broker redis://MAIN_IP:6379/0` CLI arg / env var | 0 lines |
| Main tracks active worker list | Celery heartbeats + `celery inspect ping` | 0 lines |
| Dashboard on main | Flower (`:5555`) — live workers, tasks, retries, runtimes | 0 lines |
| New worker copies DB | **Don't replicate.** Workers stay stateless; single Postgres (or existing SQLite behind the app) on main | 0 lines |
| Broadcast every DB update to all workers | Same — one central DB means nothing to broadcast | 0 lines |
| Worker crash mid-task | `task_acks_late = True` → unacked task re-queued, another worker picks it up | tasks must be idempotent |
| Main crash | Redis AOF persistence → queue survives restart; workers auto-reconnect with backoff (Celery default) | 0 lines |

The only custom code left: task definitions + a submit client + a compose file.

## Architecture

```
                 ┌──────────────── MAIN ────────────────┐
                 │ Redis :6379   (broker + result store) │
  submit ──────▶ │ Flower :5555  (dashboard)             │
                 │ Postgres :5432 (single source of truth)│
                 │ celery worker (main works too)         │
                 └───────▲───────────────▲───────────────┘
                         │ TCP           │ TCP
                  ┌──────┴─────┐   ┌─────┴──────┐
                  │  WORKER 1  │   │  WORKER N  │
                  │ celery     │   │ celery     │
                  │ (stateless)│   │ (stateless)│
                  └────────────┘   └────────────┘
```

## File layout

```
distributed/
├── celery_app.py    # Celery instance + reliability config
├── tasks.py         # task definitions (the actual compute)
├── submit.py        # client: enqueue work, fetch results
├── db.py            # thin DB access (central Postgres/SQLite)
├── docker-compose.yml
└── .env             # MAIN_IP, REDIS_PASSWORD, DB_URL
```

## celery_app.py

```python
import os
from celery import Celery

MAIN = os.environ.get("MAIN_IP", "127.0.0.1")
REDIS = f"redis://:{os.environ['REDIS_PASSWORD']}@{MAIN}:6379/0"

app = Celery("cluster", broker=REDIS, backend=REDIS)

app.conf.update(
    # --- fair scheduling: idle worker always gets the next task ---
    worker_prefetch_multiplier=1,
    # --- reliability: task re-queued if worker dies before finishing ---
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # --- retries ---
    task_default_retry_delay=10,
    task_time_limit=600,          # hard kill runaway task
    task_soft_time_limit=540,
    # --- results ---
    result_expires=3600,
    # --- broker resilience: workers survive main restarts ---
    broker_connection_retry_on_startup=True,
)
```

## tasks.py

```python
from celery_app import app

@app.task(bind=True, max_retries=3, autoretry_for=(Exception,))
def heavy_compute(self, job_id: str, payload: dict) -> dict:
    """Must be idempotent: acks_late means a crash can cause a re-run.
    Use job_id to skip / overwrite instead of duplicating side effects."""
    # ... actual compute (e.g. TTS inference) ...
    # write result to central DB keyed by job_id (upsert, not insert)
    return {"job_id": job_id, "status": "done"}
```

## submit.py

```python
from tasks import heavy_compute

result = heavy_compute.delay("job-123", {"text": "..."})
print(result.get(timeout=600))   # or poll result.id later
```

## docker-compose.yml (main node)

```yaml
services:
  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD} --appendonly yes
    ports: ["6379:6379"]
    volumes: [redis-data:/data]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    ports: ["5432:5432"]
    volumes: [pg-data:/var/lib/postgresql/data]

  flower:
    image: mher/flower:2.0
    command: celery --broker=redis://:${REDIS_PASSWORD}@redis:6379/0 flower
    ports: ["5555:5555"]

  worker-on-main:            # main also works
    build: .
    command: celery -A celery_app worker -l info --concurrency=2
    environment:
      MAIN_IP: redis
      REDIS_PASSWORD: ${REDIS_PASSWORD}

volumes:
  redis-data:
  pg-data:
```

## Startup

```bash
# MAIN
docker compose up -d

# WORKER (any machine) — main address is the startup parameter, as required
MAIN_IP=192.168.1.10 REDIS_PASSWORD=... \
  celery -A celery_app worker -l info --concurrency=4 -n worker1@%h

# Dashboard
open http://192.168.1.10:5555
```

## Failure matrix

| Failure | What happens | Data lost? |
|---|---|---|
| Worker killed mid-task | Task unacked → Redis re-delivers to another worker | No |
| Worker network blip | Celery reconnects with backoff, resumes | No |
| Main (Redis) restart | AOF replays queue; workers reconnect automatically | No |
| Main disk dies | Queue lost unless Redis AOF volume backed up | Queue yes, DB no |
| Duplicate execution (acks_late re-run) | Idempotent task + upsert by job_id → harmless | No |
| Slow/stuck task | `task_time_limit` kills it, retry elsewhere | No |

## Why no DB replication

Original design copied the DB to every worker and broadcast every update — that is
multi-master replication, the hardest problem in the whole system (ordering, missed
updates, sync races). Keeping workers **stateless** and the DB **central on main**
deletes the problem instead of solving it. Workers read/write over the network.

Upgrade path, only if measured need appears:
- Read-heavy workers → Postgres streaming replica per site (built-in, one config file).
- SQLite must stay → [Litestream](https://litestream.io) or LiteFS for replication.
- Main HA → Redis Sentinel + Postgres failover. Not before the single main is actually a bottleneck.

## Prefer-workers-before-main (optional)

Default config already balances by idleness; main's worker competes equally.
To make main strictly last resort: give main's worker `--concurrency=1` and a low
`--prefetch-multiplier`, or route to a second `overflow` queue that only main consumes
and only submit there when `celery inspect active` shows all workers busy. Add when needed.
