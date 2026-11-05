"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from urllib.parse import parse_qs, unquote, urlsplit
import logging
import math
import time

from garmin_library.destinations import destination_summary
from garmin_library.errors import AppError

LOG = logging.getLogger(__name__)


def handle(handler, method):
    context = handler.context
    try:
        parsed = urlsplit(handler.path)
        parts = parsed.path.strip("/").split("/")
        if parts[:2] != ["api", "destinations"]:
            raise AppError(404, "API endpoint not found.")
        payload = handler.mutation_payload() if method != "GET" else {}
        query = parse_qs(parsed.query)
        sport = query.get("sport", ["all"])[0]
        with closing(context.connect()) as db:
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
                return handler.send_json({"destinations": destination_summary(db, sport), "processing": state,
                                       "unassigned_endpoint_count": noise, "excluded_activity_count": excluded})
            if parts[2:] == ["settings"] and method == "GET":
                return handler.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()))
            if parts[2:] == ["settings"] and method == "PUT":
                epsilon, minimum = payload.get("epsilon_m"), payload.get("min_trips")
                if isinstance(epsilon, bool) or not isinstance(epsilon, (float, int)) or not math.isfinite(epsilon) or not 10 <= epsilon <= 1000:
                    raise AppError(400, "Neighborhood distance must be between 10 and 1000 metres.")
                if isinstance(minimum, bool) or not isinstance(minimum, int) or not 2 <= minimum <= 100:
                    raise AppError(400, "Minimum distinct trips must be an integer from 2 to 100.")
                db.execute("""UPDATE destination_state SET epsilon_m=?,min_trips=?,revision=revision+1,
                    status='pending',phase='endpoints',error=NULL,progress_done=0,progress_total=0,changed_at=?
                    WHERE id=1""", (epsilon, minimum, time.time()))
                db.commit()
                return handler.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()), 202)
            if parts[2:] == ["reprocess"] and method == "POST":
                db.execute("""UPDATE destination_state SET revision=revision+1,status='pending',phase='endpoints',
                    error=NULL,progress_done=0,progress_total=0,changed_at=? WHERE id=1""", (time.time(),))
                db.commit()
                return handler.send_json(dict(db.execute("SELECT * FROM destination_state WHERE id=1").fetchone()), 202)
            if len(parts) < 3 or parts[2] in {"settings", "reprocess"}:
                raise AppError(405, "Method not allowed.")
            did = unquote(parts[2])
            destination = db.execute("SELECT * FROM destinations WHERE id=?", (did,)).fetchone()
            if destination is None:
                raise AppError(404, "Destination not found.")
            if not destination["active"]:
                raise AppError(409, "This destination is no longer active. Refresh the destination list.")
            if len(parts) == 3 and method == "PUT":
                name = payload.get("name")
                if name is not None and (not isinstance(name, str) or len(name.strip()) > 120):
                    raise AppError(400, "A destination name must have at most 120 characters.")
                db.execute("UPDATE destinations SET name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (name.strip() or None if name is not None else None, did))
                db.commit()
                return handler.send_json(dict(db.execute("SELECT * FROM destinations WHERE id=?", (did,)).fetchone()))
            if method != "GET":
                raise AppError(405, "Method not allowed.")
            summaries = destination_summary(db, sport, did)
            if not summaries:
                raise AppError(404, "No trips for this destination and sport.")
            filter_sql, params = ("", (did,)) if sport == "all" else (" AND a.sport=?", (did, sport))
            if len(parts) == 3:
                endpoints = [dict(r) for r in db.execute(f"""SELECT e.* FROM activity_endpoints e
                    JOIN destination_memberships m ON m.activity_id=e.activity_id AND m.role=e.role
                    JOIN activities a ON a.id=e.activity_id WHERE m.destination_id=? {filter_sql}
                    ORDER BY e.activity_id,e.role""", params)]
                return handler.send_json({"destination": summaries[0], "endpoints": endpoints})
            if parts[3:] == ["activities"]:
                try:
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
                except ValueError:
                    raise AppError(400, "Invalid pagination.")
                items = [dict(r) for r in db.execute(f"""SELECT a.id,a.start_time,a.sport,a.distance_m,
                    a.duration_s,MAX(m.role='start') AS departed,MAX(m.role='end') AS arrived
                    FROM destination_memberships m JOIN activities a ON a.id=m.activity_id
                    WHERE m.destination_id=? {filter_sql} GROUP BY a.id
                    ORDER BY a.start_time DESC,a.id LIMIT ? OFFSET ?""", (*params, limit, offset))]
                return handler.send_json({"items": items, "total": summaries[0]["trip_count"],
                                       "offset": offset, "limit": limit})
            raise AppError(404, "API endpoint not found.")
    except AppError as exc:
        handler.send_json({"error": exc.message}, exc.status)
    except Exception:
        LOG.exception("Destination API request failed")
        handler.send_json({"error": "Destination request failed. Please retry."}, 500)
