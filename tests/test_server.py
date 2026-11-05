"""Real worker startup and SIGTERM shutdown with synthetic local storage."""
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

from garmin_library.activities import import_file
from garmin_library.routes import store_planned_route
from support import GPX, TemporaryLibrary


class ServerLifecycleTests(TemporaryLibrary, unittest.TestCase):
    def test_workers_publish_and_exit_on_sigterm(self):
        folder=self.context.settings.activities_path;folder.mkdir(parents=True)
        source=folder/'trip.gpx';source.write_bytes(GPX)
        with closing(self.context.connect()) as db:
            self.assertTrue(import_file(db,source))
            store_planned_route(self.context,db,GPX,'planned.gpx')
            db.execute("INSERT INTO segments(name,sport,radius_m) VALUES('Visit','gpx',30)")
            sid=db.execute('SELECT id FROM segments').fetchone()[0]
            db.execute('INSERT INTO segment_checkpoints VALUES(?,0,48,11)',(sid,))
            db.commit()
        code='''import sys
from pathlib import Path
from garmin_library.config import Settings
from garmin_library.context import AppContext
from garmin_library.server import serve
serve(AppContext(Settings.from_environment({},root=Path(sys.argv[1]))),"127.0.0.1",0)
'''
        process=subprocess.Popen([sys.executable,'-u','-c',code,str(self.root)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        started=queue.Queue()
        threading.Thread(target=lambda:started.put(process.stdout.readline()),daemon=True).start()
        self.assertIn('Listening on',started.get(timeout=15))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if process.poll() is not None:self.fail('Server exited: '+process.stderr.read())
            with closing(self.context.connect()) as db:
                statuses=[db.execute('SELECT status FROM '+table).fetchone()[0]
                          for table in ('segments','destination_state','route_matching_state')]
                if statuses==['completed']*3:
                    self.assertEqual(db.execute('SELECT classification FROM route_track_matches').fetchone()[0],'full')
                    break
            time.sleep(.1)
        else:self.fail('Workers did not finish: '+repr(statuses))
        process.send_signal(signal.SIGTERM)
        stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,stderr)
        self.assertNotIn('Traceback',stderr)
        self.assertEqual(source.read_bytes(),GPX)
