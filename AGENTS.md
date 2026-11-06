# Project guidance

## Project overview

This is a local Garmin activity library. `app.py` is the CLI entry point; `garmin_library/` implements FIT/GPX import/fetch, SQLite persistence, exports, matching workers, and the HTTP server. Browser assets live in `web/`.

## Environment and commands

- This is a `uv` project. Install dependencies with `uv sync` and run commands with `uv run python app.py ...`.
- Use `.venv/bin/python` when directly invoking the project's interpreter in this environment.
- Main commands: `import`, `fetch`, `serve`, `export-csv`, and `export-map`. See `README.md` for usage.
- Do not run a live Garmin fetch unless explicitly requested; it uses the user's personal API credentials and makes network requests.
- Do not run tests or verification commands unless the user asks for them.

## Data and secrets

- FIT and GPX source files live in `storage/activities/` (or `GARMIN_ACTIVITIES_PATH`); preserve them when changing derived data. Legacy `activities/...` database references resolve to the configured activity directory without reimporting.
- `activities.sqlite3` holds imported activity data, user-created segments, and destination identities/custom names. Imports can be rebuilt with `uv run python app.py import`; segment definitions and destination names/history require a database backup. Never delete or replace the database to rebuild activities.
- Keep API credentials in `.env` or the shell environment. Never print, commit, or copy credentials into source code. `.env` and the SQLite database are Git-ignored.
- The web server binds to localhost by default.
- Docker Compose mounts `storage/` at `/data` for SQLite, activity originals, planned route originals, and backups; `analysis/` is mounted separately. It publishes only localhost port 8001 by default. `GARMIN_DB_PATH` selects a database, `GARMIN_ACTIVITIES_PATH` selects source activities, and `GARMIN_ALLOWED_HOSTS` controls permitted mutation hosts when ports are mapped. Never bake credentials or personal data into the image.

## Implementation conventions

- Keep `app.py` as a small CLI entry point. Put backend behavior in the owning `garmin_library` feature module, HTTP endpoints in `garmin_library/http/`, and browser assets in `web/`.
- Shared config, context, database, errors, and geometry modules must not import feature modules. Features must not import HTTP, server, CLI, or `app.py`. Pass `AppContext` explicitly to code that opens connections or uses configured paths; never restore mutable path globals. Each request/worker owns its SQLite connections.
- Load `.env` only at CLI initialization. Importing backend modules must not read credentials, create databases, start workers, or make network requests. For authorized tests, use explicit temporary settings and mocked Garmin responses.
- Support both FIT and GPX in import, fetch, and export paths when changing activity handling.
- Preserve the existing SQLite schema unless a migration is required; if changing it, make database setup safely upgrade existing local databases.
- Keep Garmin API requests paced and retain bounded retry/backoff for HTTP 429 and transient 5xx responses.
- The Segments view uses ordered checkpoint matching with cached traversals and a background worker owned by the single local app server. Preserve segment definitions when importing activities.
- Destinations uses scikit-learn DBSCAN on trip endpoints, shared across sports. Preserve coordinate-derived IDs and custom names during reclustering. A separate background worker publishes revision-checked results; previous results remain readable while updating.
- Planned Routes are independent of recorded activities. Original GPX uploads live in `DB_PATH.parent / "routes"`; back up that directory with SQLite. Preserve shared route notes, independent collection order, and revision checks when changing route membership or ordering. Do not silently modify the original GPX on download.
- Route matching compares planned routes to activity tracks across sports using ordered forward/reverse alignment. A single server worker processes durable per-route jobs and publishes revision-checked cached results. New uploads queue only the new route; activity/settings changes and explicit retries queue all routes. Preserve completed caches and usable geometry for unaffected routes, global settings, source-part boundaries, original files, and stale-result protections. Notes and collection ordering must not invalidate geometry matching. Full/Partial classification uses route percentage OR shared kilometres for each level, checking Full first; nullable kilometre settings disable that criterion. Preserve the minimum-overlap safeguard.
- Keep generated CSV and static map outputs under `analysis/`; the interactive app uses `web/` and the SQLite database.
- Update `README.md` when commands, data formats, or setup steps change.
