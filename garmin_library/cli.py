"""Command-line parsing and application initialization."""
import argparse
import logging

from garmin_library.activities import import_all
from garmin_library.config import PROJECT_ROOT, Settings, load_dotenv
from garmin_library.context import AppContext
from garmin_library.exports import export_csv, export_map
from garmin_library.garmin import fetch_new
from garmin_library.server import serve


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env")
    context = AppContext(Settings.from_environment())
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import", help="Import FIT/GPX files from storage/activities/ (or GARMIN_ACTIVITIES_PATH)")
    sub.add_parser("fetch", help="Fetch new Garmin activities (requires GARMIN_API_KEY)")
    sub.add_parser("export-csv", help="Export session metadata to analysis/activities.csv")
    sub.add_parser("export-map", help="Export a static Folium map to analysis/activities_map.html")
    web = sub.add_parser("serve", help="Run the local activity map")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 inside Docker)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.command == "import":
        import_all(context)
    elif args.command == "fetch":
        fetch_new(context)
    elif args.command == "export-csv":
        export_csv(context)
    elif args.command == "export-map":
        export_map(context)
    else:
        serve(context, args.host, args.port)
