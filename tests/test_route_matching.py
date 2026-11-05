"""Synthetic route-matching scenarios. Not executed during implementation."""
import threading
import unittest

from app import (classify_route_match, matching_samples, ordered_route_alignment,
                 matching_ranges, display_segments)


def line(start, end, spacing=10, latitude=48):
    # Local coordinates in metres, far from poles and the antimeridian.
    import math
    steps = max(1, round(abs(end-start) / spacing))
    return [[latitude, 11 + (start+(end-start)*i/steps)/(111_195*math.cos(math.radians(latitude)))]
            for i in range(steps+1)]


class RouteMatchingTests(unittest.TestCase):
    settings = {"full_percent": 90, "partial_percent": 20}

    def align(self, route, track):
        r, t = matching_samples(route), matching_samples(track)
        return r, t, ordered_route_alignment(r, t, 50, threading.Event())

    def test_identical_path_and_different_recording_densities(self):
        r, _, match = self.align([line(0, 5000, 2)], [line(0, 5000, 37)])
        self.assertGreater(match["route_coverage"], .98)
        self.assertEqual(classify_route_match(match, r["length"], self.settings), "full")
        self.assertEqual(match["direction"], "forward")

    def test_long_track_completes_short_route(self):
        r, _, match = self.align([line(2000, 4000)], [line(0, 10000)])
        self.assertEqual(classify_route_match(match, r["length"], self.settings), "full")
        self.assertLess(match["track_coverage"], .25)

    def test_reverse(self):
        _, _, match = self.align([line(0, 5000)], [line(5000, 0)])
        self.assertGreater(match["route_coverage"], .98)
        self.assertEqual(match["direction"], "reverse")

    def test_partial(self):
        r, _, match = self.align([line(0, 10000)], [line(3000, 6000)])
        self.assertEqual(classify_route_match(match, r["length"], self.settings), "partial")
        self.assertAlmostEqual(match["route_coverage"], .3, delta=.02)

    def test_incidental_crossing_and_parallel_path(self):
        r, _, match = self.align([line(0, 10000)], [line(4900, 5100)])
        self.assertIsNone(classify_route_match(match, r["length"], self.settings))
        _, _, separated = self.align([line(0, 5000)], [line(0, 5000, latitude=48.002)])
        self.assertIsNone(separated)

    def test_repeated_laps_do_not_inflate_coverage(self):
        _, _, match = self.align([line(0, 5000)], [line(0, 5000), line(0, 5000)])
        self.assertLessEqual(match["route_coverage"], 1)
        self.assertLessEqual(match["track_coverage"], .51)

    def test_different_section_order_is_not_full(self):
        r, _, match = self.align([line(0, 12000)],
                                 [line(0, 3000), line(9000, 12000), line(3000, 6000), line(6000, 9000)])
        self.assertNotEqual(classify_route_match(match, r["length"], self.settings), "full")

    def test_closed_loop_with_different_start(self):
        square = [[48, 11], [48, 11.02], [48.02, 11.02], [48.02, 11], [48, 11]]
        rotated = square[2:-1] + square[:3]
        r, _, match = self.align([square], [rotated])
        self.assertEqual(classify_route_match(match, r["length"], self.settings), "full")

    def test_disconnected_parts_and_gps_time_reversal(self):
        sampled = matching_samples([line(0, 100), line(10000, 10100)])
        self.assertLess(sampled["length"], 201)
        ranges = matching_ranges(sampled, list(range(len(sampled["points"]))))
        self.assertEqual(len(ranges), 2)
        points = [{"lat":48,"lon":11+i*.001,"timestamp":t} for i,t in enumerate([
            "2026-01-01T10:00:00", "2026-01-01T10:01:00",
            "2026-01-01T09:00:00", "2026-01-01T09:01:00"])]
        self.assertEqual(len(display_segments(points)), 2)

    def test_threshold_boundaries_and_short_routes(self):
        def classify(coverage, shared, length=1000):
            return classify_route_match({"route_coverage":coverage,"shared_m":shared}, length, self.settings)
        self.assertEqual(classify(.9, 900), "full")
        self.assertEqual(classify(.899, 899), "partial")
        self.assertIsNone(classify(.2, 200))  # 250 m minimum for normal routes.
        self.assertEqual(classify(.2, 400, 2000), "partial")
        self.assertEqual(classify(.5, 50, 100), "partial")
        self.assertIsNone(classify(.49, 49, 100))
        self.assertEqual(matching_samples([[[48,11],[48,11]]])["length"], 0)


class RouteMatchingPersistenceTests(unittest.TestCase):
    def setUp(self):
        import app
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(app, "DB_PATH", Path(self.directory.name)/"library.sqlite3")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.db = app.connect()
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO activities(id,file_path,sport) VALUES('track','storage/activities/track.fit','cycling')")
        self.db.execute("""INSERT INTO planned_routes(id,source_hash,stored_filename,original_filename,name,distance_m,elevation_coverage,point_count)
            VALUES('route','hash','route.gpx','original.gpx','Plan',1000,0,2)""")
        self.db.commit()

    def cache(self):
        revision = self.db.execute("SELECT revision FROM route_matching_state").fetchone()[0]
        self.db.execute("INSERT INTO route_track_matches VALUES('route','track','full',1,.5,1000,'forward','{}',?)", (revision,))
        self.db.execute("UPDATE route_matching_state SET published_revision=revision,status='completed'")
        self.db.commit()
        return revision

    def test_metadata_changes_do_not_queue_geometry_work(self):
        revision = self.cache()
        self.db.execute("UPDATE planned_routes SET notes='Overnight stop',name='New name',revision=revision+1")
        self.db.execute("INSERT INTO route_collections(id,name) VALUES('trip','Holiday')")
        self.db.execute("INSERT INTO route_collection_members VALUES('trip','route',1)")
        self.db.commit()
        state = self.db.execute("SELECT * FROM route_matching_state").fetchone()
        self.assertEqual(state['revision'], revision)
        self.assertEqual(state['status'], 'completed')

    def test_import_invalidation_keeps_other_previous_results(self):
        revision = self.cache()
        self.db.execute("INSERT INTO activities(id,file_path) VALUES('new','storage/activities/new.gpx')")
        self.db.commit()
        state = self.db.execute("SELECT * FROM route_matching_state").fetchone()
        self.assertGreater(state['revision'], revision)
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM route_track_matches").fetchone()[0], 1)

    def test_deleted_sources_remove_cached_matches(self):
        self.cache()
        self.db.execute("DELETE FROM planned_routes WHERE id='route'")
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM route_track_matches").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT status FROM route_matching_state").fetchone()[0], 'pending')


    def test_both_views_return_the_same_classification_and_coverage(self):
        from app import route_match_results
        self.cache()
        route_result = route_match_results(self.db, "route", "route", {})["matches"][0]
        track_result = route_match_results(self.db, "activity", "track", {})["matches"][0]
        for key in ("classification", "route_coverage", "track_coverage", "shared_m", "direction", "generation"):
            self.assertEqual(route_result[key], track_result[key])

    def test_old_geometry_is_withheld_after_input_changes(self):
        from app import route_match_geometry, SegmentError
        self.cache()
        self.db.execute("INSERT INTO activities(id,file_path) VALUES('new','storage/activities/new.fit')")
        self.db.commit()
        with self.assertRaises(SegmentError) as error:
            route_match_geometry(self.db, "route", "track")
        self.assertEqual(error.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
