"""Ordered route-track alignment and revision-checked cached matches."""
from __future__ import annotations

from contextlib import closing
from itertools import groupby
import json
import logging
import math

from garmin_library.errors import AppError
from garmin_library.geometry import display_segments, distance_between
from garmin_library.routes import planned_route_detail

LOG = logging.getLogger(__name__)

class RouteMatchingCancelled(Exception):
    pass


def matching_samples(parts):
    """Distance-weighted samples, with no edge between separate source parts."""
    sampled, flat, weights, refs = [], [], [], []
    for part in parts:
        clean = []
        for point in part:
            coordinate = list(point[:2])
            if not clean or distance_between(clean[-1], coordinate) > 0.001:
                clean.append(coordinate)
        if len(clean) < 2:
            continue
        output = [clean[0]]
        # Resample by cumulative distance, rather than multiplying dense FIT records.
        until_next = 20.0
        for a, b in zip(clean, clean[1:]):
            length = distance_between(a, b)
            travelled = until_next
            while travelled < length:
                fraction = travelled / length
                delta_lon = (b[1] - a[1] + 180) % 360 - 180
                output.append([a[0] + (b[0] - a[0]) * fraction,
                               (a[1] + delta_lon * fraction + 180) % 360 - 180])
                travelled += 20.0
            until_next = travelled - length
        if distance_between(output[-1], clean[-1]) > 0.001:
            output.append(clean[-1])
        if len(output) < 2:
            continue
        edges = [distance_between(a, b) for a, b in zip(output, output[1:])]
        part_index = len(sampled)
        sampled.append(output)
        for index, coordinate in enumerate(output):
            flat.append(coordinate)
            weights.append(((edges[index-1] if index else 0) + (edges[index] if index < len(edges) else 0)) / 2)
            refs.append((part_index, index))
    return {"parts": sampled, "points": flat, "weights": weights, "refs": refs, "length": sum(weights)}


def matching_route_samples(db, rid):
    rows = db.execute("SELECT part,lat,lon FROM planned_route_points WHERE route_id=? ORDER BY part,seq", (rid,))
    return matching_samples([[[p["lat"], p["lon"]] for p in group] for _, group in groupby(rows, lambda p: p["part"])])


def matching_track_samples(db, aid):
    return matching_samples(display_segments(db.execute(
        "SELECT lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq", (aid,))))


def matching_ranges(samples, indices):
    selected = sorted({samples["refs"][index] for index in indices})
    ranges = []
    for part, index in selected:
        if ranges and ranges[-1][0] == part and index == ranges[-1][2] + 1:
            ranges[-1][2] = index
        else:
            ranges.append([part, index, index])
    return ranges


def ordered_route_alignment(route, track, tolerance, stop):
    """Sparse weighted sequence alignment; one-to-one samples prevent lap inflation."""
    import numpy as np
    from sklearn.neighbors import BallTree

    if not route["points"] or not track["points"]:
        return None
    tree = route.get("spatial_index")
    if tree is None:
        tree = BallTree(np.radians(route["points"]), metric="haversine")
        route["spatial_index"] = tree
    candidates = []
    rotations = {0}
    is_loop = (len(route["parts"]) == 1 and
               distance_between(route["points"][0], route["points"][-1]) <= min(tolerance, route["length"] * .02))
    previous_hit = False
    # Batching bounds spatial-query memory, even for long, dense source recordings.
    for offset in range(0, len(track["points"]), 256):
        if stop.is_set():
            raise RouteMatchingCancelled()
        neighbors, distances = tree.query_radius(np.radians(track["points"][offset:offset+256]),
                                                 r=tolerance / 6_371_000, return_distance=True)
        for nearby, distances_here in zip(neighbors, distances):
            hit = bool(len(nearby))
            if hit and not previous_hit and is_loop:
                rotations.add(int(nearby[int(np.argmin(distances_here))]))
            candidates.append([int(index) for index in nearby])
            previous_hit = hit
    size = len(route["points"])
    best = (0.0, [], "forward")
    for reverse in (False, True):
        for rotation in sorted(rotations):
            # A cyclic route may begin anywhere; each original sample is still used once.
            order = list(range(rotation, size)) + list(range(rotation))
            if reverse:
                order = [order[0]] + list(reversed(order[1:])) if is_loop else list(reversed(order))
            ranks = {original: rank for rank, original in enumerate(order)}
            fenwick = [None] * (size + 1)
            nodes = []

            def prefix(end):
                result = None
                while end > 0:
                    node = fenwick[end]
                    if node is not None and (result is None or nodes[node][0] > nodes[result][0]):
                        result = node
                    end -= end & -end
                return result

            for ti, nearby in enumerate(candidates):
                if ti % 256 == 0 and stop.is_set():
                    raise RouteMatchingCancelled()
                updates = []
                for ri in sorted(nearby, key=ranks.get):
                    rank = ranks[ri]
                    previous = prefix(rank)
                    score = (nodes[previous][0] if previous is not None else 0) + min(route["weights"][ri], track["weights"][ti])
                    node = len(nodes)
                    nodes.append((score, previous, ri, ti))
                    updates.append((rank + 1, node))
                # Apply after the whole row, so one track sample cannot match many route samples.
                for position, node in updates:
                    while position <= size:
                        old = fenwick[position]
                        if old is None or nodes[node][0] > nodes[old][0]:
                            fenwick[position] = node
                        position += position & -position
            end = prefix(size)
            if end is not None and nodes[end][0] > best[0]:
                pairs = []
                node = end
                while node is not None:
                    _, previous, ri, ti = nodes[node]
                    pairs.append((ri, ti))
                    node = previous
                best = (nodes[end][0], pairs, "reverse" if reverse else "forward")
    if not best[1]:
        return None
    shared, pairs, direction = best
    return {"shared_m": shared, "route_coverage": min(1, shared / route["length"]),
            "track_coverage": min(1, shared / track["length"]), "direction": direction,
            "ranges": {"route": matching_ranges(route, [p[0] for p in pairs]),
                       "track": matching_ranges(track, [p[1] for p in pairs])}}


