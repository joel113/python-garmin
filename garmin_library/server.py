"""Local HTTP server and the lifecycle of its three matching workers."""
from __future__ import annotations

from contextlib import closing
from http.server import ThreadingHTTPServer
import signal
import threading

from garmin_library.activities import import_all
from garmin_library.context import AppContext
from garmin_library.destinations import destination_worker
from garmin_library.http import Handler
from garmin_library.route_matching import route_matching_worker
from garmin_library.segments import segment_worker


class ApplicationServer(ThreadingHTTPServer):
    def __init__(self, address, context: AppContext):
        self.context = context
        super().__init__(address, Handler)


def serve(context: AppContext, host: str, port: int):
    if not context.settings.database_path.exists():
        import_all(context)
    with closing(context.connect()) as db:
        db.execute("PRAGMA journal_mode=WAL")
    stop = threading.Event()
    workers = [
        (threading.Thread(target=segment_worker, args=(context, stop), daemon=True, name="segment-matcher"), 25),
        (threading.Thread(target=destination_worker, args=(context, stop), daemon=True, name="destination-clustering"), 3),
        (threading.Thread(target=route_matching_worker, args=(context, stop), daemon=True, name="route-matching"), 3),
    ]
    server = ApplicationServer((host, port), context)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, terminate)
    try:
        for worker, _ in workers:
            worker.start()
        print(f"Listening on http://{host}:{port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        for worker, timeout in workers:
            if worker.ident is not None:
                worker.join(timeout=timeout)
        signal.signal(signal.SIGTERM, previous_sigterm)
