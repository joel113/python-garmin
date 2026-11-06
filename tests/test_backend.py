"""Configuration, import safety, CLI, schema, and mocked Garmin checks."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
import urllib.error
from contextlib import closing, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from garmin_library.activities import ensure_track_temperature, import_file
from garmin_library.cli import main
from garmin_library.config import Settings, load_dotenv
from garmin_library.context import AppContext
from garmin_library.garmin import api_request, fetch_new
from garmin_library.routes import store_planned_route
from support import GPX, TemporaryLibrary


class BackendTests(TemporaryLibrary, unittest.TestCase):
    def test_settings_precedence_paths_and_secret_repr(self):
        dotenv=self.root/'.env'
        dotenv.write_text('GARMIN_DB_PATH=storage/from-file.sqlite3\nGARMIN_API_KEY="synthetic-key"\n')
        env={'GARMIN_DB_PATH':'chosen.sqlite3','GARMIN_ACTIVITIES_PATH':'source','GARMIN_ALLOWED_HOSTS':'localhost:9000'}
        load_dotenv(dotenv,env)
        settings=Settings.from_environment(env,root=self.root)
        self.assertEqual(settings.database_path,self.root/'chosen.sqlite3')
        self.assertEqual(settings.activities_path,self.root/'source')
        self.assertEqual(settings.allowed_hosts,'localhost:9000')
        self.assertEqual(settings.api_key,'synthetic-key')
        self.assertNotIn('synthetic-key',repr(settings))
        self.assertFalse(settings.database_path.exists())
        with self.assertRaises(FrozenInstanceError):settings.allowed_hosts='changed'
        container=Settings.from_environment({'GARMIN_DB_PATH':'/data/activities.sqlite3','GARMIN_ACTIVITIES_PATH':'/data/activities'},root=self.root)
        self.assertEqual(container.routes_path,Path('/data/routes'))
        self.assertEqual(container.activities_path,Path('/data/activities'))

    def test_independent_contexts_do_not_share_connections_or_data(self):
        other=AppContext(Settings.from_environment({},root=self.root/'other'))
        with closing(self.context.connect()) as first, closing(other.connect()) as second:
            first.execute("INSERT INTO activities(id,file_path) VALUES('first','storage/activities/first.gpx')")
            first.commit()
            self.assertEqual(second.execute('SELECT COUNT(*) FROM activities').fetchone()[0],0)
            with closing(self.context.connect()) as third:
                self.assertIsNot(first,third)
                self.assertEqual(third.execute('SELECT id FROM activities').fetchone()[0],'first')

    def test_existing_schema_upgrade_and_user_data_preservation(self):
        with closing(sqlite3.connect(self.context.settings.database_path)) as old:
            old.executescript('''CREATE TABLE activities(id TEXT PRIMARY KEY,file_path TEXT NOT NULL,start_time TEXT,sport TEXT,duration_s REAL,distance_m REAL,imported_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE track_points(activity_id TEXT NOT NULL,seq INTEGER NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,timestamp TEXT,altitude REAL,PRIMARY KEY(activity_id,seq));
                INSERT INTO activities(id,file_path) VALUES('old','activities/old.fit');
                INSERT INTO track_points VALUES('old',0,48,11,'',500);''')
        with closing(self.context.connect()) as db:
            self.assertIn('temperature_c',{r[1] for r in db.execute('PRAGMA table_info(track_points)')})
            db.execute("INSERT INTO segments(id,name,sport) VALUES(1,'Saved segment','cycling')")
            db.execute("INSERT INTO destinations(id,name,seed_lat,seed_lon,centroid_lat,centroid_lon) VALUES('dest','Home',48,11,48,11)")
            db.execute("INSERT INTO route_collections(id,name) VALUES('holiday','Holiday')")
            db.commit()
            route=store_planned_route(self.context,db,GPX,'plan.gpx','holiday')['route']
            db.execute("UPDATE planned_routes SET notes='Saved notes' WHERE id=?",(route['id'],));db.commit()
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute("SELECT name FROM segments WHERE id=1").fetchone()[0],'Saved segment')
            self.assertEqual(db.execute("SELECT name FROM destinations WHERE id='dest'").fetchone()[0],'Home')
            self.assertEqual(db.execute('SELECT notes FROM planned_routes').fetchone()[0],'Saved notes')
            self.assertEqual(db.execute('SELECT position FROM route_collection_members').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT altitude FROM track_points').fetchone()[0],500)

    def test_cli_import_exports_and_legacy_source_path(self):
        folder=self.context.settings.activities_path;folder.mkdir(parents=True)
        source=folder/'sample.gpx';source.write_bytes(GPX)
        with patch('garmin_library.cli.load_dotenv') as dotenv, patch('garmin_library.cli.Settings.from_environment',return_value=self.context.settings), redirect_stdout(io.StringIO()):
            for command in ('import','export-csv','export-map'):
                main([command])
            self.assertEqual(dotenv.call_count,3)
        self.assertIn('activity_type',(self.root/'analysis/activities.csv').read_text())
        self.assertIn('leaflet',(self.root/'analysis/activities_map.html').read_text().lower())
        with closing(self.context.connect()) as db:
            db.execute("UPDATE activities SET file_path='activities/sample.gpx'");db.commit()
            result=ensure_track_temperature(self.context,db,'sample')
            self.assertEqual(result['status'],'unavailable')
            self.assertIn('No recorded temperature',result['message'])
        self.assertEqual(source.read_bytes(),GPX)

    def test_fit_import_still_uses_fitparse_records(self):
        from types import SimpleNamespace
        source=self.root/'sample.fit';source.write_bytes(b'synthetic FIT placeholder')
        session={'start_time':'2026-01-01T10:00:00','sport':'cycling','total_timer_time':60,'total_distance':200}
        points=[{'position_lat':int(48*2**31/180),'position_long':int(11*2**31/180),'timestamp':'2026-01-01T10:00:00','altitude':500,'temperature':12}]
        fit=SimpleNamespace(get_messages=lambda kind:[SimpleNamespace(get_value=item.get) for item in ([session] if kind=='session' else points)])
        with patch('garmin_library.activities.fitparse.FitFile',return_value=fit), closing(self.context.connect()) as db:
            self.assertTrue(import_file(db,source))
            self.assertEqual(db.execute('SELECT sport FROM activities').fetchone()[0],'cycling')
            self.assertEqual(db.execute('SELECT temperature_c FROM track_points').fetchone()[0],12)

    def test_garmin_auth_download_and_import_with_mocked_network(self):
        import zipfile
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr('activity.gpx',GPX)
        context=AppContext(replace(self.context.settings,api_key='test-key'))
        with patch('garmin_library.garmin.api_request',side_effect=[b'one.two.three',json.dumps([{'activityId':123}]).encode(),archive.getvalue()]) as request, patch('garmin_library.garmin.time.sleep'), redirect_stdout(io.StringIO()):
            fetch_new(context)
        self.assertEqual(request.call_args_list[0].args[1:3],('test-key','POST'))
        self.assertEqual(request.call_args_list[0].kwargs['auth_scheme'],'ApiKey')
        self.assertEqual(request.call_args_list[1].args[1],'one.two.three')
        self.assertEqual((context.settings.activities_path/'123.gpx').read_bytes(),GPX)
        with closing(context.connect()) as db:self.assertEqual(db.execute('SELECT id FROM activities').fetchone()[0],'123')

    def test_garmin_retries_are_bounded_and_honor_authorization(self):
        url='https://example.invalid/resource'
        error=urllib.error.HTTPError(url,429,'Limited',{'Retry-After':'2'},None)
        with patch('garmin_library.garmin.urllib.request.urlopen',side_effect=[error,io.BytesIO(b'ok')]) as request, patch('garmin_library.garmin.time.sleep') as sleep, patch('garmin_library.garmin.random.uniform',return_value=0):
            self.assertEqual(api_request(url,'test-key',auth_scheme='ApiKey'),b'ok')
            self.assertEqual(request.call_args.args[0].get_header('Authorization'),'ApiKey test-key')
            sleep.assert_called_once_with(2)
        error=urllib.error.HTTPError(url,503,'Unavailable',{},None)
        with patch('garmin_library.garmin.urllib.request.urlopen',side_effect=error) as request, patch('garmin_library.garmin.time.sleep'):
            with self.assertRaises(urllib.error.HTTPError):api_request(url,'test-key',retries=2)
            self.assertEqual(request.call_count,3)

    def test_importing_application_has_no_initialization_side_effects(self):
        code='''from unittest.mock import patch
from garmin_library import config, database
with patch.object(config, "load_dotenv", side_effect=AssertionError("dotenv loaded")), patch.object(database, "connect", side_effect=AssertionError("database opened")), patch("threading.Thread.start", side_effect=AssertionError("worker started")), patch("urllib.request.urlopen", side_effect=AssertionError("network called")):
    import app
    import garmin_library.cli
    import garmin_library.http
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_distance_threshold_migration_preserves_settings_and_queues_once(self):
        with closing(sqlite3.connect(self.context.settings.database_path)) as db:
            db.executescript("""CREATE TABLE route_matching_state (
                id INTEGER PRIMARY KEY,tolerance_m REAL,full_percent REAL,partial_percent REAL,
                revision INTEGER,published_revision INTEGER,status TEXT,error TEXT,
                progress_done INTEGER,progress_total INTEGER);
                INSERT INTO route_matching_state VALUES(1,70,88,12,7,7,'completed',NULL,1,1);""")
        with closing(self.context.connect()) as db:
            state=dict(db.execute('SELECT * FROM route_matching_state').fetchone())
            self.assertEqual((state['full_km'],state['partial_km']),(20,5))
            self.assertEqual((state['full_percent'],state['partial_percent'],state['tolerance_m']),(88,12,70))
            self.assertEqual((state['revision'],state['published_revision'],state['status']),(8,7,'pending'))
            db.execute('UPDATE route_matching_state SET partial_km=NULL');db.commit()
        with closing(self.context.connect()) as db:
            state=dict(db.execute('SELECT * FROM route_matching_state').fetchone())
            self.assertEqual(state['revision'],8)
            self.assertIsNone(state['partial_km'])