def classify_route_match(match, route_length, state):
    if match is None or match["shared_m"] + 1e-7 < min(250, route_length * .5):
        return None
    coverage = match["route_coverage"] * 100
    if coverage + 1e-9 >= state["full_percent"]:
        return "full"
    if coverage + 1e-9 >= state["partial_percent"]:
        return "partial"
    return None


def route_matching_worker(context, stop):
    """Revision-checked snapshots keep old results readable during recalculation."""
    with closing(context.connect()) as db:
        db.execute("UPDATE route_matching_state SET status='pending' WHERE status='processing'")
        db.commit()
    while not stop.is_set():
        revision = None
        try:
            with closing(context.connect()) as progress, closing(context.connect()) as db:
                state = dict(progress.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone())
                if state["status"] != "pending":
                    stop.wait(2)
                    continue
                revision = state["revision"]
                progress.execute("UPDATE route_matching_state SET status='processing',error=NULL,progress_done=0 WHERE id=1 AND revision=?", (revision,))
                progress.commit()
                db.execute("BEGIN")
                # Snapshot all source versions together, including imports committed just before this read.
                if db.execute("SELECT revision FROM route_matching_state WHERE id=1").fetchone()[0] != revision:
                    continue
                routes = [r[0] for r in db.execute("SELECT id FROM planned_routes ORDER BY id")]
                activities = db.execute("""SELECT activity_id,MIN(lat) AS min_lat,MAX(lat) AS max_lat,
                    MIN(lon) AS min_lon,MAX(lon) AS max_lon FROM track_points GROUP BY activity_id""").fetchall() if routes else []
                progress.execute("UPDATE route_matching_state SET progress_total=? WHERE id=1 AND revision=?", (len(routes), revision))
                progress.commit()
                results = []
                for done, rid in enumerate(routes, 1):
                    if stop.is_set() or progress.execute("SELECT revision FROM route_matching_state WHERE id=1").fetchone()[0] != revision:
                        raise RouteMatchingCancelled()
                    route = matching_route_samples(db, rid)
                    if route["length"] <= 0:
                        continue
                    latitudes = [p[0] for p in route["points"]]
                    longitudes = [p[1] for p in route["points"]]
                    pad_lat = state["tolerance_m"] / 111_195
                    pad_lon = pad_lat / max(.001, min(math.cos(math.radians(lat)) for lat in latitudes))
                    min_lat, max_lat = min(latitudes)-pad_lat, max(latitudes)+pad_lat
                    min_lon, max_lon = min(longitudes)-pad_lon, max(longitudes)+pad_lon
                    for activity in activities:
                        if stop.is_set() or progress.execute("SELECT revision FROM route_matching_state WHERE id=1").fetchone()[0] != revision:
                            raise RouteMatchingCancelled()
                        if (activity["max_lat"] < min_lat or activity["min_lat"] > max_lat or
                            activity["max_lon"] < min_lon or activity["min_lon"] > max_lon):
                            continue
                        track = matching_track_samples(db, activity["activity_id"])
                        if track["length"] <= 0:
                            continue
                        match = ordered_route_alignment(route, track, state["tolerance_m"], stop)
                        classification = classify_route_match(match, route["length"], state)
                        if classification:
                            results.append((rid, activity["activity_id"], classification, match["route_coverage"],
                                            match["track_coverage"], match["shared_m"], match["direction"],
                                            json.dumps(match["ranges"]), revision))
                    progress.execute("UPDATE route_matching_state SET progress_done=? WHERE id=1 AND revision=?", (done, revision))
                    progress.commit()
                db.rollback()
                progress.execute("BEGIN IMMEDIATE")
                if stop.is_set() or progress.execute("SELECT revision FROM route_matching_state WHERE id=1").fetchone()[0] != revision:
                    raise RouteMatchingCancelled()
                progress.execute("DELETE FROM route_track_matches")
                progress.executemany("INSERT INTO route_track_matches VALUES(?,?,?,?,?,?,?,?,?)", results)
                progress.execute("UPDATE route_matching_state SET status='completed',error=NULL,published_revision=?,progress_done=progress_total WHERE id=1", (revision,))
                progress.commit()
        except RouteMatchingCancelled:
            # New input already queued the next generation. Interrupted processing resumes on startup.
            continue
        except Exception:
            LOG.exception("Route matching failed")
            if revision is not None:
                with closing(context.connect()) as db:
                    db.execute("UPDATE route_matching_state SET status='failed',error='Route matching failed. Use Retry matching.' WHERE id=1 AND revision=?", (revision,))
                    db.commit()
            stop.wait(2)


