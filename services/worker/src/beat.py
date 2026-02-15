import os
from celery import Celery
from celery.schedules import schedule

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))

celery = Celery("beat", broker=REDIS_URL, backend=REDIS_URL)

celery.conf.beat_schedule = {
  "scan_due_trips": {
    "task": "worker.tasks.scan_due_trips",
    "schedule": schedule(run_every=SCAN_INTERVAL_SECONDS),
  }
}
