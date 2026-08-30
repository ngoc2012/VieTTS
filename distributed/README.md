# Distributed cluster (Celery + Redis + Flower)

See `../distributed-skeleton.md` for the design rationale.

## Main node

```bash
cp .env.example .env          # set a real REDIS_PASSWORD
docker compose up -d          # redis (broker+db) + flower dashboard + worker-on-main
```

Dashboard: http://MAIN_IP:5555 — live workers, queued/running tasks, retries.

## Worker node (any machine)

```bash
pip install -r requirements.txt   # or: uv pip install -r requirements.txt
MAIN_IP=192.168.1.10 REDIS_PASSWORD=... \
  celery -A celery_app worker -l info --concurrency=4 -n worker1@%h
```

Main address is a startup parameter (`MAIN_IP`). Workers auto-reconnect with
backoff if main restarts.

## Submit work

```bash
MAIN_IP=192.168.1.10 REDIS_PASSWORD=... python submit.py 10
```

## Plug in real compute

Replace the body of `do_work()` in `tasks.py` (e.g. VieNeu-TTS inference).
Keep it idempotent per `job_id` — a worker crash re-runs the task elsewhere.

## Self-check

```bash
python tasks.py    # prints "ok"
```
