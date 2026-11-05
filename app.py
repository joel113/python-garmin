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
import threading
import signal
import hashlib
import uuid
from contextlib import closing
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from itertools import chain, groupby
import zipfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit, parse_qs, unquote, quote
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fitparse
import folium

ROOT = Path(__file__).resolve().parent
ACTIVITIES = ROOT / "storage" / "activities"
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
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
      CREATE TABLE IF NOT EXISTS activity_temperature_cache (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
        file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
        status TEXT NOT NULL, message TEXT
      );
      CREATE TABLE IF NOT EXISTS planned_routes (
        id TEXT PRIMARY KEY, source_hash TEXT NOT NULL UNIQUE, stored_filename TEXT NOT NULL,
        original_filename TEXT NOT NULL, name TEXT NOT NULL, sport TEXT NOT NULL DEFAULT 'generic',
        notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        distance_m REAL NOT NULL, ascent_m REAL, elevation_coverage REAL NOT NULL,
        point_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS planned_route_points (
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        part INTEGER NOT NULL, seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        elevation REAL, PRIMARY KEY(route_id,part,seq)
      );
      CREATE TABLE IF NOT EXISTS planned_route_waypoints (
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, name TEXT, description TEXT,
        PRIMARY KEY(route_id,seq)
      );
      CREATE TABLE IF NOT EXISTS route_collections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS route_collection_members (
        collection_id TEXT NOT NULL REFERENCES route_collections(id) ON DELETE CASCADE,
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, PRIMARY KEY(collection_id,route_id), UNIQUE(collection_id,position)
      );
      CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, sport TEXT NOT NULL,
        radius_m REAL NOT NULL DEFAULT 30, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT,
        progress_done INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS segment_checkpoints (
        segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        PRIMARY KEY(segment_id,seq)
      );
      CREATE TABLE IF NOT EXISTS segment_traversals (
        id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, start_pos REAL NOT NULL, end_pos REAL NOT NULL,
        start_time TEXT, end_time TEXT, duration_s REAL,
        UNIQUE(segment_id,activity_id,ordinal)
      );
      CREATE INDEX IF NOT EXISTS traversal_segment_idx ON segment_traversals(segment_id,duration_s);
      CREATE TABLE IF NOT EXISTS activity_bounds (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
        min_lat REAL, max_lat REAL, min_lon REAL, max_lon REAL
      );
      CREATE TABLE IF NOT EXISTS destination_state (
        id INTEGER PRIMARY KEY CHECK(id=1), epsilon_m REAL NOT NULL DEFAULT 75,
        min_trips INTEGER NOT NULL DEFAULT 3, revision INTEGER NOT NULL DEFAULT 1,
        published_revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
        phase TEXT NOT NULL DEFAULT 'endpoints', error TEXT, progress_done INTEGER NOT NULL DEFAULT 0,
        progress_total INTEGER NOT NULL DEFAULT 0, changed_at REAL NOT NULL DEFAULT 0
      );
      INSERT OR IGNORE INTO destination_state(id) VALUES(1);
      CREATE TABLE IF NOT EXISTS activity_endpoints (
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('start','end')), lat REAL NOT NULL, lon REAL NOT NULL,
        recorded_at TEXT, PRIMARY KEY(activity_id,role)
      );
      CREATE TABLE IF NOT EXISTS destination_endpoint_sources (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS destinations (
        id TEXT PRIMARY KEY, name TEXT, seed_lat REAL NOT NULL, seed_lon REAL NOT NULL,
        centroid_lat REAL NOT NULL, centroid_lon REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS destination_memberships (
        activity_id TEXT NOT NULL, role TEXT NOT NULL, destination_id TEXT NOT NULL REFERENCES destinations(id),
        PRIMARY KEY(activity_id,role),
        FOREIGN KEY(activity_id,role) REFERENCES activity_endpoints(activity_id,role) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS destination_membership_idx ON destination_memberships(destination_id,activity_id);
      CREATE TRIGGER IF NOT EXISTS destinations_activity_insert AFTER INSERT ON activities BEGIN
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
      CREATE TRIGGER IF NOT EXISTS destinations_activity_delete AFTER DELETE ON activities BEGIN
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
      CREATE TRIGGER IF NOT EXISTS destinations_activity_update AFTER UPDATE OF start_time,sport ON activities BEGIN
        DELETE FROM activity_endpoints WHERE activity_id=NEW.id;
        DELETE FROM destination_endpoint_sources WHERE activity_id=NEW.id;
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
    """)
    db.execute("PRAGMA foreign_keys = ON")
    if "temperature_c" not in {row[1] for row in db.execute("PRAGMA table_info(track_points)")}:
        db.execute("BEGIN IMMEDIATE")
        if "temperature_c" not in {row[1] for row in db.execute("PRAGMA table_info(track_points)")}:
            db.execute("ALTER TABLE track_points ADD COLUMN temperature_c REAL")
        db.commit()
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
    return 2 * radius * math.asin(math.sqrt(min(1, hav)))


def point_time(raw_time):
    try:
        value = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def gps_discontinuity(previous, current):
    distance = distance_between(previous, current)
    elapsed = current[2] - previous[2] if current[2] is not None and previous[2] is not None else None
    if elapsed is None:
        return distance > 10_000
    return (elapsed < 0 or (elapsed > 300 and distance > 500) or
            (distance > 300 and (elapsed <= 0 or distance / elapsed > 55)))


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
        discontinuity = previous is not None and gps_discontinuity(previous, (lat, lon, seconds))
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


def track_speed_data(rows):
    """GPS-derived edge speeds in km/h, retaining the display's gap boundaries."""
    segments, coordinates, speeds, known = [], [], [], []
    previous = None

    def finish():
        if len(coordinates) >= 2:
            segments.append({"points": coordinates, "speeds_kmh": speeds})

    for row in rows:
        current = (row["lat"], row["lon"], point_time(row["timestamp"]))
        if previous is None or gps_discontinuity(previous, current):
            finish()
            coordinates, speeds = [], []
        elif coordinates:
            elapsed = current[2] - previous[2] if current[2] is not None and previous[2] is not None else None
            value = None
            if elapsed is not None and 0 < elapsed <= 300:
                candidate = distance_between(previous, current) / elapsed * 3.6
                if candidate <= 198:
                    value = candidate
                    known.append(value)
            speeds.append(value)
        coordinates.append([current[0], current[1]])
        previous = current
    finish()
    known.sort()
    # Use the 95th percentile so a few GPS spikes do not flatten the color range.
    if known:
        position = (len(known) - 1) * 0.95
        lower, upper = math.floor(position), math.ceil(position)
        ceiling = known[lower] + (known[upper] - known[lower]) * (position - lower)
    else:
        ceiling = None
    return {"segments": segments, "scale_max_kmh": ceiling,
            "known_edge_count": len(known), "source": "gps_timestamps"}


def temperature_value(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def file_temperature_points(path):
    """Align temperature with GPS-bearing records in the same order as import."""
    if path.suffix.lower() == ".fit":
        for record in fitparse.FitFile(str(path)).get_messages("record"):
            lat, lon = record.get_value("position_lat"), record.get_value("position_long")
            if lat is not None and lon is not None:
                yield lat * 180 / 2**31, lon * 180 / 2**31, temperature_value(record.get_value("temperature"))
    elif path.suffix.lower() == ".gpx":
        for element in ET.parse(path).getroot().iter():
            if element.tag.rsplit("}", 1)[-1] not in {"trkpt", "rtept", "wpt"}:
                continue
            if element.get("lat") is None or element.get("lon") is None:
                continue
            value = next((child.text for child in element.iter() if child.tag.rsplit("}", 1)[-1] == "atemp"), None)
            yield float(element.get("lat")), float(element.get("lon")), temperature_value(value)


def ensure_track_temperature(db, aid):
    activity = db.execute("SELECT file_path FROM activities WHERE id=?", (aid,)).fetchone()
    if activity is None:
        raise SegmentError(404, "Activity not found.")
    stored_path = Path(activity["file_path"])
    # Keep old database references usable after moving the originals. Store
    # portable paths so the same database works on the host and in /data.
    if stored_path.parent in {Path("activities"), Path("storage/activities")}:
        path = (ACTIVITIES / stored_path.name).resolve()
    else:
        path = (ROOT / stored_path).resolve()
    if not path.is_relative_to(ACTIVITIES.resolve()) or not path.is_file():
        if db.execute("SELECT 1 FROM track_points WHERE activity_id=? AND temperature_c IS NOT NULL LIMIT 1", (aid,)).fetchone():
            return {"status": "loaded", "message": None}
        return {"status": "unavailable", "message": "The source file is unavailable; recorded temperature cannot be loaded."}
    fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)
    cache = db.execute("SELECT * FROM activity_temperature_cache WHERE activity_id=?", (aid,)).fetchone()
    if cache and (cache["file_size"], cache["file_mtime_ns"]) == fingerprint:
        return dict(cache)
    try:
        readings = list(file_temperature_points(path))
    except Exception:
        LOG.exception("Could not read temperature from %s", path.name)
        return {"status": "unavailable", "message": "Recorded temperature could not be read from the source file."}
    db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute("SELECT file_path FROM activities WHERE id=?", (aid,)).fetchone()
        records = db.execute("SELECT seq,lat,lon FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)).fetchall()
        if (current is None or current["file_path"] != activity["file_path"] or
            (path.stat().st_size, path.stat().st_mtime_ns) != fingerprint or
            len(records) != len(readings) or any(abs(row["lat"]-reading[0])>1e-7 or abs(row["lon"]-reading[1])>1e-7 for row, reading in zip(records, readings))):
            db.rollback()
            return {"status": "unavailable", "message": "The source file differs from the imported track; reimport this activity to load temperature."}
        db.executemany("UPDATE track_points SET temperature_c=? WHERE activity_id=? AND seq=?",
                       [(reading[2], aid, row["seq"]) for row, reading in zip(records, readings)])
        available = any(reading[2] is not None for reading in readings)
        status, message = ("loaded", None) if available else ("unavailable", "No recorded temperature data for this track.")
        db.execute("INSERT OR REPLACE INTO activity_temperature_cache VALUES(?,?,?,?,?)", (aid, *fingerprint, status, message))
        db.commit()
        return {"status": status, "message": message}
    except Exception:
        db.rollback()
        raise


def track_temperature_data(rows):
    segments, coordinates, temperatures, known = [], [], [], []
    previous = None

    def finish():
        if len(coordinates) >= 2:
            segments.append({"points": coordinates, "temperatures_c": temperatures})

    for row in rows:
        current = (row["lat"], row["lon"], point_time(row["timestamp"]), temperature_value(row["temperature_c"]))
        if previous is None or gps_discontinuity(previous, current):
            finish()
            coordinates, temperatures = [], []
        elif coordinates:
            readings = [v for v in (previous[3], current[3]) if v is not None]
            value = sum(readings)/len(readings) if readings else None
            temperatures.append(value)
            if value is not None:
                known.append(value)
        coordinates.append([current[0], current[1]])
        previous = current
    finish()
    return {"segments": segments, "scale_min_c": min(known) if known else None,
            "scale_max_c": max(known) if known else None, "known_edge_count": len(known),
            "source": "recorded_temperature"}


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
            readings = list(file_temperature_points(path))
            points = [(*p, readings[index][2]) for index, p in enumerate(points)]
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
                               str(record.get_value("timestamp") or ""), record.get_value("altitude"),
                               temperature_value(record.get_value("temperature"))))
        old = db.execute("SELECT sport FROM activities WHERE id=?", (aid,)).fetchone()
        db.execute("DELETE FROM activities WHERE id=?", (aid,))
        db.execute("INSERT INTO activities(id,file_path,start_time,sport,duration_s,distance_m) VALUES(?,?,?,?,?,?)",
                   (aid, str(Path("storage/activities") / path.name), str(meta["start_time"] or ""), meta["sport"], meta["duration_s"], meta["distance_m"]))
        db.executemany("INSERT INTO track_points(activity_id,seq,lat,lon,timestamp,altitude,temperature_c) VALUES(?,?,?,?,?,?,?)", points)
        available = any(p[6] is not None for p in points)
        db.execute("INSERT INTO activity_temperature_cache VALUES(?,?,?,?,?)",
                   (aid, path.stat().st_size, path.stat().st_mtime_ns, "loaded" if available else "unavailable",
                    None if available else "No recorded temperature data for this track."))
        if points:
            db.execute("INSERT INTO activity_bounds VALUES(?,?,?,?,?)", (aid,
                       min(p[2] for p in points), max(p[2] for p in points),
                       min(p[3] for p in points), max(p[3] for p in points)))
        db.execute("UPDATE segments SET revision=revision+1,status='pending',error=NULL,progress_done=0,progress_total=0 WHERE sport IN (?,?)",
                   (meta["sport"], old[0] if old else meta["sport"]))
        db.commit()
        return True
    except Exception:
        db.rollback()
        LOG.exception("Could not import %s", path)
        return False


def import_all():
    ACTIVITIES.mkdir(parents=True, exist_ok=True)
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
    ACTIVITIES.mkdir(parents=True, exist_ok=True)
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


def circle_entry(a, b, checkpoint, radius):
    """First intersection fraction of a GPS edge with a checkpoint circle."""
    lat, lon = checkpoint
    scale_x = 111_195 * math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * scale_x, (a[0] - lat) * 111_195
    bx, by = (b[1] - lon) * scale_x, (b[0] - lat) * 111_195
    if ax * ax + ay * ay <= radius * radius:
        return 0.0
    dx, dy = bx - ax, by - ay
    square = dx * dx + dy * dy
    if square == 0:
        return None
    dot = ax * dx + ay * dy
    discriminant = dot * dot - square * (ax * ax + ay * ay - radius * radius)
    if discriminant < 0:
        return None
    fraction = (-dot - math.sqrt(discriminant)) / square
    return fraction if 0 <= fraction <= 1 else None


def match_segment(rows, checkpoints, radius):
    """Yield visits/forward attempts using every point and interpolated crossings."""
    armed = [True] * len(checkpoints)
    previous = None
    attempt = None
    for row in rows:
        current = (row["lat"], row["lon"], point_time(row["timestamp"]), row["seq"])
        if previous is None or gps_discontinuity(previous, current):
            previous = current
            armed = [True] * len(checkpoints)
            attempt = None
            a = current
        else:
            a = previous
        if attempt and (a[2] is None or current[2] is None):
            attempt["valid"] = False
        events = []
        for index, checkpoint in enumerate(checkpoints):
            if armed[index]:
                fraction = circle_entry(a, current, checkpoint, radius)
                if fraction is not None:
                    position = a[3] + fraction * (current[3] - a[3])
                    seconds = a[2] + fraction * (current[2] - a[2]) if a[2] is not None and current[2] is not None else None
                    events.append((position, index, seconds))
                    armed[index] = False
            if distance_between(current, checkpoint) > radius * 1.5:
                armed[index] = True
        for position, index, seconds in sorted(events):
            if len(checkpoints) == 1:
                yield (position, position, seconds, seconds, None)
            elif index == 0:
                attempt = {"start": position, "last": position, "time": seconds,
                           "next": 1, "valid": seconds is not None and a[2] is not None and current[2] is not None}
            elif attempt and index == attempt["next"] and position > attempt["last"]:
                attempt["last"] = position
                attempt["next"] += 1
                if seconds is None:
                    attempt["valid"] = False
                if attempt["next"] == len(checkpoints):
                    duration = seconds - attempt["time"] if attempt["valid"] and seconds is not None else None
                    if duration is not None and duration <= 0:
                        duration = None
                    yield (attempt["start"], position, attempt["time"], seconds, duration)
                    attempt = None
        previous = current


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat() if seconds is not None else None


def segment_worker(stop):
    """Publish cached matches only if the processed revision is still current."""
    try:
        with closing(connect()) as db:
            db.execute("UPDATE segments SET status='pending' WHERE status='processing'")
            db.execute("""INSERT OR IGNORE INTO activity_bounds
                SELECT activity_id,MIN(lat),MAX(lat),MIN(lon),MAX(lon)
                FROM track_points WHERE activity_id NOT IN (SELECT activity_id FROM activity_bounds)
                GROUP BY activity_id""")
            db.commit()
    except Exception:
        LOG.exception("Could not initialize segment matching")
        return
    while not stop.is_set():
        sid, revision = None, None
        try:
            with closing(connect()) as db:
                job = db.execute("SELECT * FROM segments WHERE status='pending' ORDER BY updated_at,id LIMIT 1").fetchone()
                if job is None:
                    stop.wait(1)
                    continue
                sid, revision = job["id"], job["revision"]
                db.execute("UPDATE segments SET status='processing',error=NULL WHERE id=? AND revision=?", (sid, revision))
                db.commit()
                checkpoints = [(r["lat"], r["lon"]) for r in db.execute(
                    "SELECT lat,lon FROM segment_checkpoints WHERE segment_id=? ORDER BY seq", (sid,))]
                if not checkpoints:
                    continue
                radius = job["radius_m"]
                lat_pad = radius / 111_195
                lon_pad = radius / (111_195 * max(0.001, min(math.cos(math.radians(p[0])) for p in checkpoints)))
                candidates = db.execute("""SELECT a.id FROM activities a JOIN activity_bounds b ON b.activity_id=a.id
                    WHERE a.sport=? AND b.max_lat>=? AND b.min_lat<=? AND b.max_lon>=? AND b.min_lon<=?""",
                    (job["sport"], max(p[0] for p in checkpoints) - lat_pad,
                     min(p[0] for p in checkpoints) + lat_pad,
                     max(p[1] for p in checkpoints) - lon_pad, min(p[1] for p in checkpoints) + lon_pad)).fetchall()
                db.execute("UPDATE segments SET progress_total=?,progress_done=0 WHERE id=? AND revision=?",
                           (len(candidates), sid, revision))
                db.commit()
                matches = []
                for done, activity in enumerate(candidates, 1):
                    current = db.execute("SELECT revision FROM segments WHERE id=?", (sid,)).fetchone()
                    if stop.is_set() or current is None or current[0] != revision:
                        break
                    records = db.execute("SELECT seq,lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq",
                                         (activity["id"],)).fetchall()
                    for ordinal, match in enumerate(match_segment(records, checkpoints, radius), 1):
                        start, end, start_time, end_time, duration = match
                        matches.append((sid, activity["id"], ordinal, start, end,
                                        iso_time(start_time), iso_time(end_time), duration))
                    db.execute("UPDATE segments SET progress_done=? WHERE id=? AND revision=?", (done, sid, revision))
                    db.commit()
                if stop.is_set():
                    continue
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT revision FROM segments WHERE id=?", (sid,)).fetchone()
                if current is not None and current[0] == revision:
                    db.execute("DELETE FROM segment_traversals WHERE segment_id=?", (sid,))
                    db.executemany("""INSERT INTO segment_traversals
                        (segment_id,activity_id,ordinal,start_pos,end_pos,start_time,end_time,duration_s)
                        VALUES(?,?,?,?,?,?,?,?)""", matches)
                    db.execute("UPDATE segments SET status='completed',error=NULL WHERE id=?", (sid,))
                db.commit()
        except Exception:
            LOG.exception("Segment matching failed for segment %s", sid)
            if sid is not None:
                with closing(connect()) as db:
                    db.execute("UPDATE segments SET status='failed',error='Matching failed. Edit and save the segment to retry.' WHERE id=? AND revision=?",
                               (sid, revision))
                    db.commit()
            stop.wait(1)


class SegmentError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def segment_detail(db, sid):
    row = db.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise SegmentError(404, "Segment not found.")
    result = dict(row)
    result["checkpoints"] = [list(p) for p in db.execute(
        "SELECT lat,lon FROM segment_checkpoints WHERE segment_id=? ORDER BY seq", (sid,))]
    result.update(traversal_count=0, activity_count=0, fastest=None)
    if row["status"] == "completed":
        counts = db.execute("SELECT COUNT(*),COUNT(DISTINCT activity_id) FROM segment_traversals WHERE segment_id=?", (sid,)).fetchone()
        result["traversal_count"], result["activity_count"] = counts
        fastest = db.execute("SELECT * FROM segment_traversals WHERE segment_id=? AND duration_s IS NOT NULL ORDER BY duration_s,id LIMIT 1", (sid,)).fetchone()
        result["fastest"] = dict(fastest) if fastest else None
    return result


def validate_segment(db, payload):
    name, sport = payload.get("name"), payload.get("sport")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise SegmentError(400, "Enter a segment name of 1–120 characters.")
    if not isinstance(sport, str) or not db.execute("SELECT 1 FROM activities WHERE sport=? LIMIT 1", (sport,)).fetchone():
        raise SegmentError(400, "Select an existing sport.")
    radius = payload.get("radius_m", 30)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or not 5 <= radius <= 500:
        raise SegmentError(400, "GPS tolerance must be between 5 and 500 metres.")
    points = payload.get("checkpoints")
    if not isinstance(points, list) or not 1 <= len(points) <= 100:
        raise SegmentError(400, "Place between 1 and 100 checkpoints.")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point) or
            not -85 <= point[0] <= 85 or not -180 <= point[1] <= 180):
            raise SegmentError(400, "Checkpoints must contain valid latitude and longitude coordinates (latitude within ±85°).")
    if any(a == b for a, b in zip(points, points[1:])):
        raise SegmentError(400, "Adjacent checkpoints must be different.")
    return name.strip(), sport, radius, points


