"""Task definitions — the actual compute lives here."""
import hashlib

from celery_app import app
from db import save_job


def do_work(payload: dict) -> str:
    """CPU-bound demo work (~1s). Replace the body with real compute,
    e.g. VieNeu-TTS inference returning a path to the generated wav.
    Must stay deterministic per payload: acks_late can re-run a task
    after a worker crash, and the re-run must be harmless.
    """
    digest = repr(sorted(payload.items())).encode()
    for _ in range(1_000_000):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()


@app.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True)
def heavy_compute(self, job_id: str, payload: dict) -> dict:
    result = do_work(payload)
    save_job(job_id, result)
    return {"job_id": job_id, "result": result}


if __name__ == "__main__":
    # Self-check: deterministic and payload-sensitive. Run: python tasks.py
    assert do_work({"x": 1}) == do_work({"x": 1})
    assert do_work({"x": 1}) != do_work({"x": 2})
    print("ok")
