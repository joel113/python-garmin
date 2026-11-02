# Garmin Activity Library

This project stores Garmin FIT and GPX activities locally, indexes activity metadata and GPS points in SQLite, and provides a searchable web map. Source files remain in `activities/`. Imported activity data can be recreated from them; saved segment definitions and destination identities/custom names require a database backup.

## Setup

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Import existing FIT files

Import all `*.fit` and `*.gpx` files in `activities/` into the configured database (by default `activities.sqlite3`):

```bash
uv run python app.py import
```

The import is safe to repeat. FIT files are keyed by their filename identifier and their metadata and track points are replaced on each import.

## Fetch new activities from Garmin

The Garmin Connect Personal API uses a Personal API key to obtain a short-lived bearer token. You can save your key in a `.env` file in the project root (already ignored by Git):

```dotenv
GARMIN_API_KEY=your-personal-api-key
```

The app reads `.env` automatically. A value already set in the shell takes precedence. Keep the key private and do not commit it. Then run:

```bash
uv run python app.py fetch
```

The fetch command saves supported FIT or GPX files to `activities/` and imports newly downloaded files. GPX distance and duration are derived from its track points and timestamps. Garmin API key and token details are documented in the [Personal API guide](https://developer.garmin.com/gc-developer-program/personal/personal-api/) and [FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/).

## Start the web application

```bash
uv run python app.py serve
```

Open <http://127.0.0.1:8000>. Use the left navigation to switch between Summary, Heatmap, Tracks, Segments, and Destinations. The summary shows totals, GPS coverage, sports, and recent activities. The heatmap draws all routes on a map with a sport filter; overlapping paths show frequently visited routes. Route coordinates are sampled for responsiveness. Tracks shows an activity list alongside its map and identifies activities without GPS. To choose another port, for example `9000`:

```bash
uv run python app.py serve --port 9000
```

The map uses Leaflet and OpenStreetMap tiles and therefore needs an internet connection to display the base map. The application server listens on localhost.

To change the database location, set `GARMIN_DB_PATH` in `.env` or the environment. Relative paths are resolved from the project root and missing parent directories are created automatically. The default remains `activities.sqlite3` in the project root. `serve --host` changes the listening address; its default remains `127.0.0.1`.

In Summary, **Compare periods** lets you choose two years, months, or weeks and optionally filter by sport. It compares activity count, distance, and recorded time with bars, absolute changes, and percentage changes relative to period B. Weeks use ISO week numbers and run Monday–Sunday. Periods without activities show zero; missing dates are excluded. Ongoing periods and partially imported history may be incomplete.

Tracks includes an activity details panel with its matched segments and traversal times. Checkpoints and traversed portions are drawn above the main track with contrasting outlines, permanent name labels, and start/finish markers. Use **Show segments on map** to toggle them, or select a segment/pass in the details panel to zoom to it. Gold, silver, and bronze medals mark ranks 1, 2, and 3. Rankings compare each activity's best timed pass on that segment; repeated laps cannot occupy multiple podium places. Times tied to the nearest millisecond share a rank (competition ranking, such as 1, 1, 3). Single-point visits and untimed passes receive no medals. Pending matches refresh automatically while viewing the track.

In the heatmap, choose **Overlapping routes** for the density view or **Individual tracks** for more visible routes that highlight on hover. Click a route to open its full track in Tracks. The route color picker updates either heatmap mode immediately.

The Tracks activity list also shows gold, silver, and bronze medal icons for podium finishes on saved segments. Each badge puts the count before the icon (for example, `2 x` followed by a gold medal for two first-place segments); hover for the segment names. The abstract medal artwork also appears in track details and as compact 26 × 30 px map markers.

The heatmap uses an approximate budget of 800,000 coordinates across the selected sports, with reduced map simplification to preserve curves when zooming in. Larger selections can take longer to load; the Tracks view uses all stored coordinates.

Both map views split routes at GPS discontinuities before sampling. Connections are omitted when timestamps go backwards, a gap exceeds five minutes with more than 500 m of displacement, or a jump exceeds 300 m at over 55 m/s (198 km/h). Without timestamps, jumps over 10 km are split. Isolated points are omitted from the display. Source files and database records are preserved; this is a display heuristic, and unusually fast activities or long GPS gaps may be split too.

## Segments

Open **Segments** in the left navigation and choose **New segment**. Enter a name, choose an existing sport, and click the map to place numbered checkpoints. Drag markers to move them, remove checkpoints from the list, or undo checkpoint changes. Save to start automatic matching. You can edit or delete saved segments from their detail view.

A single checkpoint counts visits. Two or more checkpoints match forward traversals through every checkpoint in order and rank elapsed time from the first checkpoint's entry to the last. A checkpoint is a circle with an editable tolerance of 5–500 metres (30 m by default). Tracks may cross circles between recorded points: crossing times are interpolated. Leaving 1.5 times the radius rearms a checkpoint, avoiding repeated visits from GPS jitter. Repeated laps count separately. Missing timestamps produce untimed traversals; stops are included in elapsed time. GPS discontinuities split matching, just as in the map views.

Matching uses exact sport values: cycling segments match cycling activities, and GPX tracks with unknown sport match only segments assigned to `gpx`. Results show traversal and activity counts, the fastest timed pass, matching activity routes, and a paginated traversal list. Select a traversal to highlight it, or open the full activity. Checkpoints do not require following the straight line between them.

Matching runs in a background worker while the app server is running; the first startup builds bounding boxes for existing activities. Imports made offline queue work for the next server start. Imports and edits invalidate affected results; the UI displays progress and automatically refreshes. Run only one application server per database so it owns the matching worker.

**Back up your database: segment definitions cannot be recreated from FIT/GPX files.** Use SQLite's backup API to include committed WAL data safely, even while the app is running. This example backs up the default project-root database; for a migrated Docker database, use the container backup command below.

```bash
uv run python -c 'import sqlite3; source=sqlite3.connect("activities.sqlite3"); target=sqlite3.connect("activities-backup.sqlite3"); source.backup(target); target.close(); source.close()'
```

To restore, stop the application, use SQLite's backup API from the backup file into the destination database, and restart the app. Keep backups private and outside Git.

## Destinations

Open **Destinations** to discover frequently visited places from trip start and end locations. The map and ranked list use distinct trips: a round trip counts once toward popularity but adds both a departure and an arrival. Select a place to see its centroid, member endpoints, and paginated trip list. Clicking a trip or endpoint opens Tracks. Green dots indicate departures and orange dots indicate arrivals.

Clustering uses scikit-learn DBSCAN with haversine distance on coordinates in radians. The defaults are a **75 m neighborhood distance** and **three distinct trips**. DBSCAN uses three endpoint samples as its density minimum, followed by a distinct-trip check; isolated stops and clusters with too few trips remain unassigned. Exact duplicate coordinates use sample weights to reduce memory without changing density semantics. Neighbor chains can connect areas larger than the configured distance.

Under **Clustering settings**, adjust the neighborhood distance (10–1000 m) and minimum distinct trips (2–100), then choose **Recluster destinations**. All sports, including GPX, contribute to shared places. The sport filter changes displayed counts and associated trips without changing the clusters or their identities.

First startup prepares endpoints from existing activities in the background. Endpoint extraction ignores pre-activity timestamps, invalid coordinates, isolated GPS points, and existing GPS discontinuities. Trips without a usable continuous track are excluded. Imports, replacements, and deletions automatically queue processing after a short quiet period. The UI shows progress, preserves previous results while updating, and refreshes automatically. If processing fails, use Recluster destinations to retry. Processing requires the app server to be running.

New places receive a coordinate-based ID such as `dest_48.1672_11.5354`, rounded to four decimal places (about 11 m of latitude). The full-precision spherical centroid is stored separately and can move while the ID stays fixed. Updates match old and new clusters by shared endpoints first and proximity second. The largest matching child retains an ID after a split; a merge retains the strongest overlapping identity. Coordinate collisions receive a deterministic suffix. Retired IDs are not recycled.

You can save an optional custom name such as Home or Office. Clearing the name returns to displaying the geographic ID. Names survive normal reclustering. Retired destinations and names remain in the database but do not appear in the active list. Back up the SQLite database to preserve names and identity history; source FIT/GPX files alone cannot reconstruct them. No reverse-geocoding service or Garmin request is used for destination discovery.

## Run with Docker Compose

Install Docker with Compose support (for example Docker Desktop), then use the commands below from the project root. The container runs Python 3.12 with dependencies installed from `uv.lock` during the build; it does not install packages at startup.

### Prepare host folders and configuration

```bash
mkdir -p storage activities analysis
id -u
id -g
```

Set `LOCAL_UID` and `LOCAL_GID` in your existing `.env` to those two numbers so the non-root container can write to your host folders. See `.env.example` for all settings; copy it only if you do not already have a `.env`.

```dotenv
APP_PORT=8001
LOCAL_UID=1000
LOCAL_GID=1000
```

Use your actual user/group numbers (macOS commonly differs from 1000). Existing files must be readable and the mounted folders and database must be writable by that user. Docker receives the optional Garmin key through runtime environment variables; `.env`, databases, and activity files are excluded from the image.

### Migrate an existing database once

Stop the current app server before switching to Docker. If you already have `activities.sqlite3`, copy it with SQLite's backup API so saved segments and any committed WAL data are preserved:

```bash
uv run python - <<'PY'
import sqlite3
from contextlib import closing
from pathlib import Path

source = Path("activities.sqlite3").resolve()
target = Path("storage/activities.sqlite3")
target.parent.mkdir(parents=True, exist_ok=True)
if not source.is_file():
    raise SystemExit("No existing database found; skip migration for a fresh setup.")
if target.exists():
    raise SystemExit("Destination already exists; migration cancelled to avoid overwriting it.")
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
print("Database copied. The original was preserved.")
PY
```

After migration, add the following to `.env` so your local `uv` commands use the same database as Docker:

```dotenv
GARMIN_DB_PATH=storage/activities.sqlite3
```

For a fresh setup, you can set this path without migration; the first server startup imports the mounted activity files. Compose overrides the database path inside the container to `/data/activities.sqlite3`. Original FIT/GPX files stay in `activities/`, and exports stay in `analysis/`.

### Start and manage the container

```bash
docker compose up --build -d
docker compose logs -f app
docker compose ps
```

Open <http://127.0.0.1:8001>. Set `APP_PORT` in `.env` to change this port, then recreate the container with `docker compose up -d`. Compose allows segment editing at both `localhost` and `127.0.0.1` on the published port, while keeping cross-origin mutations blocked. The container listens on `0.0.0.0:8000` internally; the published port is bound only to your computer's localhost.

Run CLI commands against the same mounted data:

```bash
docker compose run --rm app import
docker compose run --rm app fetch
docker compose run --rm app export-csv
docker compose run --rm app export-map
```

These commands do not start another web server or matching worker. Fetching requires a valid `GARMIN_API_KEY` in your runtime environment or Compose's `.env`. The running server automatically matches newly imported activities to saved segments.

To rebuild after changing the application, use `docker compose up --build -d`. To stop it, use:

```bash
docker compose down
```

Host folders are preserved when containers are removed. The app handles SIGTERM and gives the matching worker time to stop; interrupted jobs resume on the next startup. The container has a health check, and the restart policy is `unless-stopped`. A first-time bulk import may remain in the health check's starting state for several minutes.

Run only one server per database. Stop the container before running a local `uv run python app.py serve` against the same database. Prefer `docker compose run` for CLI operations while using Docker. Stop the container before local `uv` commands that modify its database, avoiding concurrent SQLite access across the host/container boundary.

### Back up container data

Back up `activities/` and create a consistent SQLite backup in the mounted storage folder:

```bash
docker compose exec app python -c 'import sqlite3; source=sqlite3.connect("/data/activities.sqlite3"); target=sqlite3.connect("/data/activities-backup.sqlite3"); source.backup(target); target.close(); source.close()'
```

The backup appears at `storage/activities-backup.sqlite3` on your computer. This command refreshes that backup file; copy dated backups elsewhere if you need history. Use SQLite's backup API rather than copying only the live database file, because committed data may still be in its WAL file. Keep `storage/` private; it is ignored by Git.

## Optional exports for analysis

Generate the legacy CSV of session metadata with:

```bash
uv run python app.py export-csv
```

Generate a standalone Folium map from all FIT files with:

```bash
uv run python app.py export-map
```

These commands write `analysis/activities.csv` and `analysis/activities_map.html` respectively.

## Data and secrets

- `activities/` contains source FIT and GPX files.
- The configured SQLite database contains the searchable activity index, track points, saved segment definitions, cached traversals, and destination identities, names, and endpoint memberships. The default `activities.sqlite3` and Docker's `storage/` folder are ignored by Git and should be backed up. Ignore custom database locations too if they are inside the repository.
- `GARMIN_API_KEY` should be kept private and is never written by the application.
- `.env` is loaded automatically and is ignored by Git.
