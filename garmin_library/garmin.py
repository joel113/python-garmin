"""Paced Garmin Personal API requests and activity downloads."""
from __future__ import annotations

from pathlib import Path
from contextlib import closing
import io
import json
import logging
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile

from garmin_library.activities import import_file
from garmin_library.workouts import fetch_activity_equipment, garmin_activity_id

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


class GarminClient:
    """One token and one pacing clock for all requests in a CLI run."""

    def __init__(self, context):
        key = context.settings.api_key
        if not key:
            sys.exit('Set GARMIN_API_KEY in .env or your environment first.')
        try:
            token_data = api_request(PERSONAL_API + '/personalapitoken-service/personal-api/personal-api-key/token',
                                     key, 'POST', auth_scheme='ApiKey')
            self.token = token_data.decode('utf-8').strip()
        except (urllib.error.URLError, OSError, UnicodeError):
            sys.exit('Could not obtain a Garmin access token. Check your credentials and connection.')
        # Never include token responses or credentials in errors.
        if not self.token or self.token.count('.') != 2:
            sys.exit('Garmin returned an invalid access token.')
        self.last_request_at = time.monotonic()

    def get(self, path, accept='*/*'):
        wait = 1.0 - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            return api_request(API + path, self.token, accept=accept)
        finally:
            self.last_request_at = time.monotonic()


def fetch_new(context):
    client = GarminClient(context)
    context.settings.activities_path.mkdir(parents=True, exist_ok=True)
    with closing(context.connect()) as db:
        known = {r[0] for r in db.execute('SELECT id FROM activities')}
    start = downloaded = 0
    while True:
        try:
            items = json.loads(client.get(f'/activitylist-service/activities/search/activities?start={start}&limit=100'))
        except urllib.error.HTTPError as exc:
            sys.exit(f'Garmin activity listing failed (HTTP {exc.code}). Already imported activities are preserved.')
        if not isinstance(items, list):
            sys.exit('Garmin returned an invalid activity list.')
        if not items:
            break
        for item in items:
            aid = str(item.get('activityId', ''))
            if not garmin_activity_id(aid) or aid in known:
                continue
            try:
                data = client.get(f'/download-service/files/activity/{aid}',
                                  accept='application/zip, application/x-zip-compressed, application/octet-stream')
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    file_name = next((name for suffix in ('.fit', '.gpx') for name in archive.namelist()
                                      if name.lower().endswith(suffix)), None)
                    if file_name is None:
                        LOG.warning('No FIT or GPX file in download for activity %s', aid)
                        continue
                    target = context.settings.activities_path / f'{aid}{Path(file_name).suffix.lower()}'
                    target.write_bytes(archive.read(file_name))
                with closing(context.connect()) as db:
                    imported = import_file(db, target)
                if imported:
                    downloaded += 1
                    known.add(aid)
                    try:
                        fetch_activity_equipment(context, client, aid)
                    except sqlite3.Error:
                        LOG.warning('Activity %s was imported, but workout metadata could not be saved. Retry with fetch-equipment.', aid)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    sys.exit('Garmin authentication failed. Already imported activities are preserved.')
                LOG.warning('Download failed for activity %s (HTTP %s)', aid, exc.code)
            except (urllib.error.URLError, OSError, zipfile.BadZipFile):
                LOG.warning('Download failed for activity %s; retry the fetch command.', aid)
        start += len(items)
        if len(items) < 100:
            break
    print(f"Downloaded and imported {downloaded} new activities")


def fetch_equipment(context, activity_id=None, refresh=False):
    try:
        _fetch_equipment(context, activity_id, refresh)
    except sqlite3.OperationalError as exc:
        if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
            raise
        sys.exit('The activity database is busy with background work. Completed equipment downloads are saved; rerun fetch-equipment to resume.')


def _fetch_equipment(context, activity_id=None, refresh=False):
    with closing(context.connect()) as db:
        rows = db.execute('''SELECT a.id,w.fetched_at,w.last_error FROM activities a
            LEFT JOIN activity_workouts w ON w.activity_id=a.id
            WHERE (? IS NULL OR a.id=?) ORDER BY a.start_time DESC,a.id''', (activity_id, activity_id)).fetchall()
    if activity_id is not None and not rows:
        sys.exit('Activity not found in the local library.')
    skipped = sum(not garmin_activity_id(row['id']) for row in rows)
    ids = [row['id'] for row in rows if garmin_activity_id(row['id'])
           and (refresh or row['fetched_at'] is None or row['last_error'] is not None)]
    if not ids:
        print(f'No workout equipment needs fetching; {skipped} non-Garmin activity IDs skipped. Use --refresh to update completed results.')
        return
    client = GarminClient(context)
    completed = 0
    for index, aid in enumerate(ids, 1):
        LOG.info('Fetching workout equipment %d/%d for activity %s', index, len(ids), aid)
        completed += fetch_activity_equipment(context, client, aid)
    print(f'Workout equipment: {completed} fetched, {len(ids)-completed} failed, {skipped} non-Garmin activity IDs skipped.')