def traversal_geometry(db, traversal):
    start, end = traversal["start_pos"], traversal["end_pos"]
    rows = db.execute("SELECT seq,lat,lon FROM track_points WHERE activity_id=? AND seq BETWEEN ? AND ? ORDER BY seq",
                      (traversal["activity_id"], math.floor(start), math.ceil(end))).fetchall()
    by_seq = {r["seq"]: r for r in rows}

    def at(position):
        a, b = by_seq.get(math.floor(position)), by_seq.get(math.ceil(position))
        if a is None or b is None:
            raise SegmentError(409, "Track data changed; wait for segment matching to finish.")
        fraction = position - math.floor(position)
        return [a["lat"] + fraction * (b["lat"] - a["lat"]), a["lon"] + fraction * (b["lon"] - a["lon"])]

    return [at(start)] + [[r["lat"], r["lon"]] for r in rows if start < r["seq"] < end] + ([at(end)] if end != start else [])


def extract_trip_endpoints(rows, start_time):
    """First/last points of usable continuous blocks, ignoring pre-activity data."""
    activity_start = point_time(start_time)
    first, last, block_start, previous, count = None, None, None, None, 0

    def finish():
        nonlocal first, last
        if count >= 2:
            if first is None:
                first = block_start
            last = previous

    for row in rows:
        lat, lon = row["lat"], row["lon"]
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            continue
        seconds = point_time(row["timestamp"])
        if activity_start is not None and seconds is not None and seconds < activity_start:
            continue
        current = (lat, lon, seconds)
        if previous is not None and gps_discontinuity(previous, current):
            finish()
            count = 0
        if count == 0:
            block_start = current
        previous = current
        count += 1
    finish()
    if first is None:
        return []
    return [(role, p[0], p[1], iso_time(p[2] if p[2] is not None else activity_start))
            for role, p in (("start", first), ("end", last))]


