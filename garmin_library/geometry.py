"""Shared coordinate, timestamp, and discontinuity helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import math

def distance_between(a, b):
    radius = 6_371_000
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(min(1, hav)))


def point_time(raw_time):
    try:
        value = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def gps_discontinuity(previous, current):
    distance = distance_between(previous, current)
    elapsed = current[2] - previous[2] if current[2] is not None and previous[2] is not None else None
    if elapsed is None:
        return distance > 10_000
    return (elapsed < 0 or (elapsed > 300 and distance > 500) or
            (distance > 300 and (elapsed <= 0 or distance / elapsed > 55)))


def display_segments(rows, stride=1):
    """Split GPS discontinuities before sampling; leave stored records untouched."""
    segments, segment = [], []
    previous = None
    last_coordinate = None
    count = 0

    def finish():
        if count > 1:
            if segment[-1] != last_coordinate:
                segment.append(last_coordinate)
            if len(segment) > 1:
                segments.append(segment)

    for row in rows:
        lat, lon, raw_time = row["lat"], row["lon"], row["timestamp"]
        try:
            timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
            seconds = timestamp.timestamp()
        except (AttributeError, ValueError, TypeError):
            seconds = None
        coordinate = [lat, lon]
        discontinuity = previous is not None and gps_discontinuity(previous, (lat, lon, seconds))
        if discontinuity:
            finish()
            segment, count = [], 0
        if count % stride == 0:
            segment.append(coordinate)
        count += 1
        last_coordinate = coordinate
        previous = (lat, lon, seconds)
    finish()
    return segments


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat() if seconds is not None else None

