"""SQLite connection setup and additive schema upgrades."""
from pathlib import Path
import sqlite3


def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS activities (
        id TEXT PRIMARY KEY, file_path TEXT NOT NULL, start_time TEXT,
        sport TEXT, duration_s REAL, distance_m REAL, imported_at TEXT DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS track_points (
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        timestamp TEXT, altitude REAL, PRIMARY KEY(activity_id, seq)
      );
      CREATE INDEX IF NOT EXISTS track_activity_idx ON track_points(activity_id, seq);
      CREATE TABLE IF NOT EXISTS activity_temperature_cache (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
        file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
        status TEXT NOT NULL, message TEXT
      );
      CREATE TABLE IF NOT EXISTS planned_routes (
        id TEXT PRIMARY KEY, source_hash TEXT NOT NULL UNIQUE, stored_filename TEXT NOT NULL,
        original_filename TEXT NOT NULL, name TEXT NOT NULL, sport TEXT NOT NULL DEFAULT 'generic',
        notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        distance_m REAL NOT NULL, ascent_m REAL, elevation_coverage REAL NOT NULL,
        point_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS planned_route_points (
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        part INTEGER NOT NULL, seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        elevation REAL, PRIMARY KEY(route_id,part,seq)
      );
      CREATE TABLE IF NOT EXISTS planned_route_waypoints (
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, name TEXT, description TEXT,
        PRIMARY KEY(route_id,seq)
      );
      CREATE TABLE IF NOT EXISTS route_collections (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS route_collection_members (
        collection_id TEXT NOT NULL REFERENCES route_collections(id) ON DELETE CASCADE,
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, PRIMARY KEY(collection_id,route_id), UNIQUE(collection_id,position)
      );
      CREATE TABLE IF NOT EXISTS route_matching_state (
        id INTEGER PRIMARY KEY CHECK(id=1), tolerance_m REAL NOT NULL DEFAULT 50,
        full_percent REAL NOT NULL DEFAULT 90, partial_percent REAL NOT NULL DEFAULT 20,
        full_km REAL DEFAULT 20, partial_km REAL DEFAULT 5,
        revision INTEGER NOT NULL DEFAULT 1, published_revision INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT,
        progress_done INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0
      );
      INSERT OR IGNORE INTO route_matching_state(id) VALUES(1);
      CREATE TABLE IF NOT EXISTS route_track_matches (
        route_id TEXT NOT NULL REFERENCES planned_routes(id) ON DELETE CASCADE,
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        classification TEXT NOT NULL, route_coverage REAL NOT NULL, track_coverage REAL NOT NULL,
        shared_m REAL NOT NULL, direction TEXT NOT NULL, ranges TEXT NOT NULL,
        generation INTEGER NOT NULL, PRIMARY KEY(route_id,activity_id)
      );
      CREATE INDEX IF NOT EXISTS route_match_activity_idx ON route_track_matches(activity_id);
      CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, sport TEXT NOT NULL,
        radius_m REAL NOT NULL DEFAULT 30, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', error TEXT,
        progress_done INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS segment_checkpoints (
        segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
        PRIMARY KEY(segment_id,seq)
      );
      CREATE TABLE IF NOT EXISTS segment_traversals (
        id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL, start_pos REAL NOT NULL, end_pos REAL NOT NULL,
        start_time TEXT, end_time TEXT, duration_s REAL,
        UNIQUE(segment_id,activity_id,ordinal)
      );
      CREATE INDEX IF NOT EXISTS traversal_segment_idx ON segment_traversals(segment_id,duration_s);
      CREATE TABLE IF NOT EXISTS activity_bounds (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
        min_lat REAL, max_lat REAL, min_lon REAL, max_lon REAL
      );
      CREATE TABLE IF NOT EXISTS destination_state (
        id INTEGER PRIMARY KEY CHECK(id=1), epsilon_m REAL NOT NULL DEFAULT 75,
        min_trips INTEGER NOT NULL DEFAULT 3, revision INTEGER NOT NULL DEFAULT 1,
        published_revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
        phase TEXT NOT NULL DEFAULT 'endpoints', error TEXT, progress_done INTEGER NOT NULL DEFAULT 0,
        progress_total INTEGER NOT NULL DEFAULT 0, changed_at REAL NOT NULL DEFAULT 0
      );
      INSERT OR IGNORE INTO destination_state(id) VALUES(1);
      CREATE TABLE IF NOT EXISTS activity_endpoints (
        activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('start','end')), lat REAL NOT NULL, lon REAL NOT NULL,
        recorded_at TEXT, PRIMARY KEY(activity_id,role)
      );
      CREATE TABLE IF NOT EXISTS destination_endpoint_sources (
        activity_id TEXT PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS destinations (
        id TEXT PRIMARY KEY, name TEXT, seed_lat REAL NOT NULL, seed_lon REAL NOT NULL,
        centroid_lat REAL NOT NULL, centroid_lon REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS destination_memberships (
        activity_id TEXT NOT NULL, role TEXT NOT NULL, destination_id TEXT NOT NULL REFERENCES destinations(id),
        PRIMARY KEY(activity_id,role),
        FOREIGN KEY(activity_id,role) REFERENCES activity_endpoints(activity_id,role) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS destination_membership_idx ON destination_memberships(destination_id,activity_id);
      CREATE TRIGGER IF NOT EXISTS destinations_activity_insert AFTER INSERT ON activities BEGIN
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
      CREATE TRIGGER IF NOT EXISTS destinations_activity_delete AFTER DELETE ON activities BEGIN
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
      CREATE TRIGGER IF NOT EXISTS destinations_activity_update AFTER UPDATE OF start_time,sport ON activities BEGIN
        DELETE FROM activity_endpoints WHERE activity_id=NEW.id;
        DELETE FROM destination_endpoint_sources WHERE activity_id=NEW.id;
        UPDATE destination_state SET revision=revision+1,status='pending',error=NULL,
          changed_at=CAST(strftime('%s','now') AS REAL) WHERE id=1;
      END;
    """)
    db.execute("PRAGMA foreign_keys = ON")
    if "temperature_c" not in {row[1] for row in db.execute("PRAGMA table_info(track_points)")}:
        db.execute("BEGIN IMMEDIATE")
        if "temperature_c" not in {row[1] for row in db.execute("PRAGMA table_info(track_points)")}:
            db.execute("ALTER TABLE track_points ADD COLUMN temperature_c REAL")
        db.commit()
    distance_columns = {"full_km": "REAL DEFAULT 20", "partial_km": "REAL DEFAULT 5"}
    if set(distance_columns) - {row[1] for row in db.execute("PRAGMA table_info(route_matching_state)")}:
        db.execute("BEGIN IMMEDIATE")
        existing = {row[1] for row in db.execute("PRAGMA table_info(route_matching_state)")}
        missing = set(distance_columns) - existing
        for column in sorted(missing):
            db.execute(f"ALTER TABLE route_matching_state ADD COLUMN {column} {distance_columns[column]}")
        if missing:
            # Older caches excluded low-percentage overlaps; rebuild to discover them.
            db.execute("""UPDATE route_matching_state SET revision=revision+1,status='pending',error=NULL,
                progress_done=0,progress_total=0 WHERE id=1""")
        db.commit()
    setup_route_matching_queue(db)
    return db

def setup_route_matching_queue(db):
    """Install durable per-route jobs once, preserving completed legacy caches."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS route_matching_jobs(
            route_id TEXT PRIMARY KEY REFERENCES planned_routes(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending', error TEXT
        );
    """)
    migration = "route_matching_jobs_v1"
    if db.execute("SELECT 1 FROM app_migrations WHERE name=?", (migration,)).fetchone():
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM app_migrations WHERE name=?", (migration,)).fetchone():
            db.commit()
            return
        queue_all = """INSERT INTO route_matching_jobs(route_id) SELECT id FROM planned_routes WHERE 1
            ON CONFLICT(route_id) DO UPDATE SET revision=revision+1,status='pending',error=NULL;"""
        changed = """UPDATE route_matching_state SET revision=revision+1,status='pending',error=NULL,
            progress_done=0,progress_total=(SELECT COUNT(*) FROM route_matching_jobs) WHERE id=1;"""
        triggers = {
            "route_matching_activity_insert": ("INSERT", "activities", queue_all),
            "route_matching_activity_delete": ("DELETE", "activities", queue_all),
            "route_matching_route_insert": ("INSERT", "planned_routes",
                "INSERT INTO route_matching_jobs(route_id) VALUES(NEW.id);"),
            "route_matching_route_delete": ("DELETE", "planned_routes",
                "DELETE FROM route_matching_jobs WHERE route_id=OLD.id;"),
        }
        for name, (event, table, queue) in triggers.items():
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
            db.execute(f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN {queue} {changed} END")
        state = db.execute("SELECT status,revision,published_revision FROM route_matching_state WHERE id=1").fetchone()
        if state[0] != "completed" or state[1] != state[2]:
            # An unfinished legacy global run has no per-route completion markers.
            db.execute(queue_all)
            db.execute("""UPDATE route_matching_state SET status='pending',error=NULL,progress_done=0,
                progress_total=(SELECT COUNT(*) FROM route_matching_jobs) WHERE id=1""")
        db.execute("INSERT INTO app_migrations VALUES(?)", (migration,))
        db.commit()
    except Exception:
        db.rollback()
        raise