def cluster_destination_endpoints(endpoints, epsilon_m, min_trips):
    import numpy as np
    from sklearn.cluster import DBSCAN

    if not endpoints:
        return []
    endpoints = sorted(endpoints, key=lambda p: (p["lat"], p["lon"], p["activity_id"], p["role"]))
    coordinates = np.radians([[p["lat"], p["lon"]] for p in endpoints])
    # Exact duplicates share a weighted sample, preserving DBSCAN density semantics.
    unique, inverse, weights = np.unique(coordinates, axis=0, return_inverse=True, return_counts=True)
    labels = DBSCAN(eps=epsilon_m / 6_371_000, min_samples=min_trips,
                    metric="haversine", algorithm="ball_tree", n_jobs=1).fit_predict(unique, sample_weight=weights)[inverse]
    groups = {}
    for point, label in zip(endpoints, labels):
        if label >= 0:
            groups.setdefault(int(label), []).append(point)
    clusters = []
    for members in groups.values():
        if len({p["activity_id"] for p in members}) < min_trips:
            continue
        radians = np.radians([[p["lat"], p["lon"]] for p in members])
        lat, lon = radians[:, 0], radians[:, 1]
        x, y, z = np.mean(np.cos(lat) * np.cos(lon)), np.mean(np.cos(lat) * np.sin(lon)), np.mean(np.sin(lat))
        centroid = (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))
        clusters.append({"centroid": centroid, "members": {(p["activity_id"], p["role"]) for p in members}})
    return sorted(clusters, key=lambda c: (*c["centroid"], sorted(c["members"])))


