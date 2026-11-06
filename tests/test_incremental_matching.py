"""Incremental matching, cache preservation, invalidation, and queue migration."""
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from garmin_library.activities import import_file
from garmin_library.route_matching import (ordered_route_alignment, process_route_matching_job,
    queue_all_route_matches, route_match_geometry, route_matching_worker)
from garmin_library.routes import store_planned_route
from support import GPX, TemporaryLibrary


class IncrementalMatchingTests(TemporaryLibrary, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stop=threading.Event()
        folder=self.context.settings.activities_path;folder.mkdir(parents=True)
        source=folder/'trip.gpx';source.write_bytes(GPX)
        with closing(self.context.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            self.assertTrue(import_file(db,source))
        self.first=self.upload('First')

    def upload(self,name):
        content=GPX.replace(b'Test journey',name.encode())
        with closing(self.context.connect()) as db:
            return store_planned_route(self.context,db,content,name+'.gpx')['route']['id']

    def process(self):
        return process_route_matching_job(self.context,self.stop)

    def cached(self,rid):
        with closing(self.context.connect()) as db:
            return [tuple(row) for row in db.execute('SELECT * FROM route_track_matches WHERE route_id=?',(rid,))]

    def test_upload_only_processes_new_route_and_keeps_old_geometry_available(self):
        self.assertTrue(self.process())
        before=self.cached(self.first);self.assertTrue(before)
        second=self.upload('Second')
        with closing(self.context.connect()) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT route_id FROM route_matching_jobs')],[second])
            self.assertTrue(route_match_geometry(db,self.first,'trip')['route'])
        with patch('garmin_library.route_matching.ordered_route_alignment',wraps=ordered_route_alignment) as alignment:
            self.assertTrue(self.process());self.assertEqual(alignment.call_count,1)
        self.assertEqual(self.cached(self.first),before)
        self.assertTrue(self.cached(second))
        self.assertFalse(self.process())

    def test_upload_during_processing_does_not_discard_current_work(self):
        added=[]
        def align(*args):
            added.append(self.upload('During processing'))
            return ordered_route_alignment(*args)
        with patch('garmin_library.route_matching.ordered_route_alignment',side_effect=align):
            self.assertTrue(self.process())
        self.assertTrue(self.cached(self.first))
        with closing(self.context.connect()) as db:
            jobs=[r[0] for r in db.execute('SELECT route_id FROM route_matching_jobs')]
        self.assertEqual(jobs,added)

    def test_settings_change_discards_only_stale_job_publication(self):
        def align(*args):
            with closing(self.context.connect()) as db:
                queue_all_route_matches(db)
                db.execute("UPDATE route_matching_state SET revision=revision+1,status='pending'")
                db.commit()
            return ordered_route_alignment(*args)
        with patch('garmin_library.route_matching.ordered_route_alignment',side_effect=align):
            self.assertTrue(self.process())
        self.assertFalse(self.cached(self.first))
        with closing(self.context.connect()) as db:
            job=db.execute('SELECT revision,status FROM route_matching_jobs').fetchone()
            self.assertEqual(tuple(job),(2,'pending'))

    def test_activity_import_queues_all_routes_and_deletion_does_not(self):
        self.process();second=self.upload('Second');self.process()
        before=self.cached(self.first)
        with closing(self.context.connect()) as db:
            db.execute('DELETE FROM planned_routes WHERE id=?',(second,));db.commit()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM route_matching_jobs').fetchone()[0],0)
            self.assertTrue(route_match_geometry(db,self.first,'trip')['route'])
            db.execute("INSERT INTO activities(id,file_path) VALUES('new','storage/activities/new.gpx')");db.commit()
            self.assertEqual([r[0] for r in db.execute('SELECT route_id FROM route_matching_jobs')],[self.first])
        self.assertEqual(self.cached(self.first),before)

    def test_duplicate_upload_does_not_queue_completed_routes(self):
        self.process()
        self.assertEqual(self.upload('First'),self.first)
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM route_matching_jobs').fetchone()[0],0)

    def test_failed_route_does_not_prevent_another_route_finishing(self):
        second=self.upload('Second')
        with patch('garmin_library.route_matching.ordered_route_alignment',side_effect=RuntimeError('synthetic failure')), self.assertLogs('garmin_library.route_matching',level='ERROR'):
            self.assertFalse(self.process())
        self.assertTrue(self.process())
        self.assertEqual(bool(self.cached(self.first))+bool(self.cached(second)),1)
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM route_matching_jobs').fetchone()[0],'failed')
            self.assertEqual(db.execute('SELECT status FROM route_matching_state').fetchone()[0],'failed')

    def test_startup_resumes_interrupted_job(self):
        with closing(self.context.connect()) as db:
            db.execute("UPDATE route_matching_jobs SET status='processing'");db.commit()
        def stop_worker(context,stop):
            stop.set();return False
        with patch('garmin_library.route_matching.process_route_matching_job',side_effect=stop_worker):
            route_matching_worker(self.context,self.stop)
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute('SELECT status FROM route_matching_jobs').fetchone()[0],'pending')

    def test_completed_legacy_cache_is_not_requeued_on_migration(self):
        self.process();before=self.cached(self.first)
        with closing(self.context.connect()) as db:
            db.execute('DROP TABLE route_matching_jobs')
            db.execute("DELETE FROM app_migrations WHERE name='route_matching_jobs_v1'")
            db.commit()
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM route_matching_jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM route_matching_state').fetchone()[0],'completed')
        self.assertEqual(self.cached(self.first),before)
