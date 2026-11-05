"""HTTP transport and dispatch; feature endpoints live in neighboring modules."""
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler
from urllib.parse import quote, urlsplit
import json

from garmin_library.errors import AppError
from garmin_library.http import activities, destinations, route_matching, routes, segments


class Handler(SimpleHTTPRequestHandler):
    @property
    def context(self):
        return self.server.context

    def __init__(self, *args, **kwargs):
        # BaseRequestHandler sets self.server only after this initializer starts.
        server = args[2] if len(args) > 2 else kwargs["server"]
        super().__init__(*args, directory=str(server.context.settings.web_path), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/route-matching"):
            return route_matching.handle(self, "GET")
        if path.startswith(("/api/routes", "/api/route-collections")):
            return routes.handle(self, "GET")
        if path.startswith("/api/destinations"):
            return destinations.handle(self, "GET")
        if path.startswith("/api/segments"):
            return segments.handle(self, "GET")
        if self.path.startswith(("/api/activities", "/api/heatmap", "/api/track/")):
            return activities.handle(self)
        if self.path == "/" or self.path.startswith("/?"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        self.mutation_request("POST")

    def do_PUT(self):
        self.mutation_request("PUT")

    def do_DELETE(self):
        self.mutation_request("DELETE")

    def mutation_request(self, method):
        path = urlsplit(self.path).path
        if path.startswith("/api/route-matching"):
            return route_matching.handle(self, method)
        if path.startswith(("/api/routes", "/api/route-collections")):
            return routes.handle(self, method)
        if path.startswith("/api/destinations"):
            return destinations.handle(self, method)
        return segments.handle(self, method)

    def end_headers(self):
        # UI assets must follow the running image, including after container rebuilds.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


    def require_same_origin(self):
        host = self.headers.get("Host", "").lower()
        configured_hosts = self.context.settings.allowed_hosts
        allowed = {value.strip().lower() for value in configured_hosts.split(",") if value.strip()}
        if not allowed:
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if (host not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site" or
            (origin is not None and origin != f"http://{host}")):
            raise AppError(403, "Only same-origin requests are allowed.")


    def mutation_payload(self, maximum=65536):
        self.require_same_origin()
        if self.headers.get_content_type() != "application/json":
            raise AppError(415, "Send an application/json request.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= maximum:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise AppError(400, f"Invalid JSON request (maximum {maximum // 1024} KB).")
        if not isinstance(payload, dict):
            raise AppError(400, "Expected a JSON object.")
        return payload


    def send_download(self, data, filename, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
