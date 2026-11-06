"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from urllib.parse import parse_qs, urlsplit
import logging
import math

from garmin_library.errors import AppError
from garmin_library.route_matching import queue_all_route_matches, route_match_geometry

LOG = logging.getLogger(__name__)


def handle(handler, method):
    context = handler.context
    try:
        parsed = urlsplit(handler.path)
        if method == "GET":
            with closing(context.connect()) as db:
                db.execute("BEGIN")
                if parsed.path == "/api/route-matching":
                    return handler.send_json(dict(db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone()))
                if parsed.path == "/api/route-matching/geometry":
                    query = parse_qs(parsed.query)
                    return handler.send_json(route_match_geometry(db, query.get("route_id", [""])[0], query.get("activity_id", [""])[0]))
            raise AppError(404, "API endpoint not found.")
        payload = handler.mutation_payload()
        if not ((method == "PUT" and parsed.path == "/api/route-matching") or
                (method == "POST" and parsed.path == "/api/route-matching/retry")):
            raise AppError(405, "Method not allowed.")
        with closing(context.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone()
            if payload.get("revision") != state["revision"]:
                raise AppError(409, "Matching settings or input changed. Reload settings and try again.")
            if method == "PUT":
                values = [payload.get(key) for key in ("tolerance_m", "partial_percent", "full_percent")]
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                    raise AppError(400, "Enter finite numeric matching settings.")
                tolerance, partial, full = values
                if not 10 <= tolerance <= 200 or not 0 < partial < full <= 100:
                    raise AppError(400, "Tolerance must be 10–200 m, with 0 < partial < full ≤ 100%.")
                partial_km = payload.get("partial_km", state["partial_km"])
                full_km = payload.get("full_km", state["full_km"])
                for value in (partial_km, full_km):
                    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
                        raise AppError(400, "Kilometre thresholds must be positive finite numbers, or blank to disable.")
                if partial_km is not None and full_km is not None and partial_km >= full_km:
                    raise AppError(400, "Partial kilometres must be below full kilometres when both are enabled.")
                db.execute("""UPDATE route_matching_state SET tolerance_m=?,partial_percent=?,full_percent=?,
                    partial_km=?,full_km=? WHERE id=1""", [*values, partial_km, full_km])
            queue_all_route_matches(db)
            db.execute("""UPDATE route_matching_state SET revision=revision+1,status='pending',error=NULL,
                progress_done=0,progress_total=(SELECT COUNT(*) FROM route_matching_jobs) WHERE id=1""")
            db.commit()
            return handler.send_json(dict(db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone()))
    except AppError as exc:
        handler.send_json({"error": exc.message}, exc.status)
    except Exception:
        LOG.exception("Route matching API failed")
        handler.send_json({"error": "Route matching data could not be loaded."}, 500)
