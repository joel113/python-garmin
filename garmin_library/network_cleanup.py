"""Bounded cleanup of derived map data, always preserving active and staging graphs."""
from contextlib import closing
from garmin_library.network_maps import request_json


def clean_intermediates(context,generation):
    try:result=request_json(context.settings.network_engine_url+'/cleanup',{'generation':generation},timeout=30)
    except Exception:return False
    with closing(context.connect()) as db:
        db.execute('UPDATE network_generations SET sources_cleaned=1,retained_bytes=? WHERE id=?',(result.get('retained_bytes'),generation));db.commit()
    return True


def maintain_network_storage(context):
    with closing(context.connect()) as db:
        ready=[row[0] for row in db.execute("SELECT id FROM network_generations WHERE status IN ('matching','published','retired') AND sources_cleaned=0")]
        obsolete=[row[0] for row in db.execute("""SELECT id FROM network_generations
            WHERE status IN ('retired','cancelled') AND COALESCE(published_at,created_at)<datetime('now','-10 minutes')
            AND id NOT IN (SELECT COALESCE(active_generation,'') FROM network_state)
            AND id NOT IN (SELECT COALESCE(staging_generation,'') FROM network_state)""")]
    for generation in ready:clean_intermediates(context,generation)
    for generation in obsolete:
        try:request_json(context.settings.network_engine_url+'/prune',{'generation':generation},timeout=30)
        except Exception:continue
        with closing(context.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            state=db.execute('SELECT active_generation,staging_generation FROM network_state').fetchone()
            if generation not in tuple(state):
                db.execute('DELETE FROM network_generations WHERE id=?',(generation,))
                db.execute('UPDATE network_state SET data_revision=data_revision+1 WHERE id=1')
            db.commit()
