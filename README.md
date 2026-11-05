# Garmin Activity Library

This project stores Garmin FIT and GPX activities locally, indexes activity metadata and GPS points in SQLite, and provides a searchable web map. It also keeps planned GPX routes in ordered collections with planning notes. Activity source files live in `storage/activities/`. Imported activity data can be recreated from them; saved segments, destination identities/custom names, and route notes/order require a database backup. Planned routes also require their original uploads beside the database.

## Setup

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Import existing FIT files

Import all `*.fit` and `*.gpx` files in `storage/activities/` into the configured database (by default `activities.sqlite3`):

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

The fetch command saves supported FIT or GPX files to `storage/activities/` and imports newly downloaded files. GPX distance and duration are derived from its track points and timestamps. Garmin API key and token details are documented in the [Personal API guide](https://developer.garmin.com/gc-developer-program/personal/personal-api/) and [FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/).

## Start the web application

```bash
uv run python app.py serve
```

Open <http://127.0.0.1:8000>. Use the left navigation to switch between Summary, Heatmap, Tracks, Segments, Destinations, and Routes. The summary shows totals, GPS coverage, sports, and recent activities. The heatmap draws all activity tracks on a map with a sport filter; overlapping paths show frequently visited routes. Coordinates are sampled for responsiveness. Tracks shows an activity list alongside its map and identifies activities without GPS. Routes holds planned journeys and their collection order. To choose another port, for example `9000`:

```bash
uv run python app.py serve --port 9000
```

The map uses Leaflet and OpenStreetMap tiles and therefore needs an internet connection to display the base map. The application server listens on localhost.

To change the database location, set `GARMIN_DB_PATH` in `.env` or the environment. Relative paths are resolved from the project root and missing parent directories are created automatically. The default remains `activities.sqlite3` in the project root. `serve --host` changes the listening address; its default remains `127.0.0.1`.

In Summary, **Compare periods** lets you choose two years, months, or weeks and optionally filter by sport. It compares activity count, distance, and recorded time with bars, absolute changes, and percentage changes relative to period B. Weeks use ISO week numbers and run Monday–Sunday. Periods without activities show zero; missing dates are excluded. Ongoing periods and partially imported history may be incomplete.

Tracks includes an activity details panel with its matched segments and traversal times. Checkpoints and traversed portions are drawn above the main track with contrasting outlines, permanent name labels, and start/finish markers. Use **Show segments on map** to toggle them, or select a segment/pass in the details panel to zoom to it. Gold, silver, and bronze medals mark ranks 1, 2, and 3. Rankings compare each activity's best timed pass on that segment; repeated laps cannot occupy multiple podium places. Times tied to the nearest millisecond share a rank (competition ranking, such as 1, 1, 3). Single-point visits and untimed passes receive no medals. Pending matches refresh automatically while viewing the track.

In the heatmap, choose **Overlapping routes** for the density view or **Individual tracks** for more visible routes that highlight on hover. Click a route to open its full track in Tracks. The route color picker updates either heatmap mode immediately.

The Tracks activity list also shows gold, silver, and bronze medal icons for podium finishes on saved segments. Each badge puts the count before the icon (for example, `2 x` followed by a gold medal for two first-place segments); hover for the segment names. The abstract medal artwork also appears in track details and as compact 26 × 30 px map markers.

Enable **Color track by speed** in Tracks to color the route from blue (slow) through cyan, green, and yellow to red (fast). A legend shows the scale for the selected track, using its 95th-percentile speed as the red threshold so occasional GPS spikes do not dominate the colors. Hover over a colored section for its estimated speed range. Speeds are calculated from stored GPS coordinates and timestamps, so no reimport is needed. Missing/non-increasing timestamps, intervals over five minutes, and implausible speeds above 198 km/h appear gray. Existing GPS discontinuities remain disconnected, and segment overlays remain above the speed colors. Turn the option off to restore the plain route without changing the map view.

Enable **Color track by temperature** for blue-to-red coloring from the selected track's coldest to warmest recorded sensor readings, with a °C legend. Speed and temperature coloring are mutually exclusive; segment overlays remain independent. FIT record temperatures and Garmin GPX `atemp` extensions are supported. Each line section uses the mean of its available endpoint readings; sections with no readings are gray. Constant temperatures use a single color. Tracks with no usable temperature data retain the plain route with an unavailable message.

Temperature is stored in the database for new imports. For existing tracks, the first temperature request reads and caches the source FIT/GPX file without reimporting the activity or changing segment/destination identities. Source coordinates are checked against the stored track before readings are attached. No weather service or inferred outdoor temperature is used.

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

## Planned Routes

Open **Routes** to keep planned journeys separate from completed activities. Choose **Upload GPX** to upload one or several files. Each file can be up to 25 MiB and contain up to 200,000 points, including waypoints. GPX 1.0/1.1 tracks and routes are supported without timestamps; waypoint-only files are rejected. Track segments stay separate, and waypoints are shown as annotations rather than joined into the route line.

Create a named **collection** for a trip or cycling holiday. Uploading while a collection is selected appends routes to its order. Drag routes into position, or use the up/down arrows. Each collection has its own saved order and numbered map markers. **All routes** is the library rather than a manually ordered collection.

A route can belong to several collections. Select it in All routes and use **Add to collection** in its details to reuse it. Its name, sport, and planning notes are shared across those collections; the position is independent in each. Uploading an identical GPX reuses the existing route without replacing its metadata or notes.

The map shows the collection together and highlights the selected route. Use **Show collection routes**, **Fit collection**, or **Zoom to route** to choose the view. Large collection previews may be simplified, while selecting a route loads its full geometry. Downloads always retain every original point and extension.

Edit the selected route's name, sport, and plain-text notes, then choose **Save route**. Downloads include **Download original GPX** and **Download notes** as a separate text file. The GPX bytes remain exactly as uploaded, so renaming a route or editing notes does not change the original file's internal name or metadata. Distance is calculated within each track part; elevation gain is shown when available, with a partial-coverage label when needed.

**Remove** removes one collection membership. **Delete route from library** removes the route, its notes, its original upload, and every membership after confirmation. Deleting a collection preserves all routes and notes. Concurrent collection changes are detected; refresh the collection and retry a stale reorder.

Original uploads live in a `routes/` directory beside the configured database: normally `routes/` in the project root, or `storage/routes/` when using the container setup. The existing `storage/` mount already persists them. Back up **both the database and this directory** to preserve planned routes, notes, and collection order. For a consistent joint backup, stop the app before backing up the database with SQLite's backup API and copying its routes directory.

For Komoot, export a planned route as GPX and upload that file here. Download a GPX to import it as a **course** in Garmin Connect using [Garmin's course-import instructions](https://support.garmin.com/en-US/?faq=wKuZXCaZRP4mWPX5aRz5h5). This version does not connect directly to Komoot accounts or publish through Garmin APIs; the existing Garmin Personal API key is for pulling data. Planned routes do not affect activity statistics, segments, or destination discovery.

## Route matching

Tracks and Routes show **Full** and **Partial** matches between recorded activities and planned routes. Both views show the percentage of the planned route completed, the percentage of the recorded track shared, shared distance, and forward/reverse direction. A 100 km track that follows an entire 20 km planned route can be a full match, with about 20% track coverage. All sports are compared.

The defaults are **50 m GPS tolerance**, **90% route coverage for Full**, and **20% for Partial**. A match must also share at least **250 m**, or half the route length for routes shorter than 500 m, to exclude incidental crossings. Under **Route matching settings** in Routes, adjust tolerance (10–200 m) and percentages, then choose **Save and recalculate**. Settings are shared globally; partial must be below full. **Reload settings** resolves conflicting settings edits.

Select a match in the details list. Enable **Show route match on map** in Tracks, or **Show track match on map** in Routes, to display the selected counterpart as a purple dashed line with matched portions highlighted in green (Full) or amber (Partial). Both toggles start off. **Zoom to match** changes the map bounds; selecting or toggling a match preserves zoom. Use **Open route/track** to navigate to the counterpart. Lists are paginated and sorted by match classification, route coverage, then shared distance. Segment overlays and speed/temperature coloring remain independently controlled.

Matching samples continuous paths at intervals of at most 20 m and uses spatial radius searches followed by distance-weighted ordered alignment. It evaluates forward and reverse separately, supports closed loops starting at a different point, and counts each planned-route portion only once. Detours and missing portions do not contribute coverage. Separate GPX parts and existing GPS discontinuities are never joined by artificial lines. Coverage is an estimate: GPS tolerance can include nearby parallel paths, and very short routes are less distinctive. Original FIT/GPX files and stored activity points remain unchanged.

A background worker runs with the single app server. Route uploads, activity imports/replacements/deletions, and settings changes queue recalculation; editing route notes, names, sports, or collection order does not. SQLite caches results and matched ranges, protected by source/settings revisions. Previous results remain listed as updating, but outdated map geometry is withheld until recalculation finishes. Views refresh automatically; failed jobs offer **Retry matching**, and interrupted jobs resume on server startup. Back up the database to preserve matching settings; cached matches can be recalculated.

Synthetic algorithm and SQLite lifecycle scenarios are provided in `tests/test_route_matching.py`; they were not run during implementation, following project instructions. Additional acceptance scenarios for an explicitly authorized verification session are: concurrent imports/settings changes must prevent stale publication; restarting during processing must resume work; both views must show identical metrics; pagination, counterpart navigation, unsaved route edits, map toggles/zoom, and simultaneous segment/speed/temperature overlays must work together. Verify source files, segment definitions, destination identities, and route notes remain intact after recalculation.

## Run with Docker Compose

Install Docker with Compose support (for example Docker Desktop), then use the commands below from the project root. The container runs Python 3.12 with dependencies installed from `uv.lock` during the build; it does not install packages at startup.

### Prepare host folders and configuration

Persistent container data uses this layout:

```text
storage/
├── activities.sqlite3
├── activities/         # Original FIT/GPX activities
├── routes/             # Original planned GPX routes
└── backups/            # Dated database backups
```

If upgrading from the old root-level `activities/` folder, follow **Move an existing activity directory once** below before creating the destination directory.

```bash
mkdir -p storage/activities analysis
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

For a fresh setup, you can set this path without migration; the first server startup imports the mounted activity files. Compose overrides the database path inside the container to `/data/activities.sqlite3`. Original FIT/GPX files live in `storage/activities/` (mounted at `/data/activities`), and exports stay in `analysis/`.

### Move an existing activity directory once

With the app stopped, move the old root-level directory into storage. Do this before creating `storage/activities/`; never overwrite an existing destination:

```bash
docker compose stop app
mkdir -p storage
uv run python - <<'PY'
from pathlib import Path
source = Path("activities")
target = Path("storage/activities")
if not source.is_dir():
    raise SystemExit("No legacy activity directory to move.")
if target.exists():
    raise SystemExit("Destination exists; reconcile the directories without overwriting files.")
source.rename(target)
PY
docker compose up --build -d --force-recreate
```

Existing database references such as `activities/123.fit` resolve to the new directory automatically. No reimport or database replacement is needed. The container now uses only the `storage/` mount for persistent personal data, plus the separate `analysis/` mount for generated exports. Local commands default to `storage/activities/` even when using an older project-root database.

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

All persistent container data is under `storage/`: the database, `activities/`, `routes/`, and backups. Create a consistent SQLite backup in that folder:

```bash
docker compose exec app python -c 'import sqlite3; source=sqlite3.connect("/data/activities.sqlite3"); target=sqlite3.connect("/data/activities-backup.sqlite3"); source.backup(target); target.close(); source.close()'
```

The backup appears at `storage/activities-backup.sqlite3` on your computer. This command refreshes that backup file; copy dated backups elsewhere if you need history. Use SQLite's backup API rather than copying only the live database file, because committed data may still be in its WAL file. For a complete backup, stop the app and copy the whole `storage/` folder, including both original-file directories. Keep `storage/` private; it is ignored by Git.

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

- `storage/activities/` contains source FIT and GPX files. `GARMIN_ACTIVITIES_PATH` can override this directory for direct `uv` usage; relative values are resolved from the project root.
- The configured SQLite database contains the searchable activity index, track points, saved segment definitions, cached traversals, destination identities/names, and planned routes/notes/collection order. Back it up together with the adjacent `routes/` directory of original uploads. The default `activities.sqlite3`, root `routes/`, and Docker's `storage/` folder are ignored by Git. Ignore custom data locations too if they are inside the repository.
- `GARMIN_API_KEY` should be kept private and is never written by the application.
- `.env` is loaded automatically and is ignored by Git.
