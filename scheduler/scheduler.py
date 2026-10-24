"""
scheduler.py
============
Starts scheduled scrape jobs. Run it on exactly one host; uploader servers
themselves never schedule anything.

Each tick calls POST /api/v1/scrape/run-due on an uploader server, which claims
every due job with an S3 conditional write and runs it in the background, so a
tick that overlaps a manual run (or a second scheduler by mistake) can't start
a job twice.

    python scheduler.py           # one tick
    python scheduler.py --loop    # tick every 60 s (the container's default command)

Reads UPLOADER_URL (default http://localhost:3000) and APP_API_KEY from the
environment (docker compose loads them from .env). Stdlib only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

UPLOADER_URL = os.getenv("UPLOADER_URL", "http://localhost:3000").rstrip("/")
APP_API_KEY = os.getenv("APP_API_KEY", "")
INTERVAL_SECONDS = 60


def tick() -> None:
    req = urllib.request.Request(f"{UPLOADER_URL}/api/v1/scrape/run-due", method="POST")
    if APP_API_KEY:
        req.add_header("X-API-Key", APP_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            started = json.loads(resp.read())["started"]
        if started:
            print(f"[scheduler] started {len(started)} job(s): {', '.join(started)}", flush=True)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"[scheduler] run-due failed: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        while True:
            tick()
            time.sleep(INTERVAL_SECONDS)
    else:
        tick()
