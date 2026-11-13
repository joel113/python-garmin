"""Coverage setup, incremental aggregation, read APIs, and cached vector tiles."""
import json
import math
import statistics
import threading
import uuid
from collections import OrderedDict
from contextlib import closing

from garmin_library.errors import AppError
from garmin_library.network_geometry import complement_ranges, merge_ranges
from garmin_library.network_maps import catalog

_TILE_CACHE=OrderedDict()
_CACHE_LOCK=threading.Lock()
_PREVIEW_LOCK=threading.Lock()


def status(db):
    state=dict(db.execute('SELECT * FROM network_state WHERE id=1').fetchone())
    generations=[dict(row) for row in db.execute('SELECT * FROM network_generations WHERE id IN (?,?)',
        (state['active_generation'],state['staging_generation']))]
    for generation in generations:
        from shapely.geometry import shape,mapping
        generation['regions']=[{'id':r['id'],'name':r['name'],'geometry':mapping(shape(r['geometry']).simplify(.001,preserve_topology=True))} for r in json.loads(generation['regions'])]
        if generation.get('footprint'):
            generation['footprint']=mapping(shape(json.loads(generation['footprint'])).simplify(.0005,preserve_topology=True))
        generation.pop('context_geometry',None)
        manifest=json.loads(generation['source_manifest']) if generation.get('source_manifest') else []
        generation['download_bytes']=sum(item.get('bytes',0) for item in manifest)
        generation['cropped_bytes']=sum(item.get('cropped_bytes',0) for item in manifest) if generation.get('mode')=='tracks' else None
        generation.pop('source_manifest',None)
        generation.pop('scales',None)
        generation['jobs']={row[0]:row[1] for row in db.execute('SELECT status,COUNT(*) FROM network_jobs WHERE generation=? GROUP BY status',(generation['id'],))}
        generation['outside_coverage_activities']=db.execute('SELECT COUNT(*) FROM network_jobs WHERE generation=? AND outside_coverage=1',(generation['id'],)).fetchone()[0]
        generation['unmatched_points']=db.execute('SELECT COALESCE(SUM(unmatched_points),0) FROM network_jobs WHERE generation=?',(generation['id'],)).fetchone()[0]
        generation['unmatched_activities']=db.execute("SELECT COUNT(*) FROM network_jobs j WHERE generation=? AND status='completed' AND NOT EXISTS(SELECT 1 FROM network_contributions c WHERE c.generation=j.generation AND c.activity_id=j.activity_id)",(generation['id'],)).fetchone()[0]
    state['generations']=generations
    state['excluded_activities']=db.execute("SELECT COUNT(*) FROM activities WHERE COALESCE(sport,'') NOT IN ('cycling','running')").fetchone()[0]
    active=db.execute('SELECT scales FROM network_generations WHERE id=?',(state['active_generation'],)).fetchone()
    state['scales']=json.loads(active[0]) if active and active[0] else {}
    return state


def start_setup(context,region_ids,revision):
    if not isinstance(region_ids,list) or not 1<=len(region_ids)<=20 or any(not isinstance(r,str) for r in region_ids):
        raise AppError(400,'Choose between 1 and 20 named regions.')
    available={r['id']:r for r in catalog(context)}
    if len(set(region_ids))!=len(region_ids) or any(r not in available for r in region_ids):raise AppError(400,'Invalid region selection.')
    generation=uuid.uuid4().hex
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
        if revision!=state['revision']:raise AppError(409,'Coverage setup changed; reload and retry.')
        if state['staging_generation']:raise AppError(409,'A build is already staged. Retry or cancel it first.')
        db.execute('INSERT INTO network_generations(id,regions) VALUES(?,?)',(generation,json.dumps([available[r] for r in region_ids])))
        db.execute("UPDATE network_state SET staging_generation=?,revision=revision+1,status='processing',phase='queued',error=NULL WHERE id=1",(generation,));db.commit()
    return generation


def retry(context,revision):
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
        if revision!=state['revision']:raise AppError(409,'Coverage setup changed; reload and retry.')
        if not state['staging_generation'] and not state['active_generation']:
            raise AppError(400,'Choose regions before retrying coverage.')
        if state['staging_generation']:
            db.execute("UPDATE network_generations SET status='queued',error=NULL WHERE id=? AND status='failed'",(state['staging_generation'],))
        db.execute("UPDATE network_jobs SET status='pending',error=NULL WHERE status='failed' AND generation IN (?,?)",(state['active_generation'],state['staging_generation']))
        db.execute("UPDATE network_state SET revision=revision+1,status='processing',error=NULL WHERE id=1");db.commit()


