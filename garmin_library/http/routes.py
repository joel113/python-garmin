"""Feature HTTP endpoints."""
from __future__ import annotations

from contextlib import closing
from urllib.parse import parse_qs, unquote, urlsplit
import logging
import uuid

from garmin_library.errors import AppError
from garmin_library.route_matching import route_match_results
from garmin_library.routes import ROUTE_UPLOAD_LIMIT, attach_route, collection_detail, collection_order, planned_route_detail, planned_route_geometry, route_list, route_storage_dir, store_planned_route

LOG = logging.getLogger(__name__)


def handle(handler, method):
    context = handler.context
    try:
        parsed = urlsplit(handler.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "api" or parts[1] not in {"routes", "route-collections"}:
            raise AppError(404, "API endpoint not found.")
        query = parse_qs(parsed.query)
        upload = parts == ["api", "routes", "upload"] and method == "POST"
        payload = {}
        if upload:
            handler.require_same_origin()
            if handler.headers.get_content_type() != "application/gpx+xml":
                raise AppError(415, "Upload the GPX as application/gpx+xml.")
            try:
                length = int(handler.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError(400, "Invalid upload size.")
            if not 0 < length <= ROUTE_UPLOAD_LIMIT:
                raise AppError(413, "Choose a nonempty GPX file no larger than 25 MiB.")
            handler.connection.settimeout(60)
            content = handler.rfile.read(length)
            if len(content) != length:
                raise AppError(400, "The GPX upload was incomplete.")
            filename = query.get("filename", ["route.gpx"])[0].replace(chr(92), "/").rsplit("/", 1)[-1]
            if len(filename) > 255:
                raise AppError(400, "The filename must have at most 255 characters.")
        elif method != "GET":
            payload = handler.mutation_payload(maximum=131072)
        with closing(context.connect()) as db:
            if upload:
                result = store_planned_route(context, db, content, filename, query.get("collection_id", [None])[0])
                return handler.send_json(result, 200 if result["duplicate"] else 201)
            if method == "GET":
                db.execute("BEGIN")
            if parts[1] == "route-collections":
                if len(parts) == 2:
                    if method == "GET":
                        ids = [r[0] for r in db.execute("SELECT id FROM route_collections ORDER BY name,id")]
                        return handler.send_json([collection_detail(db, cid) for cid in ids])
                    if method != "POST":
                        raise AppError(405, "Method not allowed.")
                    name = payload.get("name")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                        raise AppError(400, "Enter a collection name of 1–120 characters.")
                    cid = str(uuid.uuid4())
                    db.execute("INSERT INTO route_collections(id,name) VALUES(?,?)", (cid, name.strip()))
                    db.commit()
                    return handler.send_json(collection_detail(db, cid), 201)
                cid = unquote(parts[2])
                if method != "GET":
                    db.execute("BEGIN IMMEDIATE")
                collection = collection_detail(db, cid)
                if method != "GET" and payload.get("revision") != collection["revision"]:
                    raise AppError(409, "This collection changed. Refresh it before trying again.")
                if len(parts) == 3:
                    if method == "GET":
                        return handler.send_json(collection)
                    if method == "PUT":
                        name = payload.get("name")
                        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                            raise AppError(400, "Enter a collection name of 1–120 characters.")
                        db.execute("UPDATE route_collections SET name=?,revision=revision+1 WHERE id=?", (name.strip(), cid))
                        db.commit()
                        return handler.send_json(collection_detail(db, cid))
                    if method == "DELETE":
                        db.execute("DELETE FROM route_collections WHERE id=?", (cid,))
                        db.commit()
                        return handler.send_json({"deleted": True})
                elif parts[3:] == ["order"] and method == "PUT":
                    ids = payload.get("route_ids")
                    if not isinstance(ids, list) or any(not isinstance(rid, str) for rid in ids):
                        raise AppError(400, "Provide the ordered route IDs.")
                    current = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                    if len(ids) != len(current) or set(ids) != set(current):
                        raise AppError(409, "The collection membership changed. Refresh it and reorder again.")
                    collection_order(db, cid, ids)
                    db.commit()
                    return handler.send_json(collection_detail(db, cid))
                elif parts[3:] == ["members"] and method == "POST":
                    rid = payload.get("route_id")
                    if not isinstance(rid, str):
                        raise AppError(400, "Choose a route to add.")
                    planned_route_detail(db, rid)
                    attach_route(db, cid, rid)
                    db.commit()
                    return handler.send_json(collection_detail(db, cid))
                elif len(parts) == 5 and parts[3] == "members" and method == "DELETE":
                    rid = unquote(parts[4])
                    db.execute("DELETE FROM route_collection_members WHERE collection_id=? AND route_id=?", (cid, rid))
                    ids = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                    collection_order(db, cid, ids)
                    db.commit()
                    return handler.send_json(collection_detail(db, cid))
                raise AppError(405, "Method not allowed.")
            if len(parts) == 2:
                if method != "GET":
                    raise AppError(405, "Use the GPX upload endpoint to create routes.")
                cid = query.get("collection_id", [None])[0]
                return handler.send_json({"routes": route_list(db, cid), "collection": collection_detail(db, cid) if cid else None})
            rid = unquote(parts[2])
            if method != "GET":
                db.execute("BEGIN IMMEDIATE")
            route = planned_route_detail(db, rid)
            if len(parts) == 3:
                if method == "GET":
                    return handler.send_json(route)
                if payload.get("revision") != route["revision"]:
                    raise AppError(409, "This route changed. Reload it before saving.")
                if method == "PUT":
                    name, sport, notes = payload.get("name"), payload.get("sport"), payload.get("notes")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                        raise AppError(400, "Enter a route name of 1–120 characters.")
                    if not isinstance(sport, str) or not 1 <= len(sport.strip()) <= 80:
                        raise AppError(400, "Enter a sport of 1–80 characters.")
                    if not isinstance(notes, str) or len(notes) > 20000:
                        raise AppError(400, "Notes must contain at most 20,000 characters.")
                    db.execute("""UPDATE planned_routes SET name=?,sport=?,notes=?,revision=revision+1,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""", (name.strip(), sport.strip(), notes, rid))
                    db.commit()
                    return handler.send_json(planned_route_detail(db, rid))
                if method == "DELETE":
                    filename = db.execute("SELECT stored_filename FROM planned_routes WHERE id=?", (rid,)).fetchone()[0]
                    collections = [r[0] for r in db.execute("SELECT collection_id FROM route_collection_members WHERE route_id=?", (rid,))]
                    db.execute("DELETE FROM planned_routes WHERE id=?", (rid,))
                    for cid in collections:
                        ids = [r[0] for r in db.execute("SELECT route_id FROM route_collection_members WHERE collection_id=? ORDER BY position", (cid,))]
                        collection_order(db, cid, ids)
                    db.commit()
                    path = (route_storage_dir(context) / filename).resolve()
                    if path.is_relative_to(route_storage_dir(context).resolve()):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            LOG.warning("Could not remove orphaned route file %s", filename)
                    return handler.send_json({"deleted": True})
            if method == "GET" and parts[3:] == ["track-matches"]:
                return handler.send_json(route_match_results(db, "route", rid, query))
            if method == "GET" and parts[3:] == ["geometry"]:
                return handler.send_json(planned_route_geometry(db, rid, query.get("preview") == ["1"]))
            if method == "GET" and parts[3:] == ["download"]:
                filename = db.execute("SELECT stored_filename FROM planned_routes WHERE id=?", (rid,)).fetchone()[0]
                path = (route_storage_dir(context) / filename).resolve()
                if not path.is_relative_to(route_storage_dir(context).resolve()) or not path.is_file():
                    raise AppError(404, "The original GPX file is unavailable.")
                return handler.send_download(path.read_bytes(), route["original_filename"], "application/gpx+xml")
            if method == "GET" and parts[3:] == ["notes"]:
                return handler.send_download((route["name"] + "\n\n" + route["notes"]).encode("utf-8"),
                                          route["name"] + "-notes.txt", "text/plain; charset=utf-8")
            raise AppError(405, "Method not allowed.")
    except AppError as exc:
        handler.send_json({"error": exc.message}, exc.status)
    except Exception:
        LOG.exception("Routes API request failed")
        handler.send_json({"error": "The route request could not be completed. Please retry."}, 500)
