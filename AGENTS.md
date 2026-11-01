# Project guidance

## Project overview

This is a local Garmin activity library. `app.py` imports and fetches FIT/GPX activities, stores metadata and GPS points in SQLite, exports analysis files, and serves the browser application.

## Environment and commands

- This is a `uv` project. Install dependencies with `uv sync` and run commands with `uv run python app.py ...`.
- Use `.venv/bin/python` when directly invoking the project's interpreter in this environment.
- Main commands: `import`, `fetch`, `serve`, `export-csv`, and `export-map`. See `README.md` for usage.
- Do not run a live Garmin fetch unless explicitly requested; it uses the user's personal API credentials and makes network requests.
- Do not run tests or verification commands unless the user asks for them.

## Data and secrets

- FIT and GPX source files live in `activities/`; preserve them when changing derived data.
- `activities.sqlite3` is a rebuildable local index of activity metadata and track points. Rebuild it from source files with `uv run python app.py import`.
- Keep API credentials in `.env` or the shell environment. Never print, commit, or copy credentials into source code. `.env` and the SQLite database are Git-ignored.
- The web server binds to localhost by default.

## Implementation conventions

- Keep activity parsing and CLI behavior in `app.py`; keep browser assets in `web/`.
- Support both FIT and GPX in import, fetch, and export paths when changing activity handling.
- Preserve the existing SQLite schema unless a migration is required; if changing it, make database setup safely upgrade existing local databases.
- Keep Garmin API requests paced and retain bounded retry/backoff for HTTP 429 and transient 5xx responses.
- Keep generated CSV and static map outputs under `analysis/`; the interactive app uses `web/` and the SQLite database.
- Update `README.md` when commands, data formats, or setup steps change.
