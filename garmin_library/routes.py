"""Planned GPX routes, original uploads, and ordered collections."""
from __future__ import annotations

from itertools import groupby
from pathlib import Path
import hashlib
import math
import os
import uuid
import xml.etree.ElementTree as ET

from garmin_library.activities import temperature_value
from garmin_library.errors import AppError
from garmin_library.geometry import distance_between

ROUTE_UPLOAD_LIMIT = 25 * 1024 * 1024
ROUTE_POINT_LIMIT = 200_000

def route_storage_dir(context):
    return context.settings.routes_path


def parse_planned_gpx(content, filename):
    from defusedxml import ElementTree as safe_xml
    from defusedxml.common import DefusedXmlException

    try:
        root = safe_xml.fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise AppError(400, "The upload must be a valid GPX file without DTDs or entities.")
    if root.tag.rsplit("}", 1)[-1] != "gpx" or root.get("version", "1.1") not in {"1.0", "1.1"}:
        raise AppError(400, "Only GPX 1.0 and 1.1 files are supported.")

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
            raise AppError(413, "A route can contain at most 200,000 points, including waypoints.")
        try:
            lat, lon = float(element.get("lat")), float(element.get("lon"))
            if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError()
        except (TypeError, ValueError):
            raise AppError(400, "Every route point must have valid latitude and longitude coordinates.")
        elevation = temperature_value(text(element, "ele"))
        if elevation is not None and abs(elevation) > 1_000_000:
            raise AppError(400, "Elevation values must be realistic finite metre values.")
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
        raise AppError(400, "The GPX must contain a track or route with at least two points; waypoints alone are not a route.")
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
        raise AppError(404, "Collection not found.")
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
        raise AppError(404, "Route not found.")
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


def store_planned_route(context, db, content, filename, cid=None):
    if not filename.lower().endswith(".gpx"):
        raise AppError(400, "Choose a .gpx file.")
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
            folder = route_storage_dir(context)
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
