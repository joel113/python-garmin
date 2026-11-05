"""Synthetic fixtures; never read the user's environment, database, or originals."""
import tempfile
from contextlib import closing
from pathlib import Path

from garmin_library.config import Settings
from garmin_library.context import AppContext

GPX = b'''<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Test journey</name><trkseg><trkpt lat="48" lon="11"><ele>500</ele><time>2026-01-01T10:00:00Z</time></trkpt><trkpt lat="48.001" lon="11.001"><ele>510</ele><time>2026-01-01T10:01:00Z</time></trkpt><trkpt lat="48.002" lon="11.002"><ele>505</ele><time>2026-01-01T10:02:00Z</time></trkpt></trkseg></trk></gpx>'''


class TemporaryLibrary:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.context = AppContext(Settings.from_environment({}, root=self.root))


def seed_baseline(context):
    with closing(context.connect()) as db:
        db.execute("INSERT INTO activities(id,file_path,sport,start_time,distance_m,duration_s) VALUES('sample','storage/activities/sample.gpx','cycling','2026-01-01T10:00:00',200,120)")
        db.executemany("INSERT INTO track_points(activity_id,seq,lat,lon,timestamp,altitude,temperature_c) VALUES('sample',?,?,?,?,?,?)",[(0,48,11,'2026-01-01T10:00:00',500,10),(1,48.001,11.001,'2026-01-01T10:01:00',510,12),(2,48.002,11.002,'2026-01-01T10:02:00',505,14)])
        db.execute("INSERT INTO planned_routes(id,source_hash,stored_filename,original_filename,name,distance_m,elevation_coverage,point_count) VALUES('plan','hash','plan.gpx','plan.gpx','Planned trip',200,0,3)")
        db.executemany("INSERT INTO planned_route_points VALUES('plan',0,?,?,?,NULL)",[(0,48,11),(1,48.001,11.001),(2,48.002,11.002)])
        db.commit()