def assign_destination_ids(clusters, previous, memberships, reserved, epsilon_m):
    """Reuse identities by overlap, then proximity; never recycle retired IDs."""
    assigned, used_old = {}, set()
    pairs = []
    for index, cluster in enumerate(clusters):
        for old in previous:
            old_members = memberships.get(old["id"], set())
            common = len(cluster["members"] & old_members)
            if common:
                jaccard = common / len(cluster["members"] | old_members)
                pairs.append((-common, -jaccard, old["id"], index))
    for _, _, old_id, index in sorted(pairs):
        if index not in assigned and old_id not in used_old:
            assigned[index] = old_id
            used_old.add(old_id)
    nearby = []
    for index, cluster in enumerate(clusters):
        if index in assigned:
            continue
        for old in previous:
            if old["id"] in used_old:
                continue
            distance = distance_between(cluster["centroid"], (old["centroid_lat"], old["centroid_lon"]))
            if distance <= epsilon_m:
                nearby.append((distance, old["id"], index))
    for _, old_id, index in sorted(nearby):
        if index not in assigned and old_id not in used_old:
            assigned[index] = old_id
            used_old.add(old_id)
    reserved = set(reserved)
    for index, cluster in enumerate(clusters):
        if index in assigned:
            cluster["id"] = assigned[index]
            continue
        lat, lon = cluster["centroid"]
        lat, lon = (0 if abs(v) < 0.00005 else v for v in (lat, lon))
        base = f"dest_{lat:.4f}_{lon:.4f}"
        candidate = base
        suffix = hashlib.sha256(json.dumps(sorted(cluster["members"])).encode()).hexdigest()[:8]
        collision = 1
        while candidate in reserved:
            candidate = f"{base}_{suffix}" + (f"_{collision}" if collision > 1 else "")
            collision += 1
        cluster["id"] = candidate
        reserved.add(candidate)
    return clusters


def backfill_destination_endpoints(db, revision, stop):
    missing = db.execute("""SELECT id,start_time FROM activities
        WHERE id NOT IN (SELECT activity_id FROM destination_endpoint_sources) ORDER BY id""").fetchall()
    db.execute("UPDATE destination_state SET phase='endpoints',progress_done=0,progress_total=? WHERE id=1 AND revision=?", (len(missing), revision))
    db.commit()
    for done, activity in enumerate(missing, 1):
        if stop.is_set():
            return False
        points = db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (activity["id"],)).fetchall()
        endpoints = extract_trip_endpoints(points, activity["start_time"])
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT revision FROM destination_state WHERE id=1").fetchone()[0] != revision:
            db.rollback()
            return False
        db.execute("DELETE FROM activity_endpoints WHERE activity_id=?", (activity["id"],))
        db.executemany("INSERT INTO activity_endpoints VALUES(?,?,?,?,?)", [(activity["id"], *p) for p in endpoints])
        db.execute("INSERT OR IGNORE INTO destination_endpoint_sources VALUES(?)", (activity["id"],))
        db.execute("UPDATE destination_state SET progress_done=? WHERE id=1 AND revision=?", (done, revision))
        db.commit()
    return True


def destination_worker(stop):
    try:
        with closing(connect()) as db:
            db.execute("UPDATE destination_state SET status='pending' WHERE status='processing'")
            db.commit()
    except Exception:
        LOG.exception("Could not initialize destination processing")
        return
    while not stop.is_set():
        revision = None
        try:
            with closing(connect()) as db:
                state = db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()
                if state["status"] != "pending" or time.time() - state["changed_at"] < 2:
                    stop.wait(1)
                    continue
                revision = state["revision"]
                db.execute("UPDATE destination_state SET status='processing',error=NULL WHERE id=1 AND revision=?", (revision,))
                db.commit()
                if not backfill_destination_endpoints(db, revision, stop):
                    continue
                db.execute("UPDATE destination_state SET phase='clustering' WHERE id=1 AND revision=?", (revision,))
                db.commit()
                db.execute("BEGIN")
                endpoints = [dict(r) for r in db.execute("SELECT * FROM activity_endpoints ORDER BY activity_id,role")]
                previous = [dict(r) for r in db.execute("SELECT * FROM destinations WHERE active=1 ORDER BY id")]
                reserved = {r[0] for r in db.execute("SELECT id FROM destinations")}
                memberships = {}
                for row in db.execute("SELECT * FROM destination_memberships"):
                    memberships.setdefault(row["destination_id"], set()).add((row["activity_id"], row["role"]))
                db.commit()
                clusters = cluster_destination_endpoints(endpoints, state["epsilon_m"], state["min_trips"])
                assign_destination_ids(clusters, previous, memberships, reserved, state["epsilon_m"])
                if stop.is_set():
                    continue
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT revision FROM destination_state WHERE id=1").fetchone()[0] != revision:
                    db.rollback()
                    continue
                db.execute("DELETE FROM destination_memberships")
                db.execute("UPDATE destinations SET active=0")
                for cluster in clusters:
                    lat, lon = cluster["centroid"]
                    db.execute("""INSERT INTO destinations(id,seed_lat,seed_lon,centroid_lat,centroid_lon)
                        VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                        centroid_lat=excluded.centroid_lat,centroid_lon=excluded.centroid_lon,
                        active=1,updated_at=CURRENT_TIMESTAMP""", (cluster["id"], lat, lon, lat, lon))
                    db.executemany("INSERT INTO destination_memberships VALUES(?,?,?)",
                                   [(aid, role, cluster["id"]) for aid, role in sorted(cluster["members"])])
                db.execute("""UPDATE destination_state SET status='completed',phase='completed',error=NULL,
                    published_revision=?,progress_done=progress_total WHERE id=1""", (revision,))
                db.commit()
        except Exception:
            LOG.exception("Destination clustering failed")
            if revision is not None:
                with closing(connect()) as db:
                    db.execute("UPDATE destination_state SET status='failed',error='Destination processing failed. Retry clustering.' WHERE id=1 AND revision=?", (revision,))
                    db.commit()
            stop.wait(1)


def destination_summary(db, sport="all", destination_id=None):
    conditions, params = ["d.active=1"], []
    if sport != "all":
        conditions.append("a.sport=?")
        params.append(sport)
    if destination_id is not None:
        conditions.append("d.id=?")
        params.append(destination_id)
    return [dict(row) for row in db.execute(f"""SELECT d.id,d.name,d.centroid_lat,d.centroid_lon,
        COUNT(DISTINCT m.activity_id) AS trip_count,
        SUM(m.role='start') AS departure_count,SUM(m.role='end') AS arrival_count,
        MAX(COALESCE(e.recorded_at,a.start_time)) AS latest_visit
        FROM destinations d JOIN destination_memberships m ON m.destination_id=d.id
        JOIN activity_endpoints e ON e.activity_id=m.activity_id AND e.role=m.role
        JOIN activities a ON a.id=m.activity_id WHERE {' AND '.join(conditions)}
        GROUP BY d.id ORDER BY trip_count DESC,arrival_count DESC,d.id""", params)]


def activity_medals(db):
    """Podium results for the overview, calculated in one query for all activities."""
    medals = {}
    rows = db.execute("""WITH best AS (
        SELECT t.segment_id,t.activity_id,MIN(t.duration_s) AS best_time
        FROM segment_traversals t JOIN segments s ON s.id=t.segment_id
        WHERE s.status='completed' AND t.duration_s IS NOT NULL
        GROUP BY t.segment_id,t.activity_id
      ), ranked AS (
        SELECT *,RANK() OVER (PARTITION BY segment_id ORDER BY ROUND(best_time,3)) AS rank FROM best
      ) SELECT r.activity_id,r.segment_id,r.rank,s.name FROM ranked r
        JOIN segments s ON s.id=r.segment_id WHERE r.rank<=3 ORDER BY r.rank,s.name,s.id""")
    for row in rows:
        medals.setdefault(row["activity_id"], []).append({
            "segment_id": row["segment_id"], "name": row["name"], "rank": row["rank"]})
    return medals


