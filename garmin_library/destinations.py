"""Destination clustering, stable identities, and background processing."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import logging
import math
import time

from garmin_library.geometry import distance_between, gps_discontinuity, point_time
from garmin_library.geometry import iso_time

LOG = logging.getLogger(__name__)

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


def destination_worker(context, stop):
    try:
        with closing(context.connect()) as db:
            db.execute("UPDATE destination_state SET status='pending' WHERE status='processing'")
            db.commit()
    except Exception:
        LOG.exception("Could not initialize destination processing")
        return
    while not stop.is_set():
        revision = None
        try:
            with closing(context.connect()) as db:
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
                with closing(context.connect()) as db:
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
