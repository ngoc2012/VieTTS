"""Central job store on main.

ponytail: Redis already runs on main as the broker, so it is also the DB —
no replication, workers stay stateless. Swap for Postgres when data gets
relational; nothing else changes.
"""
import json

import redis

from celery_app import REDIS_URL

_r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def save_job(job_id: str, result: str) -> None:
    # Upsert by job_id: a re-run after a worker crash is harmless.
    _r.hset("jobs", job_id, json.dumps({"status": "done", "result": result}))


def get_job(job_id: str) -> dict | None:
    raw = _r.hget("jobs", job_id)
    return json.loads(raw) if raw else None


def all_jobs() -> dict:
    return {k: json.loads(v) for k, v in _r.hgetall("jobs").items()}