def cancel_staging(context,revision):
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE');state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
        if revision!=state['revision']:raise AppError(409,'Coverage setup changed; reload and retry.')
        if state['staging_generation']:
            db.execute("UPDATE network_generations SET status='cancelled' WHERE id=?",(state['staging_generation'],))
            db.execute("UPDATE network_jobs SET status='cancelled' WHERE generation=?",(state['staging_generation'],))
        db.execute("UPDATE network_state SET staging_generation=NULL,revision=revision+1,status=CASE WHEN active_generation IS NULL THEN 'unconfigured' ELSE 'ready' END,phase='',error=NULL WHERE id=1");db.commit()
    if state['staging_generation']:
        from garmin_library.network_maps import request_json
        try:request_json(context.settings.network_engine_url+'/cancel',{'generation':state['staging_generation']},timeout=5)
        except Exception:pass


def aggregate_dirty(db,limit=500):
    ids=[row[0] for row in db.execute('SELECT section_id FROM network_dirty LIMIT ?',(limit,))]
    if not ids:return False
    db.execute('BEGIN IMMEDIATE')
    references={}
    for sid in ids:
        generation=db.execute('SELECT generation FROM network_sections WHERE id=?',(sid,)).fetchone()[0]
        rows=[dict(row) for row in db.execute('''SELECT c.*,a.sport FROM network_contributions c
            JOIN activities a ON a.id=c.activity_id WHERE section_id=?''',(sid,))]
        for sport in ('cycling','running','all'):
            selected=[row for row in rows if sport=='all' or row['sport']==sport]
            if not selected:
                db.execute('DELETE FROM network_stats WHERE section_id=? AND sport=?',(sid,sport));continue
            speeds=[r['timed_m']/r['elapsed_s']*3.6 for r in selected if r['elapsed_s']>0 and r['timed_m']>0]
            references[(generation,sport)]=max(references.get((generation,sport),1),len(selected))
            ranges=merge_ranges([interval for r in selected for interval in json.loads(r['ranges'])])
            db.execute('INSERT OR REPLACE INTO network_stats VALUES(?,?,?,?,?,?)',
                (sid,sport,len(selected),statistics.median(speeds) if speeds and sport!='all' else None,len(speeds),json.dumps(ranges)))
        db.execute('DELETE FROM network_dirty WHERE section_id=?',(sid,))
    for generation in {key[0] for key in references}:
        row=db.execute('SELECT scales FROM network_generations WHERE id=?',(generation,)).fetchone()
        if row and row[0]:
            scales=json.loads(row[0])
            for sport in ('cycling','running','all'):
                scales[sport]['max_count']=max(scales[sport]['max_count'],references.get((generation,sport),1))
            db.execute('UPDATE network_generations SET scales=? WHERE id=?',(json.dumps(scales),generation))
    db.execute('UPDATE network_state SET data_revision=data_revision+1 WHERE id=1');db.commit()
    return True


