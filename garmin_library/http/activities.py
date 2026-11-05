"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from itertools import chain, groupby
from urllib.parse import parse_qs, unquote, urlsplit
import logging

from garmin_library.activities import ensure_track_temperature, track_speed_data, track_temperature_data
from garmin_library.errors import AppError
from garmin_library.geometry import display_segments
from garmin_library.route_matching import route_match_results
from garmin_library.segments import activity_medals, track_segment_results

LOG = logging.getLogger(__name__)


def handle(handler):
    context = handler.context
    if handler.path.startswith("/api/activities"):
        with closing(context.connect()) as db:
            db.execute("BEGIN")
            medals = activity_medals(db)
            rows = [dict(r) for r in db.execute("SELECT id,start_time,sport,duration_s,distance_m, EXISTS(SELECT 1 FROM track_points p WHERE p.activity_id=activities.id) AS has_track FROM activities ORDER BY start_time DESC")]
            for row in rows:
                row["medals"] = medals.get(row["id"], [])
        return handler.send_json(rows)
    if handler.path.startswith("/api/heatmap"):
        sport = parse_qs(urlsplit(handler.path).query).get("sport", ["all"])[0]
        with context.connect() as db:
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
        return handler.send_json(routes)
    if handler.path.startswith("/api/track/"):
        parts = urlsplit(handler.path).path.strip("/").split("/")
        if len(parts) < 3:
            return handler.send_json({"error": "Activity not found."}, 404)
        aid = unquote(parts[2])
        try:
            with closing(context.connect()) as db:
                if len(parts) == 4 and parts[3] == "temperature":
                    availability = ensure_track_temperature(context, db, aid)
                    db.execute("BEGIN")
                    result = track_temperature_data(db.execute(
                        "SELECT lat,lon,timestamp,temperature_c FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)))
                    result["availability"] = availability["status"]
                    result["message"] = availability.get("message")
                    return handler.send_json(result)
                db.execute("BEGIN")
                if len(parts) == 4 and parts[3] == "route-matches":
                    return handler.send_json(route_match_results(db, "activity", aid, parse_qs(urlsplit(handler.path).query)))
                if len(parts) == 4 and parts[3] == "segments":
                    return handler.send_json(track_segment_results(db, aid))
                if len(parts) == 4 and parts[3] == "speed":
                    if not db.execute("SELECT 1 FROM activities WHERE id=?", (aid,)).fetchone():
                        raise AppError(404, "Activity not found.")
                    return handler.send_json(track_speed_data(db.execute(
                        "SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,))))
                if len(parts) != 3:
                    raise AppError(404, "API endpoint not found.")
                segments = display_segments(db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,)))
                return handler.send_json(segments)
        except AppError as exc:
            return handler.send_json({"error": exc.message}, exc.status)
        except Exception:
            LOG.exception("Could not load track details")
            return handler.send_json({"error": "Track details could not be loaded."}, 500)
