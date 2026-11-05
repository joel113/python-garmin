"""Recorded activity parsing, import, speed, and temperature."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import logging
import math
import sqlite3
import xml.etree.ElementTree as ET

import fitparse

from garmin_library.errors import AppError
from garmin_library.geometry import distance_between, gps_discontinuity, point_time

LOG = logging.getLogger(__name__)

def activity_id(path: Path) -> str:
    # Garmin-exported filenames commonly end in _<activity-id>.fit.
    stem = path.stem
    return stem.rsplit("_", 1)[-1] if "_" in stem else stem


def activity_files(context):
    return sorted({p for pattern in ("*.fit", "*.FIT", "*.gpx", "*.GPX") for p in context.settings.activities_path.glob(pattern)})


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


def ensure_track_temperature(context, db, aid):
    activity = db.execute("SELECT file_path FROM activities WHERE id=?", (aid,)).fetchone()
    if activity is None:
        raise AppError(404, "Activity not found.")
    stored_path = Path(activity["file_path"])
    # Keep old database references usable after moving the originals. Store
    # portable paths so the same database works on the host and in /data.
    if stored_path.parent in {Path("activities"), Path("storage/activities")}:
        path = (context.settings.activities_path / stored_path.name).resolve()
    else:
        path = (context.settings.root / stored_path).resolve()
    if not path.is_relative_to(context.settings.activities_path.resolve()) or not path.is_file():
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


def import_all(context):
    context.settings.activities_path.mkdir(parents=True, exist_ok=True)
    db = context.connect()
    ok = sum(import_file(db, p) for p in activity_files(context))
    db.close()
    print(f"Imported {ok} FIT files into {context.settings.database_path}")