def route_match_results(db, kind, identifier, query):
    if kind == "route":
        planned_route_detail(db, identifier)
        where, join = "m.route_id=?", "a.start_time,a.sport,a.distance_m AS activity_distance_m,r.name"
    else:
        if not db.execute("SELECT 1 FROM activities WHERE id=?", (identifier,)).fetchone():
            raise AppError(404, "Activity not found.")
        where, join = "m.activity_id=?", "a.start_time,a.sport,r.name"
    try:
        page = max(0, int(query.get("page", [0])[0]))
    except ValueError:
        raise AppError(400, "Invalid match page.")
    totals = dict(db.execute(f"""SELECT COUNT(*) AS total,COALESCE(SUM(classification='full'),0) AS full,
        COALESCE(SUM(classification='partial'),0) AS partial FROM route_track_matches m WHERE {where}""", (identifier,)).fetchone())
    rows = db.execute(f"""SELECT m.route_id,m.activity_id,m.classification,m.route_coverage,m.track_coverage,
        m.shared_m,m.direction,m.generation,{join} FROM route_track_matches m
        JOIN activities a ON a.id=m.activity_id JOIN planned_routes r ON r.id=m.route_id WHERE {where}
        ORDER BY (m.classification='full') DESC,m.route_coverage DESC,m.shared_m DESC,m.route_id,m.activity_id LIMIT 25 OFFSET ?""",
        (identifier, page * 25)).fetchall()
    return {"matches": [dict(r) for r in rows], "counts": totals, "page": page,
            "processing": dict(db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone())}


def route_match_geometry(db, rid, aid):
    state = db.execute("SELECT * FROM route_matching_state WHERE id=1").fetchone()
    match = db.execute("SELECT * FROM route_track_matches WHERE route_id=? AND activity_id=?", (rid, aid)).fetchone()
    if match is None:
        raise AppError(404, "Match not found.")
    if match["generation"] != state["revision"]:
        raise AppError(409, "Match geometry is updating. Please wait for matching to finish.")
    route, track = matching_route_samples(db, rid), matching_track_samples(db, aid)
    ranges = json.loads(match["ranges"])

    def highlights(samples, spans):
        result = []
        for part, start, end in spans:
            # A sample represents a distance bin; include its neighboring half-edges.
            points = samples["parts"][part]
            line = points[max(0, start-1):min(len(points), end+2)]
            if len(line) >= 2:
                result.append(line)
        return result

    return {"route": route["parts"], "track": track["parts"],
            "matched_route": highlights(route, ranges["route"]), "matched_track": highlights(track, ranges["track"])}
