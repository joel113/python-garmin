"""Chronological exploration uses synthetic sections and cached matches only."""
import json
import unittest
from contextlib import closing

from garmin_library.errors import AppError
from garmin_library.exploration import new_ranges, track_exploration
from support import TemporaryLibrary


class ExplorationTests(TemporaryLibrary, unittest.TestCase):
    generation = 'e'*32

    def setUp(self):
        super().setUp()
        with closing(self.context.connect()) as db:
            db.execute("INSERT INTO network_generations(id,regions,status) VALUES(?,'[]','published')", (self.generation,))
            db.execute("UPDATE network_state SET active_generation=?,status='ready' WHERE id=1", (self.generation,))
            self.sid = db.execute('''INSERT INTO network_sections(generation,way_id,ordinal,name,road_class,
                cycling,running,geometry,length_m) VALUES(?,1,0,'Road','path',1,1,?,100)''',
                (self.generation, json.dumps([[11,48],[11.0015,48]]))).lastrowid
            db.commit()

    def activity(self, db, aid, date, ranges=None, sport='cycling', status='completed', outside=0, unmatched=0):
        db.execute('INSERT INTO activities(id,file_path,sport,start_time) VALUES(?,?,?,?)',
                   (aid, aid+'.fit', sport, date))
        db.execute('UPDATE network_jobs SET status=?,outside_coverage=?,unmatched_points=? WHERE activity_id=? AND generation=?',
                   (status, outside, unmatched, aid, self.generation))
        if ranges is not None:
            db.execute('INSERT INTO network_contributions VALUES(?,?,?,?,0,0)',
                       (self.generation, aid, self.sid, json.dumps(ranges)))
        db.commit()

    def result(self, db, aid='selected', geometry=False):
        db.execute('BEGIN')
        try:
            return track_exploration(db, aid, geometry)
        finally:
            db.rollback()

    def test_interval_subtraction_preserves_partial_new_sections(self):
        self.assertEqual(new_ranges([[0,.7],[.4,1]], [[.2,.4],[.3,.5],[.8,1]]), [[0,.2],[.5,.8]])
        self.assertEqual(new_ranges([[0,1]], [[0,1]]), [])
        self.assertEqual(new_ranges([[.2,.4]], []), [[.2,.4]])

    def test_cross_sport_history_and_repeated_laps_count_once(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'bike', '2026-01-01T10:00:00Z', [[0,.2]])
            self.activity(db, 'run', '2026-01-02T10:00:00', [[.2,.4]], sport='running')
            self.activity(db, 'future', '2026-02-01T10:00:00Z', [[0,1]])
            self.activity(db, 'selected', '2026-01-03T10:00:00Z', [[0,.7],[.4,1]])
            result = self.result(db, geometry=True)
            self.assertEqual(result['status'], 'available')
            self.assertAlmostEqual(result['matched_unique_m'], 100)
            self.assertAlmostEqual(result['new_m'], 60)
            self.assertAlmostEqual(result['new_percent'], 60)
            self.assertEqual(result['earlier_activities'], 2)
            features = result['geometry']['features']
            self.assertEqual(len(features), 1)
            self.assertAlmostEqual(features[0]['geometry']['coordinates'][0][0], 11.0006)
            self.assertAlmostEqual(features[0]['properties']['new_m'], 60)

    def test_first_visit_and_later_reverse_visit_share_same_sections(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'selected', '2026-01-03T10:00:00Z', [[0,1]])
            self.assertEqual(self.result(db)['new_percent'], 100)
            self.activity(db, 'earlier', '2026-01-02T10:00:00Z', [[0,1]])
            result = self.result(db, geometry=True)
            self.assertEqual(result['new_percent'], 0)
            self.assertEqual(result['geometry']['features'], [])
            db.execute("DELETE FROM activities WHERE id='earlier'"); db.commit()
            self.assertEqual(self.result(db)['new_percent'], 100)

    def test_timezone_offsets_ties_and_changed_dates(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'selected', '2026-01-03T13:00:00+01:00', [[0,1]])
            self.activity(db, 'tied', '2026-01-03T12:00:00Z', [[0,1]])
            self.activity(db, 'prior', '2026-01-03T11:59:00Z', [[0,.25]])
            self.assertEqual(self.result(db)['new_percent'], 75)
            db.execute("UPDATE activities SET start_time='2026-01-02' WHERE id='tied'"); db.commit()
            self.assertEqual(self.result(db)['new_percent'], 0)

    def test_incomplete_history_and_coverage_are_provisional(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'selected', '2026-01-03T10:00:00Z', [[0,1]], outside=1, unmatched=5)
            self.activity(db, 'pending', '2026-01-01', [[0,1]], status='pending')
            self.activity(db, 'failed', '2026-01-01', status='failed')
            self.activity(db, 'stale', '2026-01-01', [[0,1]])
            self.activity(db, 'undated', None, [[0,1]])
            self.activity(db, 'partial', '2026-01-01', [[0,.25]], unmatched=2)
            db.execute("UPDATE network_source_versions SET revision=revision+1 WHERE activity_id='stale'"); db.commit()
            result = self.result(db)
            self.assertEqual(result['status'], 'provisional')
            self.assertEqual(result['missing_history_matches'], 3)
            self.assertEqual(result['undated_history_activities'], 1)
            self.assertEqual(result['partial_history_activities'], 1)
            self.assertFalse(result['history_complete'])
            self.assertFalse(result['coverage_complete'])
            self.assertEqual(result['new_percent'], 75)

    def test_missing_dates_pending_failure_zero_distance_and_unsupported_sport(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'selected', None, [[0,1]])
            self.assertEqual(self.result(db)['status'], 'unavailable')
            db.execute("UPDATE activities SET start_time='2026-01-03' WHERE id='selected'")
            db.execute("UPDATE network_jobs SET status='pending' WHERE activity_id='selected'"); db.commit()
            self.assertEqual(self.result(db)['status'], 'pending')
            db.execute("UPDATE network_jobs SET status='failed' WHERE activity_id='selected'"); db.commit()
            self.assertEqual(self.result(db)['status'], 'failed')
            db.execute("UPDATE network_jobs SET status='completed' WHERE activity_id='selected'")
            db.execute("UPDATE network_source_versions SET revision=revision+1 WHERE activity_id='selected'"); db.commit()
            self.assertEqual(self.result(db)['status'], 'pending')
            db.execute("UPDATE network_jobs SET revision=(SELECT revision FROM network_source_versions WHERE activity_id='selected') WHERE activity_id='selected'")
            db.execute("DELETE FROM network_contributions WHERE activity_id='selected'"); db.commit()
            result = self.result(db)
            self.assertEqual(result['status'], 'no_matched_distance')
            self.assertIsNone(result['new_percent'])
            db.execute("UPDATE activities SET sport='swimming' WHERE id='selected'"); db.commit()
            self.assertEqual(self.result(db)['status'], 'unavailable')
            with self.assertRaises(AppError):self.result(db, 'missing')

    def test_published_generation_changes_never_reuse_old_geometry(self):
        with closing(self.context.connect()) as db:
            self.activity(db, 'selected', '2026-01-03', [[0,1]])
            self.assertEqual(self.result(db)['new_percent'], 100)
            other = 'f'*32
            db.execute("INSERT INTO network_generations(id,regions,status) VALUES(?,'[]','published')", (other,))
            db.execute("UPDATE network_generations SET status='retired' WHERE id=?", (self.generation,))
            db.execute('UPDATE network_state SET active_generation=? WHERE id=1', (other,)); db.commit()
            result = self.result(db, geometry=True)
            self.assertEqual(result['generation'], other)
            self.assertEqual(result['status'], 'pending')
            self.assertEqual(result['geometry']['features'], [])
