"""Paced Garmin Personal API requests and activity downloads."""
from __future__ import annotations

from pathlib import Path
import io
import json
import logging
import random
import sys
import time
import urllib.error
import urllib.request
import zipfile

from garmin_library.activities import import_file

PERSONAL_API = "https://personal.connectapi.garmin.com"
API = PERSONAL_API

LOG = logging.getLogger(__name__)

def api_request(url: str, credential: str, method="GET", auth_scheme="Bearer", accept="*/*", retries=5):
    """Call Garmin with bounded retries for 429 and transient server errors."""
    headers = {"Authorization": f"{auth_scheme} {credential}", "Accept": accept}
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 300) if retry_after else min(2 ** attempt, 60)
            except ValueError:
                delay = min(2 ** attempt, 60)
            delay += random.uniform(0, min(1.0, delay * 0.1))
            LOG.warning("Garmin returned HTTP %s; retrying in %.1f seconds (%d/%d)", exc.code, delay, attempt + 1, retries)
            time.sleep(delay)


def fetch_new(context):
    key = context.settings.api_key
    if not key:
        sys.exit("Set GARMIN_API_KEY in your environment first.")
    context.settings.activities_path.mkdir(parents=True, exist_ok=True)
    token_data = api_request(PERSONAL_API + "/personalapitoken-service/personal-api/personal-api-key/token", key, "POST", auth_scheme="ApiKey")
    # Garmin's token endpoint returns the bearer JWT as plain text, not JSON.
    token = token_data.decode("utf-8").strip()
    if not token or token.count(".") != 2:
        preview = token[:80] if token else "<empty response>"
        sys.exit(f"Garmin returned an unexpected token response ({preview!r}); expected a JWT in plain text.")
    db = context.connect()
    known = {r[0] for r in db.execute("SELECT id FROM activities")}
    # Personal API activity endpoint supports paginated activity summaries.
    start = 0
    downloaded = 0
    last_request_at = 0.0

    def paced_request(*args, **kwargs):
        nonlocal last_request_at
        wait = 1.0 - (time.monotonic() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        result = api_request(*args, **kwargs)
        last_request_at = time.monotonic()
        return result

    while True:
        payload = paced_request(f"{API}/activitylist-service/activities/search/activities?start={start}&limit=100", token)
        items = json.loads(payload)
        if not isinstance(items, list) or not items:
            break
        for item in items:
            aid = str(item.get("activityId", ""))
            if not aid or aid in known:
                continue
            try:
                data = paced_request(
                    f"{API}/download-service/files/activity/{aid}", token,
                    accept="application/zip, application/x-zip-compressed, application/octet-stream",
                )
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    file_name = next((name for suffix in (".fit", ".gpx") for name in archive.namelist()
                                      if name.lower().endswith(suffix)), None)
                    if file_name is None:
                        LOG.warning("No FIT or GPX file in download for activity %s", aid)
                        continue
                    target = context.settings.activities_path / f"{aid}{Path(file_name).suffix.lower()}"
                    target.write_bytes(archive.read(file_name))
                if import_file(db, target):
                    downloaded += 1
                    known.add(aid)
            except (urllib.error.URLError, OSError):
                LOG.exception("Download failed for activity %s", aid)
        start += len(items)
        if len(items) < 100:
            break
    db.close()
    print(f"Downloaded and imported {downloaded} new activities")