def track_segment_results(db, aid):
    """Activity medals compare each activity's best pass, with shared tie ranks."""
    activity = db.execute("SELECT sport FROM activities WHERE id=?", (aid,)).fetchone()
    if activity is None:
        raise SegmentError(404, "Activity not found.")
    result = {"segments": [], "pending_count": 0, "failed_count": 0}
    result["pending_count"] = db.execute("SELECT COUNT(*) FROM segments WHERE sport=? AND status IN ('pending','processing')", (activity["sport"],)).fetchone()[0]
    result["failed_count"] = db.execute("SELECT COUNT(*) FROM segments WHERE sport=? AND status='failed'", (activity["sport"],)).fetchone()[0]
    segments = db.execute("""SELECT DISTINCT s.id,s.name,s.sport,s.radius_m
        FROM segments s JOIN segment_traversals t ON t.segment_id=s.id
        WHERE t.activity_id=? AND s.status='completed' ORDER BY s.name,s.id""", (aid,)).fetchall()
    for row in segments:
        segment = dict(row)
        sid = segment["id"]
        segment["checkpoints"] = [list(p) for p in db.execute(
            "SELECT lat,lon FROM segment_checkpoints WHERE segment_id=? ORDER BY seq", (sid,))]
        ranked = db.execute("""WITH best AS (
            SELECT activity_id,MIN(duration_s) AS best_duration_s
            FROM segment_traversals WHERE segment_id=? AND duration_s IS NOT NULL GROUP BY activity_id
          ), ranked AS (
            SELECT *,RANK() OVER (ORDER BY ROUND(best_duration_s,3)) AS rank,
              COUNT(*) OVER () AS ranked_activity_count FROM best
          ) SELECT * FROM ranked WHERE activity_id=?""", (sid, aid)).fetchone()
        segment.update(rank=ranked["rank"] if ranked else None,
                       best_duration_s=ranked["best_duration_s"] if ranked else None,
                       ranked_activity_count=ranked["ranked_activity_count"] if ranked else 0)
        segment["traversals"] = []
        for traversal in db.execute("SELECT * FROM segment_traversals WHERE segment_id=? AND activity_id=? ORDER BY ordinal", (sid, aid)).fetchall():
            item = dict(traversal)
            item["points"] = traversal_geometry(db, traversal)
            segment["traversals"].append(item)
        result["segments"].append(segment)
    return result


ROUTE_UPLOAD_LIMIT = 25 * 1024 * 1024
ROUTE_POINT_LIMIT = 200_000


def route_storage_dir():
    return DB_PATH.parent / "routes"


def parse_planned_gpx(content, filename):
    from defusedxml import ElementTree as safe_xml
    from defusedxml.common import DefusedXmlException

    try:
        root = safe_xml.fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise SegmentError(400, "The upload must be a valid GPX file without DTDs or entities.")
    if root.tag.rsplit("}", 1)[-1] != "gpx" or root.get("version", "1.1") not in {"1.0", "1.1"}:
        raise SegmentError(400, "Only GPX 1.0 and 1.1 files are supported.")

    def children(element, tag):
        return [child for child in element if child.tag.rsplit("}", 1)[-1] == tag]

    def text(element, tag):
        matches = children(element, tag)
        return (matches[0].text or "").strip() if matches else ""

    count = 0

    def point(element):
        nonlocal count
        count += 1
        if count > ROUTE_POINT_LIMIT:
            raise SegmentError(413, "A route can contain at most 200,000 points, including waypoints.")
        try:
            lat, lon = float(element.get("lat")), float(element.get("lon"))
            if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError()
        except (TypeError, ValueError):
            raise SegmentError(400, "Every route point must have valid latitude and longitude coordinates.")
        elevation = temperature_value(text(element, "ele"))
        if elevation is not None and abs(elevation) > 1_000_000:
            raise SegmentError(400, "Elevation values must be realistic finite metre values.")
        return (lat, lon, elevation)

    parts, waypoints, names, types = [], [], [text(root, "name")], []
    metadata = children(root, "metadata")
    if metadata:
        names.append(text(metadata[0], "name"))
    for element in root:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "trk":
            names.append(text(element, "name"))
            types.append(text(element, "type"))
            for segment in children(element, "trkseg"):
                points = [point(p) for p in children(segment, "trkpt")]
                if points:
                    parts.append(points)
        elif tag == "rte":
            names.append(text(element, "name"))
            types.append(text(element, "type"))
            points = [point(p) for p in children(element, "rtept")]
            if points:
                parts.append(points)
        elif tag == "wpt":
            lat, lon, _ = point(element)
            waypoints.append((lat, lon, text(element, "name")[:200], text(element, "desc")[:2000]))
    if not any(len(part) >= 2 for part in parts):
        raise SegmentError(400, "The GPX must contain a track or route with at least two points; waypoints alone are not a route.")
    distance, ascent, elevation_pairs = 0.0, 0.0, 0
    for part in parts:
        for a, b in zip(part, part[1:]):
            distance += distance_between(a, b)
            if a[2] is not None and b[2] is not None:
                elevation_pairs += 1
                ascent += max(0, b[2] - a[2])
    points = sum(len(part) for part in parts)
    covered = sum(p[2] is not None for part in parts for p in part)
    return {"name": next((name for name in names if name), Path(filename).stem or "Planned route")[:120],
            "sport": next((value for value in types if value and not value.isdigit()), "generic")[:80],
            "parts": parts, "waypoints": waypoints, "distance_m": distance,
            "ascent_m": ascent if elevation_pairs else None, "elevation_coverage": covered / points,
            "point_count": points}


def collection_detail(db, cid):
    row = db.execute("""SELECT c.*,COUNT(m.route_id) AS route_count,COALESCE(SUM(r.distance_m),0) AS distance_m
        FROM route_collections c LEFT JOIN route_collection_members m ON m.collection_id=c.id
        LEFT JOIN planned_routes r ON r.id=m.route_id WHERE c.id=? GROUP BY c.id""", (cid,)).fetchone()
    if row is None:
        raise SegmentError(404, "Collection not found.")
    return dict(row)


def route_memberships(db):
    result = {}
    for row in db.execute("""SELECT m.route_id,c.id,c.name,m.position FROM route_collection_members m
        JOIN route_collections c ON c.id=m.collection_id ORDER BY c.name,c.id"""):
        result.setdefault(row["route_id"], []).append({"id": row["id"], "name": row["name"], "position": row["position"]})
    return result


def route_list(db, cid=None):
    memberships = route_memberships(db)
    columns = "r.id,r.name,r.sport,r.distance_m,r.ascent_m,r.elevation_coverage,r.point_count,r.revision,r.created_at,r.updated_at,r.original_filename"
    if cid:
        collection_detail(db, cid)
        rows = db.execute(f"""SELECT {columns},m.position FROM planned_routes r
            JOIN route_collection_members m ON m.route_id=r.id WHERE m.collection_id=? ORDER BY m.position""", (cid,))
    else:
        rows = db.execute(f"SELECT {columns} FROM planned_routes r ORDER BY r.created_at DESC,r.rowid DESC")
    result = []
    for row in rows:
        item = dict(row)
        item["collections"] = memberships.get(row["id"], [])
        result.append(item)
    return result


