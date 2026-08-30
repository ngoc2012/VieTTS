"""Submit jobs to the cluster and wait for results.

Usage: python submit.py [n_jobs]
"""
import sys
import uuid

from tasks import heavy_compute


def main(n: int) -> None:
    async_results = [
        heavy_compute.delay(f"job-{uuid.uuid4().hex[:8]}", {"i": i})
        for i in range(n)
    ]
    for r in async_results:
        out = r.get(timeout=600)
        print(f"{out['job_id']}  done  {out['result'][:16]}…")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
