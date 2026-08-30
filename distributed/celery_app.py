"""Celery app shared by main and workers.

Workers get the main address at startup:
    MAIN_IP=192.168.1.10 REDIS_PASSWORD=... celery -A celery_app worker
"""
import os

from celery import Celery

MAIN_IP = os.environ.get("MAIN_IP", "127.0.0.1")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "changeme")
REDIS_URL = f"redis://:{REDIS_PASSWORD}@{MAIN_IP}:6379/0"

app = Celery("cluster", broker=REDIS_URL, backend=REDIS_URL, include=["tasks"])

app.conf.update(
    # Fair scheduling: an idle worker always gets the next task,
    # a busy worker never hoards prefetched tasks.
    worker_prefetch_multiplier=1,
    # Reliability: task is acked only after it finishes, so a worker
    # that dies mid-task leaves the task on the queue for someone else.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Kill runaway tasks, retry elsewhere.
    task_time_limit=600,
    task_soft_time_limit=540,
    # Results kept 1h in Redis.
    result_expires=3600,
    # Workers survive main/Redis restarts.
    broker_connection_retry_on_startup=True,
)
