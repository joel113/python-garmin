# Garmin Activity Library

This project stores Garmin FIT and GPX activities locally, indexes activity metadata and GPS points in SQLite, and provides a searchable web map. Source files remain in `activities/`. Imported activity data can be recreated from them; saved segment definitions are user-created data and require a database backup.

## Setup

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Import existing FIT files

Import all `*.fit` and `*.gpx` files in `activities/` into `activities.sqlite3`:

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

Open <http://127.0.0.1:8000>. Use the left navigation to switch between Summary, Heatmap, Tracks, and Segments. The summary shows totals, GPS coverage, sports, and recent activities. The heatmap draws all routes on a map with a sport filter; overlapping paths show frequently visited routes. Route coordinates are sampled for responsiveness. Tracks shows an activity list alongside its map and identifies activities without GPS. To choose another port, for example `9000`:

```bash
uv run python app.py serve --port 9000
```

The map uses Leaflet and OpenStreetMap tiles and therefore needs an internet connection to display the base map. The application server listens on localhost.

In Summary, **Compare periods** lets you choose two years, months, or weeks and optionally filter by sport. It compares activity count, distance, and recorded time with bars, absolute changes, and percentage changes relative to period B. Weeks use ISO week numbers and run Monday–Sunday. Periods without activities show zero; missing dates are excluded. Ongoing periods and partially imported history may be incomplete.

In the heatmap, choose **Overlapping routes** for the density view or **Individual tracks** for more visible routes that highlight on hover. Click a route to open its full track in Tracks. The route color picker updates either heatmap mode immediately.

The heatmap uses an approximate budget of 800,000 coordinates across the selected sports, with reduced map simplification to preserve curves when zooming in. Larger selections can take longer to load; the Tracks view uses all stored coordinates.

Both map views split routes at GPS discontinuities before sampling. Connections are omitted when timestamps go backwards, a gap exceeds five minutes with more than 500 m of displacement, or a jump exceeds 300 m at over 55 m/s (198 km/h). Without timestamps, jumps over 10 km are split. Isolated points are omitted from the display. Source files and database records are preserved; this is a display heuristic, and unusually fast activities or long GPS gaps may be split too.

## Segments

Open **Segments** in the left navigation and choose **New segment**. Enter a name, choose an existing sport, and click the map to place numbered checkpoints. Drag markers to move them, remove checkpoints from the list, or undo checkpoint changes. Save to start automatic matching. You can edit or delete saved segments from their detail view.

A single checkpoint counts visits. Two or more checkpoints match forward traversals through every checkpoint in order and rank elapsed time from the first checkpoint's entry to the last. A checkpoint is a circle with an editable tolerance of 5–500 metres (30 m by default). Tracks may cross circles between recorded points: crossing times are interpolated. Leaving 1.5 times the radius rearms a checkpoint, avoiding repeated visits from GPS jitter. Repeated laps count separately. Missing timestamps produce untimed traversals; stops are included in elapsed time. GPS discontinuities split matching, just as in the map views.

Matching uses exact sport values: cycling segments match cycling activities, and GPX tracks with unknown sport match only segments assigned to `gpx`. Results show traversal and activity counts, the fastest timed pass, matching activity routes, and a paginated traversal list. Select a traversal to highlight it, or open the full activity. Checkpoints do not require following the straight line between them.

Matching runs in a background worker while the app server is running; the first startup builds bounding boxes for existing activities. Imports made offline queue work for the next server start. Imports and edits invalidate affected results; the UI displays progress and automatically refreshes. Run only one application server per database so it owns the matching worker.

**Back up `activities.sqlite3`: segment definitions cannot be recreated from FIT/GPX files.** Use SQLite's backup API to include committed WAL data safely, even while the app is running:

```bash
uv run python -c 'import sqlite3; source=sqlite3.connect("activities.sqlite3"); target=sqlite3.connect("activities-backup.sqlite3"); source.backup(target); target.close(); source.close()'
```

To restore, stop the application, use SQLite's backup API from the backup file into the destination database, and restart the app. Keep backups private and outside Git.

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
- `activities.sqlite3` contains the searchable activity index, track points, saved segment definitions, and cached traversals; it is ignored by Git and should be backed up.
- `GARMIN_API_KEY` should be kept private and is never written by the application.
- `.env` is loaded automatically and is ignored by Git.
