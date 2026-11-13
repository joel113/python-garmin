"""Workout equipment uses temporary libraries and mocked Garmin responses only."""
import io
import json
import sqlite3
import unittest
import urllib.error
from contextlib import closing, redirect_stdout
from unittest.mock import Mock, patch

from garmin_library.activities import import_file
from garmin_library.garmin import GarminClient, fetch_equipment
from garmin_library.workouts import activity_equipment, fetch_activity_equipment
from support import GPX, TemporaryLibrary


WORKOUTS = [
    {'index': 3, 'workoutName': '<Swim>', 'steps': [
        {'stepIndex': 0, 'name': 'Warm up', 'equipmentType': 'NONE'},
        {'stepIndex': 1, 'name': 'Kick', 'equipmentType': 'SWIM_KICKBOARD'},
        {'stepIndex': 1, 'equipmentType': 'SWIM_KICKBOARD'},
        {'name': None, 'equipmentType': 'NEW_EQUIPMENT'},
        {'stepIndex': 4}]},
    {'workoutName': 'Second workout', 'steps': [{'equipmentType': 'SWIM_FINS'}]},
]


class WorkoutTests(TemporaryLibrary, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(self.context.connect()) as db:
            db.executemany('INSERT INTO activities(id,file_path) VALUES(?,?)',
                           [('123', '123.gpx'), ('124', '124.fit'), ('local-trip', 'local-trip.gpx')])
            db.commit()

    def result(self, aid='123'):
        with closing(self.context.connect()) as db:
            return activity_equipment(db, aid)

    def test_workouts_preserve_steps_and_unknown_equipment_without_gps(self):
        client = Mock()
        client.get.return_value = json.dumps(WORKOUTS).encode()
        self.assertTrue(fetch_activity_equipment(self.context, client, '123'))
        result = self.result()
        self.assertEqual(result['status'], 'loaded')
        self.assertEqual(result['workouts'], WORKOUTS)
        self.assertEqual(result['equipment_types'], ['NEW_EQUIPMENT', 'SWIM_FINS', 'SWIM_KICKBOARD'])
        self.assertIsNotNone(result['fetched_at'])

    def test_empty_and_unspecified_equipment_are_completed_results(self):
        self.assertEqual(self.result()['status'], 'not_fetched')
        client = Mock()
        for payload, expected in [([], 'no_workouts'), ([{'steps': [{'equipmentType': 'NONE'}]}], 'no_equipment'),
                                  ([{'steps': None}], 'no_equipment')]:
            with self.subTest(payload=payload):
                client.get.return_value = json.dumps(payload).encode()
                self.assertTrue(fetch_activity_equipment(self.context, client, '123'))
                self.assertEqual(self.result()['status'], expected)

    def test_failed_refresh_keeps_previous_workouts_and_sanitizes_errors(self):
        client = Mock()
        client.get.return_value = json.dumps(WORKOUTS).encode()
        fetch_activity_equipment(self.context, client, '123')
        fetched_at = self.result()['fetched_at']
        for failure in [urllib.error.HTTPError('https://example.invalid', 403, 'private', {}, None),
                        urllib.error.URLError('private response')]:
            client.get.side_effect = failure
            self.assertFalse(fetch_activity_equipment(self.context, client, '123'))
            result = self.result()
            self.assertEqual(result['status'], 'refresh_failed')
            self.assertEqual(result['workouts'], WORKOUTS)
            self.assertEqual(result['fetched_at'], fetched_at)
            self.assertNotIn('private', result['error'])
        client.get.side_effect = None
        client.get.return_value = b'{invalid'
        self.assertFalse(fetch_activity_equipment(self.context, client, '124'))
        self.assertEqual(self.result('124')['status'], 'failed')
        client.get.return_value = b'[]'
        self.assertTrue(fetch_activity_equipment(self.context, client, '123'))
        self.assertIsNone(self.result()['error'])

    def test_invalid_structures_do_not_overwrite_successful_data(self):
        client = Mock()
        client.get.return_value = json.dumps(WORKOUTS).encode()
        fetch_activity_equipment(self.context, client, '123')
        for payload in [{}, [None], [{'steps': {}}], [{'steps': [None]}], [{'steps': [{'equipmentType': {}}]}]]:
            client.get.return_value = json.dumps(payload).encode()
            self.assertFalse(fetch_activity_equipment(self.context, client, '123'))
            self.assertEqual(self.result()['workouts'], WORKOUTS)

    def test_backfill_resume_refresh_and_authentication_stop(self):
        client = Mock()
        client.get.side_effect = [b'[]', KeyboardInterrupt()]
        with patch('garmin_library.garmin.GarminClient', return_value=client), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):fetch_equipment(self.context)
        self.assertEqual(self.result()['status'], 'no_workouts')
        client.get.side_effect = None
        client.get.return_value = b'[]'
        client.get.reset_mock()
        with patch('garmin_library.garmin.GarminClient', return_value=client), redirect_stdout(io.StringIO()) as output:
            fetch_equipment(self.context)
            self.assertIn('1 non-Garmin', output.getvalue())
            self.assertEqual(client.get.call_count, 1)
            fetch_equipment(self.context)
            self.assertEqual(client.get.call_count, 1)
            fetch_equipment(self.context, activity_id='123', refresh=True)
            self.assertEqual(client.get.call_count, 2)
        client.get.side_effect = urllib.error.HTTPError('https://example.invalid', 401, 'private', {}, None)
        with patch('garmin_library.garmin.GarminClient', return_value=client):
            with self.assertRaises(SystemExit):fetch_equipment(self.context, refresh=True)
        self.assertEqual(self.result()['status'], 'refresh_failed')

    def test_reimport_preserves_metadata_and_migration_preserves_activities(self):
        client = Mock()
        client.get.return_value = json.dumps(WORKOUTS).encode()
        fetch_activity_equipment(self.context, client, '123')
        before = self.result()
        source = self.root/'123.gpx'
        source.write_bytes(GPX)
        with closing(self.context.connect()) as db:
            self.assertTrue(import_file(db, source))
        self.assertEqual(self.result(), before)
        fit_source = self.root/'123.fit'
        fit_source.write_bytes(b'synthetic fixture')
        session = Mock()
        session.get_value.side_effect = {'start_time':None,'sport':'swimming','total_timer_time':10,'total_distance':25}.get
        fit = Mock()
        fit.get_messages.side_effect = lambda name: [session] if name == 'session' else []
        with patch('garmin_library.activities.fitparse.FitFile', return_value=fit), closing(self.context.connect()) as db:
            self.assertTrue(import_file(db, fit_source))
        self.assertEqual(self.result(), before)
        # Simulate an existing installation without this additive table.
        with closing(sqlite3.connect(self.context.settings.database_path)) as db:
            db.execute('DROP TABLE activity_workouts')
            db.commit()
        self.assertEqual(self.result()['status'], 'not_fetched')
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activities').fetchone()[0], 3)

    def test_shared_client_paces_successes_and_failures(self):
        client = GarminClient.__new__(GarminClient)
        client.token = 'synthetic.token.value'
        client.last_request_at = 10
        failure = urllib.error.HTTPError('https://example.invalid', 503, 'failure', {}, None)
        with patch('garmin_library.garmin.api_request', side_effect=[b'[]', failure]) as request, \
             patch('garmin_library.garmin.time.monotonic', side_effect=[10.25, 12, 12.5, 14]), \
             patch('garmin_library.garmin.time.sleep') as sleep:
            self.assertEqual(client.get('/activity-service/activity/123/workouts'), b'[]')
            with self.assertRaises(urllib.error.HTTPError):client.get('/download-service/files/activity/124')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [.75, .5])
        self.assertEqual(client.last_request_at, 14)
        self.assertTrue(all(call.args[1] == client.token for call in request.call_args_list))
