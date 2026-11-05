"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from urllib.parse import parse_qs, urlsplit
import logging
import math

from garmin_library.errors import AppError
from garmin_library.route_matching import route_match_geometry

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
                db.execute("UPDATE route_matching_state SET tolerance_m=?,partial_percent=?,full_percent=? WHERE id=1", values)
            db.execute("UPDATE route_matching_state SET revision=revision+1,status='pending',error=NULL,progress_done=0,progress_total=0 WHERE id=1")
            db.commit()
            return handler.send_json(dict(db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone()))
    except AppError as exc:
        handler.send_json({"error": exc.message}, exc.status)
    except Exception:
        LOG.exception("Route matching API failed")
        handler.send_json({"error": "Route matching data could not be loaded."}, 500)