def planned_route_detail(db, rid):
    row = db.execute("SELECT * FROM planned_routes WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise SegmentError(404, "Route not found.")
    result = dict(row)
    result.pop("stored_filename")
    result.pop("source_hash")
    result["collections"] = route_memberships(db).get(rid, [])
    return result


def collection_order(db, cid, ids):
    # Move positions out of the positive range before assigning their new values.
    db.execute("UPDATE route_collection_members SET position=-position WHERE collection_id=?", (cid,))
    db.executemany("UPDATE route_collection_members SET position=? WHERE collection_id=? AND route_id=?",
                   [(index, cid, rid) for index, rid in enumerate(ids, 1)])
    db.execute("UPDATE route_collections SET revision=revision+1 WHERE id=?", (cid,))


def attach_route(db, cid, rid):
    collection_detail(db, cid)
    if db.execute("SELECT 1 FROM route_collection_members WHERE collection_id=? AND route_id=?", (cid, rid)).fetchone():
        return False
    position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM route_collection_members WHERE collection_id=?", (cid,)).fetchone()[0]
    db.execute("INSERT INTO route_collection_members VALUES(?,?,?)", (cid, rid, position))
    db.execute("UPDATE route_collections SET revision=revision+1 WHERE id=?", (cid,))
    return True


def store_planned_route(db, content, filename, cid=None):
    if not filename.lower().endswith(".gpx"):
        raise SegmentError(400, "Choose a .gpx file.")
    parsed = parse_planned_gpx(content, filename)
    digest = hashlib.sha256(content).hexdigest()
    created_path = None
    db.execute("BEGIN IMMEDIATE")
    try:
        if cid:
            collection_detail(db, cid)
        existing = db.execute("SELECT id FROM planned_routes WHERE source_hash=?", (digest,)).fetchone()
        if existing:
            rid, duplicate = existing["id"], True
        else:
            rid, duplicate = str(uuid.uuid4()), False
            folder = route_storage_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (rid + ".gpx")
            with path.open("xb") as stream:
                created_path = path
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            db.execute("""INSERT INTO planned_routes(id,source_hash,stored_filename,original_filename,name,sport,
                distance_m,ascent_m,elevation_coverage,point_count) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (rid, digest, path.name, filename, parsed["name"], parsed["sport"], parsed["distance_m"],
                 parsed["ascent_m"], parsed["elevation_coverage"], parsed["point_count"]))
            db.executemany("INSERT INTO planned_route_points VALUES(?,?,?,?,?,?)",
                           [(rid, part_index, seq, *point) for part_index, part in enumerate(parsed["parts"]) for seq, point in enumerate(part)])
            db.executemany("INSERT INTO planned_route_waypoints VALUES(?,?,?,?,?,?)",
                           [(rid, seq, *point) for seq, point in enumerate(parsed["waypoints"])])
        attached = attach_route(db, cid, rid) if cid else False
        result = {"route": planned_route_detail(db, rid), "duplicate": duplicate, "attached": attached}
        db.commit()
        return result
    except Exception:
        db.rollback()
        if created_path is not None:
            created_path.unlink(missing_ok=True)
        raise


def planned_route_geometry(db, rid, preview=False):
    route = planned_route_detail(db, rid)
    stride = max(1, math.ceil(route["point_count"] / 20_000)) if preview else 1
    parts = []
    rows = db.execute("SELECT * FROM planned_route_points WHERE route_id=? ORDER BY part,seq", (rid,))
    for _, group in groupby(rows, key=lambda row: row["part"]):
        points = [[row["lat"], row["lon"]] for row in group]
        sampled = points[::stride]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        parts.append(sampled)
    waypoints = [dict(row) for row in db.execute("SELECT seq,lat,lon,name,description FROM planned_route_waypoints WHERE route_id=? ORDER BY seq", (rid,))]
    return {"id": rid, "parts": parts, "waypoints": waypoints, "sampled": stride > 1}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def end_headers(self):
        # UI assets must follow the running image, including after container rebuilds.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if urlsplit(self.path).path.startswith(("/api/routes", "/api/route-collections")):
            return self.routes_request("GET")
        if urlsplit(self.path).path.startswith("/api/destinations"):
            return self.destination_request("GET")
        if urlsplit(self.path).path.startswith("/api/segments"):
            return self.segment_request("GET")
        if self.path.startswith("/api/activities"):
            with closing(connect()) as db:
                db.execute("BEGIN")
                medals = activity_medals(db)
                rows = [dict(r) for r in db.execute("SELECT id,start_time,sport,duration_s,distance_m, EXISTS(SELECT 1 FROM track_points p WHERE p.activity_id=activities.id) AS has_track FROM activities ORDER BY start_time DESC")]
                for row in rows:
                    row["medals"] = medals.get(row["id"], [])
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
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) < 3:
                return self.send_json({"error": "Activity not found."}, 404)
            aid = unquote(parts[2])
            try:
                with closing(connect()) as db:
                    if len(parts) == 4 and parts[3] == "temperature":
                        availability = ensure_track_temperature(db, aid)
                        db.execute("BEGIN")
                        result = track_temperature_data(db.execute(
                            "SELECT lat,lon,timestamp,temperature_c FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)))
                        result["availability"] = availability["status"]
                        result["message"] = availability.get("message")
                        return self.send_json(result)
                    db.execute("BEGIN")
                    if len(parts) == 4 and parts[3] == "segments":
                        return self.send_json(track_segment_results(db, aid))
                    if len(parts) == 4 and parts[3] == "speed":
                        if not db.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone():
                            raise SegmentError(404, "Activity not found.")
                        return self.send_json(track_speed_data(db.execute(
                            "SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,))))
                    if len(parts) != 3:
                        raise SegmentError(404, "API endpoint not found.")
                    segments = display_segments(db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)))
                    return self.send_json(segments)
            except SegmentError as exc:
                return self.send_json({"error": exc.message}, exc.status)
            except Exception:
                LOG.exception("Could not load track details")
                return self.send_json({"error": "Track details could not be loaded."}, 500)
        if self.path == "/" or self.path.startswith("/?"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        self.mutation_request("POST")

    def do_PUT(self):
        self.mutation_request("PUT")

    def do_DELETE(self):
        self.mutation_request("DELETE")

    def mutation_request(self, method):
        if urlsplit(self.path).path.startswith(("/api/routes", "/api/route-collections")):
            return self.routes_request(method)
        if urlsplit(self.path).path.startswith("/api/destinations"):
            return self.destination_request(method)
        return self.segment_request(method)

    def require_same_origin(self):
        host = self.headers.get("Host", "").lower()
        configured_hosts = os.environ.get("GARMIN_ALLOWED_HOSTS", "")
        allowed = {value.strip().lower() for value in configured_hosts.split(",") if value.strip()}
        if not allowed:
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if (host not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site" or
            (origin is not None and origin != f"http://{host}")):
            raise SegmentError(403, "Only same-origin requests are allowed.")

    def mutation_payload(self, maximum=65536):
        self.require_same_origin()
        if self.headers.get_content_type() != "application/json":
            raise SegmentError(415, "Send an application/json request.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= maximum:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise SegmentError(400, f"Invalid JSON request (maximum {maximum // 1024} KB).")
        if not isinstance(payload, dict):
            raise SegmentError(400, "Expected a JSON object.")
        return payload

    def send_download(self, data, filename, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def routes_request(self, method):
        try:
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) < 2 or parts[0] != "api" or parts[1] not in {"routes", "route-collections"}:
                raise SegmentError(404, "API endpoint not found.")
            query = parse_qs(parsed.query)
            upload = parts == ["api", "routes", "upload"] and method == "POST"
            payload = {}
            if upload:
                self.require_same_origin()
                if self.headers.get_content_type() != "application/gpx+xml":
                    raise SegmentError(415, "Upload the GPX as application/gpx+xml.")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise SegmentError(400, "Invalid upload size.")
                if not 0 < length <= ROUTE_UPLOAD_LIMIT:
                    raise SegmentError(413, "Choose a nonempty GPX file no larger than 25 MiB.")
                self.connection.settimeout(60)
                content = self.rfile.read(length)
                if len(content) != length:
                    raise SegmentError(400, "The GPX upload was incomplete.")
                filename = query.get("filename", ["route.gpx"])[0].replace(chr(92), "/").rsplit("/", 1)[-1]
                if len(filename) > 255:
                    raise SegmentError(400, "The filename must have at most 255 characters.")
            elif method != "GET":
                payload = self.mutation_payload(maximum=131072)
            with closing(connect()) as db:
                if upload:
                    result = store_planned_route(db, content, filename, query.get("collection_id", [None])[0])
                    return self.send_json(result, 200 if result["duplicate"] else 201)
                if method == "GET":
                    db.execute("BEGIN")
                if parts[1] == "route-collections":
                    if len(parts) == 2:
                        if method == "GET":
                            ids = [r[0] for r in db.execute("SELECT id FROM route_collections ORDER BY name,id")]
                            return self.send_json([collection_detail(db, cid) for cid in ids])
                        if method != "POST":
                            raise SegmentError(405, "Method not allowed.")
                        name = payload.get("name")
                        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                            raise SegmentError(400, "Enter a collection name of 1–120 characters.")
                        cid = str(uuid.uuid4())
                        db.execute("INSERT INTO route_collections(id,name) VALUES(?,?)", (cid, name.strip()))
                        db.commit()
                        return self.send_json(collection_detail(db, cid), 201)
                    cid = unquote(parts[2])
                    if method != "GET":
                        db.execute("BEGIN IMMEDIATE")
                    collection = collection_detail(db, cid)
                    if method != "GET" and payload.get("revision") != collection["revision"]:
                        raise SegmentError(409, "This collection changed. Refresh it before trying again.")
                    if len(parts) == 3:
                        if method == "GET":
                            return self.send_json(collection)
                        if method == "PUT":
                            name = payload.get("name")
                            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                                raise SegmentError(400, "Enter a collection name of 1–120 characters.")
                            db.execute("UPDATE route_collections SET name=?,revision=revision+1 WHERE id=?", (name.strip(), cid))
                            db.commit()
                            return self.send_json(collection_detail(db, cid))
                        if method == "DELETE":
                            db.execute("DELETE FROM route_collections WHERE id=?", (cid,))
                            db.commit()
                            return self.send_json({"deleted": True})
                    elif parts[3:] == ["order"] and method == "PUT":
                        ids = payload.get("route_ids")
                        if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids):
                            raise SegmentError(400, "Provide the ordered route IDs.")
                        current = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                        if len(ids) != len(current) or set(ids) != set(current):
                            raise SegmentError(409, "The collection membership changed. Refresh it and reorder again.")
                        collection_order(db, cid, ids)
                        db.commit()
                        return self.send_json(collection_detail(db, cid))
                    elif parts[3:] == ["members"] and method == "POST":
                        rid = payload.get("route_id")
                        if not isinstance(rid, str):
                            raise SegmentError(400, "Choose a route to add.")
                        planned_route_detail(db, rid)
                        attach_route(db, cid, rid)
                        db.commit()
                        return self.send_json(collection_detail(db, cid))
                    elif len(parts) == 5 and parts[3] == "members" and method == "DELETE":
                        rid = unquote(parts[4])
                        db.execute("DELETE FROM route_collection_members WHERE collection_id=? AND route_id=?", (cid, rid))
                        ids = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                        collection_order(db, cid, ids)
                        db.commit()
                        return self.send_json(collection_detail(db, cid))
                    raise SegmentError(405, "Method not allowed.")
                if len(parts) == 2:
                    if method != "GET":
                        raise SegmentError(405, "Use the GPX upload endpoint to create routes.")
                    cid = query.get("collection_id", [None])[0]
                    return self.send_json({"routes": route_list(db, cid), "collection": collection_detail(db, cid) if cid else None})
                rid = unquote(parts[2])
                if method != "GET":
                    db.execute("BEGIN IMMEDIATE")
                route = planned_route_detail(db, rid)
                if len(parts) == 3:
                    if method == "GET":
                        return self.send_json(route)
                    if payload.get("revision") != route["revision"]:
                        raise SegmentError(409, "This route changed. Reload it before saving.")
                    if method == "PUT":
                        name, sport, notes = payload.get("name"), payload.get("sport"), payload.get("notes")
                        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                            raise SegmentError(400, "Enter a route name of 1–120 characters.")
                        if not isinstance(sport, str) or not 1 <= len(sport.strip()) <= 80:
                            raise SegmentError(400, "Enter a sport of 1–80 characters.")
                        if not isinstance(notes, str) or len(notes) > 20000:
                            raise SegmentError(400, "Notes must contain at most 20,000 characters.")
                        db.execute("""UPDATE planned_routes SET name=?,sport=?,notes=?,revision=revision+1,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?""", (name.strip(), sport.strip(), notes, rid))
                        db.commit()
                        return self.send_json(planned_route_detail(db, rid))
                    if method == "DELETE":
                        filename = db.execute("SELECT stored_filename FROM planned_routes WHERE id=?", (rid,)).fetchone()[0]
                        collections = [r[0] for r in db.execute("SELECT collection_id FROM route_collection_members WHERE route_id=?", (rid,))]
                        db.execute("DELETE FROM planned_routes WHERE id=?", (rid,))
                        for cid in collections:
                            ids = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                            collection_order(db, cid, ids)
                        db.commit()
                        path = (route_storage_dir() / filename).resolve()
                        if path.is_relative_to(route_storage_dir().resolve()):
                            try:
                                path.unlink(missing_ok=True)
                            except OSError:
                                LOG.warning("Could not remove orphaned route file %s", filename)
                        return self.send_json({"deleted": True})
                if method == "GET" and parts[3:] == ["geometry"]:
                    return self.send_json(planned_route_geometry(db, rid, query.get("preview") == ["1"]))
                if method == "GET" and parts[3:] == ["download"]:
                    filename = db.execute("SELECT stored_filename FROM planned_routes WHERE id=?", (rid,)).fetchone()[0]
                    path = (route_storage_dir() / filename).resolve()
                    if not path.is_relative_to(route_storage_dir().resolve()) or not path.is_file():
                        raise SegmentError(404, "The original GPX file is unavailable.")
                    return self.send_download(path.read_bytes(), route["original_filename"], "application/gpx+xml")
                if method == "GET" and parts[3:] == ["notes"]:
                    return self.send_download((route["name"] + "\n\n" + route["notes"]).encode("utf-8"),
                                              route["name"] + "-notes.txt", "text/plain; charset=utf-8")
                raise SegmentError(405, "Method not allowed.")
        except SegmentError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception:
            LOG.exception("Routes API request failed")
            self.send_json({"error": "The route request could not be completed. Please retry."}, 500)

    def destination_request(self, method):
        try:
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if parts[:2] != ["api", "destinations"]:
                raise SegmentError(404, "API endpoint not found.")
            payload = self.mutation_payload() if method != "GET" else {}
            query = parse_qs(parsed.query)
            sport = query.get("sport", ["all"])[0]
            with closing(connect()) as db:
                if method == "GET":
                    db.execute("BEGIN")
                if len(parts) == 2 and method == "GET":
                    state = dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone())
                    filter_sql, params = ("", ()) if sport == "all" else (" AND a.sport=?", (sport,))
                    noise = db.execute(f"""SELECT COUNT(*) FROM activity_endpoints e
                        JOIN activities a ON a.id=e.activity_id LEFT JOIN destination_memberships m
                        ON m.activity_id=e.activity_id AND m.role=e.role WHERE m.destination_id IS NULL {filter_sql}""", params).fetchone()[0]
                    excluded = db.execute(f"""SELECT COUNT(*) FROM activities a
                        JOIN destination_endpoint_sources s ON s.activity_id=a.id
                        WHERE NOT EXISTS(SELECT 1 FROM activity_endpoints e WHERE e.activity_id=a.id) {filter_sql}""", params).fetchone()[0]
                    return self.send_json({"destinations": destination_summary(db, sport), "processing": state,
                                           "unassigned_endpoint_count": noise, "excluded_activity_count": excluded})
                if parts[2:] == ["settings"] and method == "GET":
                    return self.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()))
                if parts[2:] == ["settings"] and method == "PUT":
                    epsilon, minimum = payload.get("epsilon_m"), payload.get("min_trips")
                    if isinstance(epsilon, bool) or not isinstance(epsilon, (float, int)) or not math.isfinite(epsilon) or not 10 <= epsilon <= 1000:
                        raise SegmentError(400, "Neighborhood distance must be between 10 and 1000 metres.")
                    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 2 <= minimum <= 100:
                        raise SegmentError(400, "Minimum distinct trips must be an integer from 2 to 100.")
                    db.execute("""UPDATE destination_state SET epsilon_m=?,min_trips=?,revision=revision+1,
                        status='pending',phase='endpoints',error=NULL,progress_done=0,progress_total=0,changed_at=?
                        WHERE id=1""", (epsilon, minimum, time.time()))
                    db.commit()
                    return self.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()), 202)
                if parts[2:] == ["reprocess"] and method == "POST":
                    db.execute("""UPDATE destination_state SET revision=revision+1,status='pending',phase='endpoints',
                        error=NULL,progress_done=0,progress_total=0,changed_at=? WHERE id=1""", (time.time(),))
                    db.commit()
                    return self.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()), 202)
                if len(parts) < 3 or parts[2] in {"settings", "reprocess"}:
                    raise SegmentError(405, "Method not allowed.")
                did = unquote(parts[2])
                destination = db.execute("SELECT * FROM destinations WHERE id=?", (did,)).fetchone()
                if destination is None:
                    raise SegmentError(404, "Destination not found.")
                if not destination["active"]:
                    raise SegmentError(409, "This destination is no longer active. Refresh the destination list.")
                if len(parts) == 3 and method == "PUT":
                    name = payload.get("name")
                    if name is not None and (not isinstance(name, str) or len(name.strip()) > 120):
                        raise SegmentError(400, "A destination name must have at most 120 characters.")
                    db.execute("UPDATE destinations SET name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (name.strip() or None if name is not None else None, did))
                    db.commit()
                    return self.send_json(dict(db.execute("SELECT * FROM destinations WHERE id=?", (did,)).fetchone()))
                if method != "GET":
                    raise SegmentError(405, "Method not allowed.")
                summaries = destination_summary(db, sport, did)
                if not summaries:
                    raise SegmentError(404, "No trips for this destination and sport.")
                filter_sql, params = ("", (did,)) if sport == "all" else (" AND a.sport=?", (did, sport))
                if len(parts) == 3:
                    endpoints = [dict(r) for r in db.execute(f"""SELECT e.* FROM activity_endpoints e
                        JOIN destination_memberships m ON m.activity_id=e.activity_id AND m.role=e.role
                        JOIN activities a ON a.id=e.activity_id WHERE m.destination_id=? {filter_sql}
                        ORDER BY e.activity_id,e.role""", params)]
                    return self.send_json({"destination": summaries[0], "endpoints": endpoints})
                if parts[3:] == ["activities"]:
                    try:
                        offset = max(0, int(query.get("offset", ["0"])[0]))
                        limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
                    except ValueError:
                        raise SegmentError(400, "Invalid pagination.")
                    items = [dict(r) for r in db.execute(f"""SELECT a.id,a.start_time,a.sport,a.distance_m,
                        a.duration_s,MAX(m.role='start') AS departed,MAX(m.role='end') AS arrived
                        FROM destination_memberships m JOIN activities a ON a.id=m.activity_id
                        WHERE m.destination_id=? {filter_sql} GROUP BY a.id
                        ORDER BY a.start_time DESC,a.id LIMIT ? OFFSET ?""", (*params, limit, offset))]
                    return self.send_json({"items": items, "total": summaries[0]["trip_count"],
                                           "offset": offset, "limit": limit})
                raise SegmentError(404, "API endpoint not found.")
        except SegmentError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception:
            LOG.exception("Destination API request failed")
            self.send_json({"error": "Destination request failed. Please retry."}, 500)

    def segment_request(self, method):
        try:
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if parts[:2] != ["api", "segments"]:
                raise SegmentError(404, "API endpoint not found.")
            payload = self.mutation_payload() if method != "GET" else {}
            with closing(connect()) as db:
                if len(parts) == 2:
                    if method == "GET":
                        return self.send_json([segment_detail(db, r[0]) for r in db.execute("SELECT id FROM segments ORDER BY name,id").fetchall()])
                    if method != "POST":
                        raise SegmentError(405, "Method not allowed.")
                    name, sport, radius, points = validate_segment(db, payload)
                    with db:
                        sid = db.execute("INSERT INTO segments(name,sport,radius_m) VALUES(?,?,?)", (name, sport, radius)).lastrowid
                        db.executemany("INSERT INTO segment_checkpoints VALUES(?,?,?,?)",
                                       [(sid, index, *point) for index, point in enumerate(points)])
                    return self.send_json(segment_detail(db, sid), 201)
                try:
                    sid = int(parts[2])
                except (ValueError, IndexError):
                    raise SegmentError(404, "Segment not found.")
                segment = segment_detail(db, sid)
                if len(parts) == 3:
                    if method == "GET":
                        return self.send_json(segment)
                    if method not in {"PUT", "DELETE"}:
                        raise SegmentError(405, "Method not allowed.")
                    db.execute("BEGIN IMMEDIATE")
                    segment = segment_detail(db, sid)
                    if payload.get("revision") != segment["revision"]:
                        raise SegmentError(409, "This segment changed. Cancel editing and reopen it.")
                    if method == "DELETE":
                        db.execute("DELETE FROM segments WHERE id=?", (sid,))
                        db.commit()
                        return self.send_json({"deleted": True})
                    name, sport, radius, points = validate_segment(db, payload)
                    changed = sport != segment["sport"] or radius != segment["radius_m"] or points != segment["checkpoints"]
                    # Any edit increments the revision to detect concurrent editors.
                    status = "pending" if changed or segment["status"] == "failed" else segment["status"]
                    if status == "processing":
                        status = "pending"
                    db.execute("""UPDATE segments SET name=?,sport=?,radius_m=?,revision=revision+1,status=?,
                        error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (name, sport, radius, status, sid))
                    db.execute("DELETE FROM segment_checkpoints WHERE segment_id=?", (sid,))
                    db.executemany("INSERT INTO segment_checkpoints VALUES(?,?,?,?)",
                                   [(sid, index, *point) for index, point in enumerate(points)])
                    if status == "pending":
                        db.execute("DELETE FROM segment_traversals WHERE segment_id=?", (sid,))
                        db.execute("UPDATE segments SET progress_done=0,progress_total=0 WHERE id=?", (sid,))
                    db.commit()
                    return self.send_json(segment_detail(db, sid))
                if method != "GET":
                    raise SegmentError(405, "Method not allowed.")
                if segment["status"] != "completed":
                    raise SegmentError(409, "Segment results are not ready yet.")
                if parts[3:] == ["routes"]:
                    routes = []
                    for activity in db.execute("SELECT DISTINCT activity_id FROM segment_traversals WHERE segment_id=?", (sid,)).fetchall():
                        records = db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (activity[0],)).fetchall()
                        routes.append({"activity_id": activity[0], "segments": display_segments(records, max(1, len(records) // 500))})
                    return self.send_json(routes)
                if parts[3:] == ["traversals"]:
                    query = parse_qs(parsed.query)
                    try:
                        offset = max(0, int(query.get("offset", ["0"])[0]))
                        limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
                    except ValueError:
                        raise SegmentError(400, "Invalid pagination.")
                    rows = db.execute("""SELECT t.*,a.start_time AS activity_start_time,a.sport FROM segment_traversals t
                        JOIN activities a ON a.id=t.activity_id WHERE t.segment_id=?
                        ORDER BY t.duration_s IS NULL,t.duration_s,a.start_time DESC,t.id LIMIT ? OFFSET ?""", (sid, limit, offset))
                    return self.send_json({"items": [dict(r) for r in rows], "total": segment["traversal_count"], "offset": offset, "limit": limit})
                if len(parts) == 6 and parts[3] == "traversals" and parts[5] == "geometry":
                    try:
                        tid = int(parts[4])
                    except ValueError:
                        raise SegmentError(404, "Traversal not found.")
                    traversal = db.execute("SELECT * FROM segment_traversals WHERE id=? AND segment_id=?", (tid, sid)).fetchone()
                    if traversal is None:
                        raise SegmentError(404, "Traversal not found.")
                    return self.send_json({"activity_id": traversal["activity_id"], "points": traversal_geometry(db, traversal)})
                raise SegmentError(404, "API endpoint not found.")
        except SegmentError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception:
            LOG.exception("Segment API request failed")
            self.send_json({"error": "The request could not be completed. Please retry."}, 500)

    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global DB_PATH, ACTIVITIES
    load_dotenv()
    configured_db = Path(os.environ.get("GARMIN_DB_PATH") or ROOT / "activities.sqlite3").expanduser()
    DB_PATH = configured_db if configured_db.is_absolute() else ROOT / configured_db
    configured_activities = Path(os.environ.get("GARMIN_ACTIVITIES_PATH") or ROOT / "storage/activities").expanduser()
    ACTIVITIES = configured_activities if configured_activities.is_absolute() else ROOT / configured_activities
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import", help="Import FIT/GPX files from storage/activities/ (or GARMIN_ACTIVITIES_PATH)")
    sub.add_parser("fetch", help="Fetch new Garmin activities (requires GARMIN_API_KEY)")
    sub.add_parser("export-csv", help="Export session metadata to analysis/activities.csv")
    sub.add_parser("export-map", help="Export a static Folium map to analysis/activities_map.html")
    serve = sub.add_parser("serve", help="Run the local activity map")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 inside Docker)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "import": import_all()
    elif args.command == "fetch": fetch_new()
    elif args.command == "export-csv": export_csv()
    elif args.command == "export-map": export_map()
    else:
        if not DB_PATH.exists(): import_all()
        with closing(connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
        stop = threading.Event()
        worker = threading.Thread(target=segment_worker, args=(stop,), daemon=True, name="segment-matcher")
        destinations_worker = threading.Thread(target=destination_worker, args=(stop,), daemon=True, name="destination-clustering")
        server = ThreadingHTTPServer((args.host, args.port), Handler)

        def terminate(signum, frame):
            raise KeyboardInterrupt

        previous_sigterm = signal.signal(signal.SIGTERM, terminate)
        try:
            worker.start()
            destinations_worker.start()
            print(f"Listening on http://{args.host}:{args.port}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
            if worker.ident is not None:
                worker.join(timeout=25)
            if destinations_worker.ident is not None:
                destinations_worker.join(timeout=3)
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
