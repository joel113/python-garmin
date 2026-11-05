"""Segment definitions, matching, traversals, and medals."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import logging
import math

from garmin_library.errors import AppError
from garmin_library.geometry import iso_time, distance_between, gps_discontinuity, point_time

LOG = logging.getLogger(__name__)

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




def segment_worker(context, stop):
    """Publish cached matches only if the processed revision is still current."""
    try:
        with closing(context.connect()) as db:
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
            with closing(context.connect()) as db:
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
                with closing(context.connect()) as db:
                    db.execute("UPDATE segments SET status='failed',error='Matching failed. Edit and save the segment to retry.' WHERE id=? AND revision=?",
                               (sid, revision))
                    db.commit()
            stop.wait(1)


def segment_detail(db, sid):
    row = db.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise AppError(404, "Segment not found.")
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
        raise AppError(400, "Enter a segment name of 1–120 characters.")
    if not isinstance(sport, str) or not db.execute("SELECT 1 FROM activities WHERE sport=? LIMIT 1", (sport,)).fetchone():
        raise AppError(400, "Select an existing sport.")
    radius = payload.get("radius_m", 30)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not math.isfinite(radius) or not 5 <= radius <= 500:
        raise AppError(400, "GPS tolerance must be between 5 and 500 metres.")
    points = payload.get("checkpoints")
    if not isinstance(points, list) or not 1 <= len(points) <= 100:
        raise AppError(400, "Place between 1 and 100 checkpoints.")
    for point in points:
        if (not isinstance(point, list) or len(point) != 2 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point) or
            not -85 <= point[0] <= 85 or not -180 <= point[1] <= 180):
            raise AppError(400, "Checkpoints must contain valid latitude and longitude coordinates (latitude within ±85°).")
    if any(a == b for a, b in zip(points, points[1:])):
        raise AppError(400, "Adjacent checkpoints must be different.")
    return name.strip(), sport, radius, points


def traversal_geometry(db, traversal):
    start, end = traversal["start_pos"], traversal["end_pos"]
    rows = db.execute("SELECT seq,lat,lon FROM track_points WHERE activity_id=? AND seq BETWEEN ? AND ? ORDER BY seq",
                      (traversal["activity_id"], math.floor(start), math.ceil(end))).fetchall()
    by_seq = {r["seq"]: r for r in rows}

    def at(position):
        a, b = by_seq.get(math.floor(position)), by_seq.get(math.ceil(position))
        if a is None or b is None:
            raise AppError(409, "Track data changed; wait for segment matching to finish.")
        fraction = position - math.floor(position)
        return [a["lat"] + fraction * (b["lat"] - a["lat"]), a["lon"] + fraction * (b["lon"] - a["lon"])]

    return [at(start)] + [[r["lat"], r["lon"]] for r in rows if start < r["seq"] < end] + ([at(end)] if end != start else [])


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
        raise AppError(404, "Activity not found.")
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
