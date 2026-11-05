"""Generated CSV and standalone map exports."""
from __future__ import annotations

from datetime import datetime
import csv
import logging

import fitparse
import folium

from garmin_library.activities import activity_files, parse_gpx
from garmin_library.geometry import distance_between

LOG = logging.getLogger(__name__)

def export_csv(context):
    """Export one row per FIT session, preserving the former main.py output."""
    import pandas as pd

    rows = []
    for path in activity_files(context):
        try:
            if path.suffix.lower() == ".gpx":
                points = parse_gpx(path)
                ts = [p[2] for p in points if p[2]]
                try:
                    duration = (datetime.fromisoformat(ts[-1].replace("Z", "+00:00")) - datetime.fromisoformat(ts[0].replace("Z", "+00:00"))).total_seconds() if len(ts) > 1 else None
                except ValueError:
                    duration = None
                rows.append({"start_time": ts[0] if ts else "", "duration_s": duration,
                             "distance_m": sum(distance_between(a, b) for a, b in zip(points, points[1:])),
                             "activity_type": "gpx"})
            else:
                fit = fitparse.FitFile(str(path))
                for session in fit.get_messages("session"):
                    rows.append({"start_time": session.get_value("start_time"), "duration_s": session.get_value("total_timer_time"),
                                 "distance_m": session.get_value("total_distance"), "activity_type": session.get_value("sport")})
        except Exception:
            LOG.exception("Could not read session data from %s", path)
    target = context.settings.root / "analysis" / "activities.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["start_time", "duration_s", "distance_m", "activity_type"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Exported {len(rows)} sessions to {target}")


def export_map(context):
    """Generate the former standalone Folium map from the FIT files."""
    map_view = folium.Map(location=[48.1351, 11.5820], zoom_start=12)
    for path in activity_files(context):
        try:
            if path.suffix.lower() == ".gpx":
                points = [(lat, lon) for lat, lon, _, _ in parse_gpx(path)]
            else:
                points = []
                fit = fitparse.FitFile(str(path))
                for record in fit.get_messages("record"):
                    lat, lon = record.get_value("position_lat"), record.get_value("position_long")
                    if lat is not None and lon is not None:
                        points.append((lat * 180 / 2**31, lon * 180 / 2**31))
            if points:
                folium.PolyLine(points, color="blue", weight=2.5, opacity=1,
                                tooltip=path.name).add_to(map_view)
        except Exception:
            LOG.exception("Could not plot %s", path)
    target = context.settings.root / "analysis" / "activities_map.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    map_view.save(str(target))
    print(f"Saved static map to {target}")
