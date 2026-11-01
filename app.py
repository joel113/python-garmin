"""Local Garmin FIT library: import FIT files, fetch new activities, and browse tracks."""
from __future__ import annotations

import argparse
import json
import logging
import random
import os
import io
import sqlite3
import sys
import csv
import time
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from itertools import chain, groupby
import zipfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit, parse_qs, unquote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fitparse
import folium

ROOT = Path(__file__).resolve().parent
ACTIVITIES = ROOT / "activities"
DB_PATH = ROOT / "activities.sqlite3"
PERSONAL_API = "https://personal.connectapi.garmin.com"
API = PERSONAL_API
LOG = logging.getLogger("app")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load simple KEY=VALUE entries without overriding the shell environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS activities (
        id TEXT PRIMARY KEY, file_path TEXT NOT NULL, start_time TEXT,
        sport TEXT, duration_s REAL, distance_m REAL, imported_at TEXT DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS track_points (
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        timestamp TEXT, altitude REAL, PRIMARY KEY(activity_id, seq)
      );
      CREATE INDEX IF NOT EXISTS track_activity_idx ON track_points(activity_id, seq);
    """)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def activity_id(path: Path) -> str:
    # Garmin-exported filenames commonly end in _<activity-id>.fit.
    stem = path.stem
    return stem.rsplit("_", 1)[-1] if "_" in stem else stem


def activity_files():
    return sorted({p for pattern in ("*.fit", "*.FIT", "*.gpx", "*.GPX") for p in ACTIVITIES.glob(pattern)})


def parse_gpx(path: Path):
    root = ET.parse(path).getroot()
    points = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"trkpt", "rtept", "wpt"}:
            continue
        lat, lon = element.get("lat"), element.get("lon")
        if lat is None or lon is None:
            continue
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in element}
        points.append((float(lat), float(lon), values.get("time", ""),
                       float(values["ele"]) if values.get("ele") else None))
    return points


def distance_between(a, b):
    radius = 6_371_000
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(hav))


def display_segments(rows, stride=1):
    """Split GPS discontinuities before sampling; leave stored records untouched."""
    segments, segment = [], []
    previous = None
    last_coordinate = None
    count = 0

    def finish():
        if count > 1:
            if segment[-1] != last_coordinate:
                segment.append(last_coordinate)
            if len(segment) > 1:
                segments.append(segment)

    for row in rows:
        lat, lon, raw_time = row["lat"], row["lon"], row["timestamp"]
        try:
            timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
            seconds = timestamp.timestamp()
        except (AttributeError, ValueError, TypeError):
            seconds = None
        coordinate = [lat, lon]
        discontinuity = False
        if previous is not None:
            distance = distance_between(previous, coordinate)
            elapsed = seconds - previous[2] if seconds is not None and previous[2] is not None else None
            if elapsed is None:
                discontinuity = distance > 10_000
            else:
                discontinuity = (elapsed < 0 or
                                 (elapsed > 300 and distance > 500) or
                                 (distance > 300 and (elapsed <= 0 or distance / elapsed > 55)))
        if discontinuity:
            finish()
            segment, count = [], 0
        if count % stride == 0:
            segment.append(coordinate)
        count += 1
        last_coordinate = coordinate
        previous = (lat, lon, seconds)
    finish()
    return segments


def import_file(db: sqlite3.Connection, path: Path) -> bool:
    aid = activity_id(path)
    try:
        if path.suffix.lower() == ".gpx":
            raw_points = parse_gpx(path)
            times = [p[2] for p in raw_points if p[2]]
            start_time = times[0] if times else ""
            try:
                duration = (datetime.fromisoformat(times[-1].replace("Z", "+00:00")) -
                            datetime.fromisoformat(times[0].replace("Z", "+00:00"))).total_seconds() if len(times) > 1 else None
            except ValueError:
                duration = None
            distance = sum(distance_between(a, b) for a, b in zip(raw_points, raw_points[1:]))
            meta = {"start_time": start_time, "sport": "gpx", "duration_s": duration, "distance_m": distance}
            points = [(aid, seq, lat, lon, timestamp, altitude)
                      for seq, (lat, lon, timestamp, altitude) in enumerate(raw_points)]
        else:
            fit = fitparse.FitFile(str(path))
            session = next(iter(fit.get_messages("session")), None)
            meta = {
                "start_time": session.get_value("start_time") if session else None,
                "sport": str(session.get_value("sport")) if session else None,
                "duration_s": session.get_value("total_timer_time") if session else None,
                "distance_m": session.get_value("total_distance") if session else None,
            }
            points = []
            for record in fit.get_messages("record"):
                lat, lon = record.get_value("position_lat"), record.get_value("position_long")
                if lat is None or lon is None:
                    continue
                points.append((aid, len(points), lat * 180 / 2**31, lon * 180 / 2**31,
                               str(record.get_value("timestamp") or ""), record.get_value("altitude")))
        db.execute("DELETE FROM activities WHERE id=?", (aid,))
        db.execute("INSERT INTO activities(id,file_path,start_time,sport,duration_s,distance_m) VALUES(?,?,?,?,?,?)",
                   (aid, str(path.relative_to(ROOT)), str(meta["start_time"] or ""), meta["sport"], meta["duration_s"], meta["distance_m"]))
        db.executemany("INSERT INTO track_points(activity_id,seq,lat,lon,timestamp,altitude) VALUES(?,?,?,?,?,?)", points)
        db.commit()
        return True
    except Exception:
        db.rollback()
        LOG.exception("Could not import %s", path)
        return False


def import_all():
    ACTIVITIES.mkdir(exist_ok=True)
    db = connect()
    ok = sum(import_file(db, p) for p in activity_files())
    db.close()
    print(f"Imported {ok} FIT files into {DB_PATH}")


def export_csv():
    """Export one row per FIT session, preserving the former main.py output."""
    import pandas as pd

    rows = []
    for path in activity_files():
        try:
            if path.suffix.lower() == ".gpx":
                points = parse_gpx(path)
                ts = [p[2] for p in points if p[2]]
                try:
                    duration = (datetime.fromisoformat(ts[-1].replace("Z", "+00:00")) - datetime.fromisoformat(ts[0].replace("Z", "+00:00"))).total_seconds() if len(ts) > 1 else None
                except ValueError:
                    duration = None
                rows.append({"start_time": ts[0] if ts else "", "duration_s": duration,
                             "distance_m": sum(distance_between(a, b) for a, b in zip(points, points[1:])),
                             "activity_type": "gpx"})
            else:
                fit = fitparse.FitFile(str(path))
                for session in fit.get_messages("session"):
                    rows.append({"start_time": session.get_value("start_time"), "duration_s": session.get_value("total_timer_time"),
                                 "distance_m": session.get_value("total_distance"), "activity_type": session.get_value("sport")})
        except Exception:
            LOG.exception("Could not read session data from %s", path)
    target = ROOT / "analysis" / "activities.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["start_time", "duration_s", "distance_m", "activity_type"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} sessions to {target}")


def export_map():
    """Generate the former standalone Folium map from the FIT files."""
    map_view = folium.Map(location=[48.1351, 11.5820], zoom_start=12)
    for path in activity_files():
        try:
            if path.suffix.lower() == ".gpx":
                points = [(lat, lon) for lat, lon, _, _ in parse_gpx(path)]
            else:
                points = []
                fit = fitparse.FitFile(str(path))
                for record in fit.get_messages("record"):
                    lat, lon = record.get_value("position_lat"), record.get_value("position_long")
                    if lat is not None and lon is not None:
                        points.append((lat * 180 / 2**31, lon * 180 / 2**31))
            if points:
                folium.PolyLine(points, color="blue", weight=2.5, opacity=1,
                                tooltip=path.name).add_to(map_view)
        except Exception:
            LOG.exception("Could not plot %s", path)
    target = ROOT / "analysis" / "activities_map.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    map_view.save(str(target))
    print(f"Saved static map to {target}")


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


def fetch_new():
    key = os.environ.get("GARMIN_API_KEY")
    if not key:
        sys.exit("Set GARMIN_API_KEY in your environment first.")
    token_data = api_request(PERSONAL_API + "/personalapitoken-service/personal-api/personal-api-key/token", key, "POST", auth_scheme="ApiKey")
    # Garmin's token endpoint returns the bearer JWT as plain text, not JSON.
    token = token_data.decode("utf-8").strip()
    if not token or token.count(".") != 2:
        preview = token[:80] if token else "<empty response>"
        sys.exit(f"Garmin returned an unexpected token response ({preview!r}); expected a JWT in plain text.")
    db = connect()
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
                    target = ACTIVITIES / f"{aid}{Path(file_name).suffix.lower()}"
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/activities"):
            with connect() as db:
                rows = [dict(r) for r in db.execute("SELECT id,start_time,sport,duration_s,distance_m, EXISTS(SELECT 1 FROM track_points p WHERE p.activity_id=activities.id) AS has_track FROM activities ORDER BY start_time DESC")]
            return self.send_json(rows)
        if self.path.startswith("/api/heatmap"):
            sport = parse_qs(urlsplit(self.path).query).get("sport", ["all"])[0]
            with connect() as db:
                where, params = ("WHERE 1=1", ()) if sport == "all" else ("WHERE a.sport = ?", (sport,))
                total = db.execute(f"SELECT COUNT(*) FROM track_points p JOIN activities a ON a.id=p.activity_id {where}", params).fetchone()[0]
                # Retain ten times more detail than the original overview budget.
                stride = max(1, (total + 799999) // 800000)
                routes = []
                rows = db.execute(
                    f"SELECT a.id,a.sport,p.lat,p.lon,p.timestamp FROM track_points p JOIN activities a ON a.id=p.activity_id {where} ORDER BY p.activity_id,p.seq",
                    params,
                )
                for aid, records in groupby(rows, key=lambda row: row["id"]):
                    first = next(records)
                    segments = display_segments(chain([first], records), stride)
                    if segments:
                        routes.append({"id": aid, "sport": first["sport"], "segments": segments})
            return self.send_json(routes)
        if self.path.startswith("/api/track/"):
            aid = self.path.split("/api/track/", 1)[1].split("?", 1)[0]
            with connect() as db:
                segments = display_segments(db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)))
            return self.send_json(segments)
        if self.path == "/" or self.path.startswith("/?"):
            self.path = "/index.html"
        return super().do_GET()

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(data)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import", help="Import FIT files from activities/")
    sub.add_parser("fetch", help="Fetch new Garmin activities (requires GARMIN_API_KEY)")
    sub.add_parser("export-csv", help="Export session metadata to analysis/activities.csv")
    sub.add_parser("export-map", help="Export a static Folium map to analysis/activities_map.html")
    serve = sub.add_parser("serve", help="Run the local activity map")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "import": import_all()
    elif args.command == "fetch": fetch_new()
    elif args.command == "export-csv": export_csv()
    elif args.command == "export-map": export_map()
    else:
        if not DB_PATH.exists(): import_all()
        print(f"Open http://127.0.0.1:{args.port}")
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
