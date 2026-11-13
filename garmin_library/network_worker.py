"""Single-server orchestration for regional graph builds and activity coverage jobs."""
import logging
import time
from contextlib import closing

from garmin_library.network_coverage import aggregate_dirty, refresh_publication
from garmin_library.network_maps import build_generation, request_json
from garmin_library.network_matching import process_activity_job
from garmin_library.network_cleanup import maintain_network_storage

LOG=logging.getLogger(__name__)


def network_worker(context,stop):
    with closing(context.connect()) as db:
        db.execute("UPDATE network_jobs SET status='pending' WHERE status='processing'")
        db.execute("UPDATE network_generations SET status='queued' WHERE status='building'")
        db.commit()
    last_cleanup=0
    while not stop.is_set():
        generation=None
        try:
            with closing(context.connect()) as db:
                state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
                if state['status']=='unconfigured':
                    if time.monotonic()-last_cleanup>60:
                        last_cleanup=time.monotonic();maintain_network_storage(context)
                    stop.wait(2);continue
                staged=db.execute("SELECT id FROM network_generations WHERE id=? AND status='queued'",(state['staging_generation'],)).fetchone()
                if staged:
                    generation=staged[0]
                    db.execute("UPDATE network_generations SET status='building' WHERE id=?",(generation,));db.commit()
            if generation:
                build_generation(context,generation,stop)
                continue
            with closing(context.connect()) as db:
                if aggregate_dirty(db):continue
                refresh_publication(db)
                pending=db.execute("SELECT 1 FROM network_jobs j JOIN network_generations g ON g.id=j.generation WHERE j.status='pending' AND g.status IN ('matching','published') LIMIT 1").fetchone()
            if time.monotonic()-last_cleanup>60:
                last_cleanup=time.monotonic();maintain_network_storage(context)
            if pending:
                try:request_json(context.settings.network_engine_url+'/status',timeout=5)
                except Exception:
                    with closing(context.connect()) as db:
                        db.execute("UPDATE network_state SET error='Local map-matching service unavailable; waiting to reconnect.' WHERE id=1");db.commit()
                    stop.wait(5);continue
                if process_activity_job(context,stop):continue
            stop.wait(2)
        except Exception as error:
            LOG.exception('Network coverage processing failed')
            with closing(context.connect()) as db:
                if generation:
                    db.execute("UPDATE network_generations SET status='failed',error=? WHERE id=? AND status!='cancelled'",(str(error)[:500],generation))
                    db.execute("UPDATE network_state SET status='failed',error=? WHERE staging_generation=?",(str(error)[:500],generation))
                else:db.execute("UPDATE network_state SET status='failed',error=? WHERE id=1",(str(error)[:500],))
                db.commit()
            stop.wait(3)