def refresh_publication(db):
    db.execute('BEGIN IMMEDIATE')
    state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
    staging=state['staging_generation']
    if staging:
        generation=db.execute('SELECT status FROM network_generations WHERE id=?',(staging,)).fetchone()
        if generation and generation[0]=='matching':
            jobs={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM network_jobs WHERE generation=? GROUP BY status',(staging,))}
            dirty=db.execute('SELECT 1 FROM network_dirty d JOIN network_sections s ON s.id=d.section_id WHERE s.generation=? LIMIT 1',(staging,)).fetchone()
            total=sum(jobs.values());done=jobs.get('completed',0)
            db.execute('UPDATE network_state SET progress_done=?,progress_total=? WHERE id=1',(done,total))
            if not dirty and not jobs.get('pending',0) and not jobs.get('processing',0):
                if jobs.get('failed',0):db.execute("UPDATE network_state SET status='failed',error='Some activities failed to match; retry to finish this graph.' WHERE id=1")
                else:
                    scales={}
                    for sport in ('cycling','running','all'):
                        rows=db.execute('''SELECT st.speed_kmh,st.activity_count FROM network_stats st
                            JOIN network_sections s ON s.id=st.section_id WHERE s.generation=? AND st.sport=?''',(staging,sport)).fetchall()
                        speeds=sorted(row[0] for row in rows if row[0] is not None)
                        scales[sport]={'speed_max':max(1,speeds[min(len(speeds)-1,int(len(speeds)*.95))]) if speeds else None,
                                       'max_count':max([row[1] for row in rows],default=1)}
                    db.execute('UPDATE network_generations SET scales=? WHERE id=?',(json.dumps(scales),staging))
                    db.execute("UPDATE network_jobs SET status='cancelled' WHERE status IN ('pending','processing','failed') AND generation IN (SELECT id FROM network_generations WHERE status='published')")
                    db.execute("UPDATE network_generations SET status='retired',published_at=CURRENT_TIMESTAMP WHERE status='published'")
                    db.execute("UPDATE network_generations SET status='published',published_at=CURRENT_TIMESTAMP WHERE id=?",(staging,))
                    db.execute("""UPDATE network_state SET active_generation=?,staging_generation=NULL,status='ready',phase='',error=NULL,
                        revision=revision+1,data_revision=data_revision+1 WHERE id=1""",(staging,))
    elif state['active_generation']:
        pending=db.execute("SELECT 1 FROM network_jobs WHERE generation=? AND status IN ('pending','processing') LIMIT 1",(state['active_generation'],)).fetchone()
        failed=db.execute("SELECT 1 FROM network_jobs WHERE generation=? AND status='failed' LIMIT 1",(state['active_generation'],)).fetchone()
        db.execute('UPDATE network_state SET status=?,phase=?,error=? WHERE id=1',
            ('processing' if pending else 'failed' if failed else 'ready','matching activities' if pending else '',
             'Some activities failed; retry matching.' if failed else None))
    db.commit()


def section_detail(db,sid,sport,page=0):
    section=db.execute('SELECT id,name,road_class,generation,length_m FROM network_sections WHERE id=?',(sid,)).fetchone()
    if section is None:raise AppError(404,'Network section not found.')
    where='' if sport=='all' else ' AND a.sport=?'
    params=(sid,) if sport=='all' else (sid,sport)
    rows=db.execute('''SELECT a.id,a.start_time,a.sport,c.timed_m,c.elapsed_s FROM network_contributions c
        JOIN activities a ON a.id=c.activity_id WHERE c.section_id=?'''+where+' ORDER BY a.start_time DESC,a.id LIMIT 25 OFFSET ?',(*params,page*25))
    stats=db.execute('SELECT * FROM network_stats WHERE section_id=? AND sport=?',(sid,sport)).fetchone()
    return {'section':dict(section),'stats':dict(stats) if stats else None,'page':page,'activities':[dict(row) for row in rows]}


def tile_bounds(z,x,y):
    n=2**z
    def latitude(value):return math.degrees(math.atan(math.sinh(math.pi*(1-2*value/n))))
    return x/n*360-180,latitude(y+1),(x+1)/n*360-180,latitude(y)


def vector_tile(context,db,z,x,y,sport,unvisited,generation=None):
    import mapbox_vector_tile
    from shapely.geometry import LineString,box
    from shapely.ops import substring
    from shapely import transform
    state=db.execute('SELECT active_generation,data_revision FROM network_state WHERE id=1').fetchone()
    generation=generation or state[0]
    if generation and not db.execute("SELECT 1 FROM network_generations WHERE id=? AND status IN ('published','retired')",(generation,)).fetchone():
        raise AppError(404,'Published graph generation not found.')
    key=(str(context.settings.database_path),generation,state[1],z,x,y,sport,unvisited)
    with _CACHE_LOCK:
        if key in _TILE_CACHE:_TILE_CACHE.move_to_end(key);return _TILE_CACHE[key]
    west,south,east,north=tile_bounds(z,x,y);bounds=box(west,south,east,north)
    access='' if sport=='all' else ' AND s.'+sport+'=1'
    overview=' AND st.activity_count>0' if z<12 or not unvisited else ''
    # Keep the spatial index as the outer loop. An ordinary JOIN lets SQLite
    # scan every section in the generation and probe the R-tree once per row.
    rows=db.execute('''SELECT s.*,st.activity_count,st.speed_kmh,st.speed_samples,st.ranges FROM network_bounds b
        CROSS JOIN network_sections s ON s.id=b.id LEFT JOIN network_stats st ON st.section_id=s.id AND st.sport=?
        WHERE s.generation=? AND b.max_lon>=? AND b.min_lon<=? AND b.max_lat>=? AND b.min_lat<=?'''+access+overview+
        ' ORDER BY COALESCE(st.activity_count,0) DESC,s.id LIMIT 20000',(sport,generation,west,east,south,north))
    features=[];n=2**z
    def project(lon,lat,z_value=None):
        import numpy as np
        lon=np.asarray(lon);lat=np.asarray(lat)
        px=((lon+180)/360*n-x)*4096
        radians=np.radians(np.clip(lat,-85.05112878,85.05112878))
        py=((1-np.arcsinh(np.tan(radians))/math.pi)/2*n-y)*4096
        return px,py
    for row in rows:
        line=LineString(json.loads(row['geometry']));visited=json.loads(row['ranges']) if row['ranges'] else []
        groups=[(visited,True)]
        if unvisited and z>=12:groups.append((complement_ranges(visited),False))
        for intervals,is_visited in groups:
            for low,high in intervals:
                geometry=substring(line,low,high,normalized=True).intersection(bounds)
                if geometry.is_empty or geometry.geom_type not in {'LineString','MultiLineString'}:continue
                properties={'section_id':row['id'],'name':row['name'],'count':row['activity_count'] if is_visited else 0,
                    'speed_samples':row['speed_samples'] if is_visited else 0,'visited':is_visited}
                if is_visited and row['speed_kmh'] is not None:properties['speed']=round(row['speed_kmh'],2)
                features.append({'geometry':transform(geometry,project,interleaved=False),'properties':properties})
    data=mapbox_vector_tile.encode({'name':'coverage','features':features},default_options={'y_coord_down':True})
    with _CACHE_LOCK:
        _TILE_CACHE[key]=data
        while len(_TILE_CACHE)>128:_TILE_CACHE.popitem(last=False)
    return data


def preview_track_setup(context,buffer_km=2):
    with _PREVIEW_LOCK:
        return _preview_track_setup(context,buffer_km)


def _preview_track_setup(context,buffer_km=2):
    """Calculate a local-only footprint and download proposal, never fetch map extracts."""
    from garmin_library.network_footprint import track_corridors,track_fingerprint,choose_regions,validate_buffer_km
    from garmin_library.network_maps import region_size
    from concurrent.futures import ThreadPoolExecutor
    validate_buffer_km(buffer_km)
    with closing(context.connect()) as db:
        db.execute('BEGIN')
        fingerprint=track_fingerprint(db)
        cached=db.execute("SELECT summary FROM network_previews WHERE fingerprint=? AND buffer_km=? AND created_at>=datetime('now','-1 day') ORDER BY created_at DESC LIMIT 1",(fingerprint,buffer_km)).fetchone()
        if cached:return json.loads(cached[0])
        summary=track_corridors(db,buffer_km)
    selected,uncovered=choose_regions(summary['context_geometry'],catalog(context))
    with ThreadPoolExecutor(max_workers=4) as executor:
        sizes=list(executor.map(region_size,selected))
    preview=uuid.uuid4().hex
    proposal={key:value for key,value in summary.items() if key not in {'footprint','context_geometry'}}
    from shapely.geometry import mapping,shape
    proposal.update(id=preview,geometry=mapping(shape(summary['footprint']).simplify(.0005,preserve_topology=True)),regions=[{'id':r['id'],'name':r['name'],'bytes':size} for r,size in zip(selected,sizes)],
                    download_bytes=sum(size or 0 for size in sizes),largest_source_bytes=max([size or 0 for size in sizes],default=0),
                    temporary_reserve_bytes=int(max([size or 0 for size in sizes],default=0)*2.5+512*1024**2),unknown_sizes=sum(size is None for size in sizes),
                    uncovered=uncovered)
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        if track_fingerprint(db)!=fingerprint:raise AppError(409,'Tracks changed during preview. Please calculate it again.')
        db.execute("DELETE FROM network_previews WHERE created_at<datetime('now','-1 day')")
        db.execute('''INSERT INTO network_previews(id,fingerprint,buffer_km,footprint,context_geometry,regions,summary)
            VALUES(?,?,?,?,?,?,?)''',(preview,fingerprint,buffer_km,json.dumps(summary['footprint']),json.dumps(summary['context_geometry']),json.dumps(selected),json.dumps(proposal)))
        db.commit()
    return proposal


def start_track_setup(context,preview_id,revision):
    if not isinstance(preview_id,str) or len(preview_id)!=32:raise AppError(400,'Calculate a track preview first.')
    from garmin_library.network_footprint import track_fingerprint
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        state=db.execute('SELECT * FROM network_state WHERE id=1').fetchone()
        if revision!=state['revision']:raise AppError(409,'Coverage setup changed; reload and retry.')
        if state['staging_generation']:raise AppError(409,'Cancel or finish the staged setup before starting a track-based build.')
        preview=db.execute("SELECT * FROM network_previews WHERE id=? AND created_at>=datetime('now','-1 day')",(preview_id,)).fetchone()
        if preview is None or preview['fingerprint']!=track_fingerprint(db):
            raise AppError(409,'Preview expired or tracks changed. Calculate a new preview.')
        generation=uuid.uuid4().hex
        db.execute('''INSERT INTO network_generations(id,regions,mode,buffer_km,footprint,context_geometry)
            VALUES(?,?,'tracks',?,?,?)''',(generation,preview['regions'],preview['buffer_km'],preview['footprint'],preview['context_geometry']))
        db.execute("UPDATE network_state SET staging_generation=?,revision=revision+1,status='processing',phase='queued',error=NULL WHERE id=1",(generation,));db.commit()
    return generation
