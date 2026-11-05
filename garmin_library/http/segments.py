"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from urllib.parse import parse_qs, urlsplit
import logging

from garmin_library.errors import AppError
from garmin_library.geometry import display_segments
from garmin_library.segments import segment_detail, traversal_geometry, validate_segment

LOG = logging.getLogger(__name__)


def handle(handler, method):
    context = handler.context
    try:
        parsed = urlsplit(handler.path)
        parts = parsed.path.strip("/").split("/")
        if parts[:2] != ["api", "segments"]:
            raise AppError(404, "API endpoint not found.")
        payload = handler.mutation_payload() if method != "GET" else {}
        with closing(context.connect()) as db:
            if len(parts) == 2:
                if method == "GET":
                    return handler.send_json([segment_detail(db, r[0]) for r in db.execute("SELECT id FROM segments ORDER BY name,id").fetchall()])
                if method != "POST":
                    raise AppError(405, "Method not allowed.")
                name, sport, radius, points = validate_segment(db, payload)
                with db:
                    sid = db.execute("INSERT INTO segments(name,sport,radius_m) VALUES(?,?,?)", (name, sport, radius)).lastrowid
                    db.executemany("INSERT INTO segment_checkpoints VALUES(?,?,?,?)",
                                   [(sid, index, *point) for index, point in enumerate(points)])
                return handler.send_json(segment_detail(db, sid), 201)
            try:
                sid = int(parts[2])
            except (ValueError, IndexError):
                raise AppError(404, "Segment not found.")
            segment = segment_detail(db, sid)
            if len(parts) == 3:
                if method == "GET":
                    return handler.send_json(segment)
                if method not in {"PUT", "DELETE"}:
                    raise AppError(405, "Method not allowed.")
                db.execute("BEGIN IMMEDIATE")
                segment = segment_detail(db, sid)
                if payload.get("revision") != segment["revision"]:
                    raise AppError(409, "This segment changed. Cancel editing and reopen it.")
                if method == "DELETE":
                    db.execute("DELETE FROM segments WHERE id=?", (sid,))
                    db.commit()
                    return handler.send_json({"deleted": True})
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
                return handler.send_json(segment_detail(db, sid))
            if method != "GET":
                raise AppError(405, "Method not allowed.")
            if segment["status"] != "completed":
                raise AppError(409, "Segment results are not ready yet.")
            if parts[3:] == ["routes"]:
                routes = []
                for activity in db.execute("SELECT DISTINCT activity_id FROM segment_traversals WHERE segment_id=?", (sid,)).fetchall():
                    records = db.execute("SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (activity[0],)).fetchall()
                    routes.append({"activity_id": activity[0], "segments": display_segments(records, max(1, len(records) // 500))})
                return handler.send_json(routes)
            if parts[3:] == ["traversals"]:
                query = parse_qs(parsed.query)
                try:
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
                except ValueError:
                    raise AppError(400, "Invalid pagination.")
                rows = db.execute("""SELECT t.*,a.start_time AS activity_start_time,a.sport FROM segment_traversals t
                    JOIN activities a ON a.id=t.activity_id WHERE t.segment_id=?
                    ORDER BY t.duration_s IS NULL,t.duration_s,a.start_time DESC,t.id LIMIT ? OFFSET ?""", (sid, limit, offset))
                return handler.send_json({"items": [dict(r) for r in rows], "total": segment["traversal_count"], "offset": offset, "limit": limit})
            if len(parts) == 6 and parts[3] == "traversals" and parts[5] == "geometry":
                try:
                    tid = int(parts[4])
                except ValueError:
                    raise AppError(404, "Traversal not found.")
                traversal = db.execute("SELECT * FROM segment_traversals WHERE id=? AND segment_id=?", (tid, sid)).fetchone()
                if traversal is None:
                    raise AppError(404, "Traversal not found.")
                return handler.send_json({"activity_id": traversal["activity_id"], "points": traversal_geometry(db, traversal)})
            raise AppError(404, "API endpoint not found.")
    except AppError as exc:
        handler.send_json({"error": exc.message}, exc.status)
    except Exception:
        LOG.exception("Segment API request failed")
        handler.send_json({"error": "The request could not be completed. Please retry."}, 500)
