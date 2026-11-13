"""Additive SQLite schema for derived street/path coverage."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS network_state(
 id INTEGER PRIMARY KEY CHECK(id=1), active_generation TEXT, staging_generation TEXT,
 revision INTEGER NOT NULL DEFAULT 1, data_revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'unconfigured', phase TEXT NOT NULL DEFAULT '', error TEXT,
 progress_done INTEGER NOT NULL DEFAULT 0, progress_total INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO network_state(id) VALUES(1);
CREATE TABLE IF NOT EXISTS network_generations(
 id TEXT PRIMARY KEY, regions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, error TEXT, source_manifest TEXT, scales TEXT);
CREATE TABLE IF NOT EXISTS network_source_versions(activity_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS network_jobs(
 generation TEXT NOT NULL REFERENCES network_generations(id) ON DELETE CASCADE,
 activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 matched_points INTEGER NOT NULL DEFAULT 0, unmatched_points INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(generation,activity_id));
CREATE TABLE IF NOT EXISTS network_sections(
 id INTEGER PRIMARY KEY, generation TEXT NOT NULL REFERENCES network_generations(id) ON DELETE CASCADE,
 way_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, name TEXT NOT NULL, road_class TEXT NOT NULL,
 cycling INTEGER NOT NULL, running INTEGER NOT NULL, geometry TEXT NOT NULL, length_m REAL NOT NULL,
 UNIQUE(generation,way_id,ordinal));
CREATE INDEX IF NOT EXISTS network_section_way_idx ON network_sections(generation,way_id);
CREATE VIRTUAL TABLE IF NOT EXISTS network_bounds USING rtree(id,min_lon,max_lon,min_lat,max_lat);
CREATE TRIGGER IF NOT EXISTS network_section_delete AFTER DELETE ON network_sections BEGIN
 DELETE FROM network_bounds WHERE id=OLD.id;
END;
CREATE TABLE IF NOT EXISTS network_contributions(
 generation TEXT NOT NULL, activity_id TEXT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
 section_id INTEGER NOT NULL REFERENCES network_sections(id) ON DELETE CASCADE,
 ranges TEXT NOT NULL, timed_m REAL NOT NULL, elapsed_s REAL NOT NULL,
 PRIMARY KEY(generation,activity_id,section_id));
CREATE INDEX IF NOT EXISTS network_contribution_section_idx
 ON network_contributions(generation,section_id,activity_id);
CREATE TABLE IF NOT EXISTS network_stats(
 section_id INTEGER NOT NULL REFERENCES network_sections(id) ON DELETE CASCADE,
 sport TEXT NOT NULL, activity_count INTEGER NOT NULL, speed_kmh REAL,
 speed_samples INTEGER NOT NULL, ranges TEXT NOT NULL, PRIMARY KEY(section_id,sport));
CREATE TABLE IF NOT EXISTS network_dirty(section_id INTEGER PRIMARY KEY REFERENCES network_sections(id) ON DELETE CASCADE);
CREATE TRIGGER IF NOT EXISTS network_contribution_insert AFTER INSERT ON network_contributions BEGIN
 INSERT OR IGNORE INTO network_dirty VALUES(NEW.section_id);
END;
CREATE TRIGGER IF NOT EXISTS network_contribution_update AFTER UPDATE ON network_contributions BEGIN
 INSERT OR IGNORE INTO network_dirty VALUES(NEW.section_id);
END;
CREATE TRIGGER IF NOT EXISTS network_contribution_delete AFTER DELETE ON network_contributions
 WHEN EXISTS(SELECT 1 FROM network_sections WHERE id=OLD.section_id) BEGIN
 INSERT OR IGNORE INTO network_dirty VALUES(OLD.section_id);
END;
CREATE TRIGGER IF NOT EXISTS network_activity_insert AFTER INSERT ON activities BEGIN
 INSERT INTO network_source_versions VALUES(NEW.id,1)
 ON CONFLICT(activity_id) DO UPDATE SET revision=revision+1;
 INSERT INTO network_jobs(generation,activity_id,revision)
 SELECT id,NEW.id,(SELECT revision FROM network_source_versions WHERE activity_id=NEW.id)
 FROM network_generations WHERE status IN ('matching','published') AND NEW.sport IN ('cycling','running');
END;
CREATE TRIGGER IF NOT EXISTS network_activity_delete AFTER DELETE ON activities BEGIN
 INSERT INTO network_source_versions VALUES(OLD.id,1)
 ON CONFLICT(activity_id) DO UPDATE SET revision=revision+1;
END;
"""


def setup_network_schema(db):
    db.executescript(SCHEMA)
    columns={"mode":"TEXT NOT NULL DEFAULT 'regions'","buffer_km":"REAL DEFAULT 2",
             "footprint":"TEXT","context_geometry":"TEXT","published_at":"TEXT","sources_cleaned":"INTEGER NOT NULL DEFAULT 0","retained_bytes":"INTEGER"}
    missing=set(columns)-{row[1] for row in db.execute('PRAGMA table_info(network_generations)')}
    job_missing='outside_coverage' not in {row[1] for row in db.execute('PRAGMA table_info(network_jobs)')}
    if missing or job_missing:
        db.execute('BEGIN IMMEDIATE')
        present={row[1] for row in db.execute('PRAGMA table_info(network_generations)')}
        for name,definition in columns.items():
            if name not in present:db.execute(f'ALTER TABLE network_generations ADD COLUMN {name} {definition}')
        if 'outside_coverage' not in {row[1] for row in db.execute('PRAGMA table_info(network_jobs)')}:
            db.execute('ALTER TABLE network_jobs ADD COLUMN outside_coverage INTEGER NOT NULL DEFAULT 0')
        db.commit()
    db.executescript("""CREATE TABLE IF NOT EXISTS network_previews(
        id TEXT PRIMARY KEY,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        fingerprint TEXT NOT NULL,buffer_km REAL NOT NULL,footprint TEXT NOT NULL,
        context_geometry TEXT NOT NULL,regions TEXT NOT NULL,summary TEXT NOT NULL);""")
