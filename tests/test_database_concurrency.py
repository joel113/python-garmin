"""Fresh CLI processes must read initialized libraries while a worker writes."""
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing

from support import TemporaryLibrary


class DatabaseConcurrencyTests(TemporaryLibrary, unittest.TestCase):
    def test_fresh_process_opens_initialized_database_during_write(self):
        with closing(self.context.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
        # A separate writer simulates the matching worker. The new interpreter
        # has no in-process schema cache and must not attempt schema writes.
        with closing(sqlite3.connect(self.context.settings.database_path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO activities(id,file_path) VALUES('pending','pending.fit')")
            try:
                completed = subprocess.run([sys.executable, '-c', '''
from pathlib import Path
from contextlib import closing
from garmin_library.database import connect
import sys
with closing(connect(Path(sys.argv[1]))) as db:
    assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 0
    assert not db.in_transaction
''', str(self.context.settings.database_path)], capture_output=True, text=True, timeout=10)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            finally:
                writer.rollback()

    def test_missing_schema_object_still_runs_additive_setup(self):
        with closing(self.context.connect()) as db:
            db.execute("INSERT INTO activities(id,file_path) VALUES('kept','kept.gpx')")
            db.execute('DROP TABLE activity_workouts')
            db.execute('DROP INDEX route_match_activity_idx')
            db.commit()
        with closing(self.context.connect()) as db:
            self.assertEqual(db.execute("SELECT id FROM activities").fetchone()[0], 'kept')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_workouts').fetchone()[0], 0)
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='route_match_activity_idx'").fetchone())
