"""User-initiated regional downloads, graph builds, and OSM network import."""
import hashlib
import json
import shutil
import time
import urllib.request
from contextlib import closing
from pathlib import Path

from garmin_library.errors import AppError
from garmin_library.network_geometry import split_way

CATALOG='https://download.geofabrik.de/index-v1.json'


def request_json(url,body=None,timeout=60):
    request=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json','User-Agent':'GarminActivityLibrary/1.0'})
    with urllib.request.urlopen(request,timeout=timeout) as response:return json.load(response)


def catalog(context,refresh=False):
    cache=context.settings.network_path/'catalog.json'
    if refresh or not cache.is_file():
        data=request_json(CATALOG)
        cache.parent.mkdir(parents=True,exist_ok=True)
        temporary=cache.with_suffix('.tmp');temporary.write_text(json.dumps(data));temporary.replace(cache)
    data=json.loads(cache.read_text())
    result=[]
    for feature in data['features']:
        prop=feature['properties'];url=prop.get('urls',{}).get('pbf','')
        if url.startswith('https://download.geofabrik.de/') and url.endswith('.osm.pbf'):
            result.append({'id':prop['id'],'name':prop.get('name',prop['id']),
                           'parent':prop.get('parent'),'url':url,'geometry':feature['geometry']})
    return sorted(result,key=lambda r:r['id'])


def region_size(region):
    try:
        with urllib.request.urlopen(urllib.request.Request(region['url'],method='HEAD',headers={'User-Agent':'GarminActivityLibrary/1.0'}),timeout=15) as response:
            return int(response.headers.get('Content-Length','0')) or None
    except Exception:
        return None


def access_flags(tags):
    highway=tags.get('highway','')
    if not highway or highway in {'motorway','motorway_link','construction','proposed','raceway','platform'}:
        return 0,0
    if tags.get('area')=='yes':return 0,0
    allowed={'yes','designated','permissive','official','destination'}
    prohibited={'no','private','customers'}
    general=tags.get('access') not in prohibited and tags.get('motorroad')!='yes'
    bicycle=tags.get('bicycle');foot=tags.get('foot')
    cycling=(bicycle in allowed) if bicycle is not None else general and highway not in {'steps','footway','pedestrian','bridleway'}
    running=(foot in allowed) if foot is not None else general and highway not in {'cycleway','trunk','trunk_link'}
    return int(cycling),int(running)


def download_region(context,generation,index,region,stop,reserve_factor=6):
    folder=context.settings.network_path/'sources';folder.mkdir(parents=True,exist_ok=True)
    url=region['url']
    if not url.startswith('https://download.geofabrik.de/') or not url.endswith('.osm.pbf'):
        raise ValueError('Invalid regional download URL')
    path=folder/(generation+'-'+str(index)+'.osm.pbf');temporary=path.with_suffix('.part')
    if not path.is_file():
        request=urllib.request.Request(url,headers={'User-Agent':'GarminActivityLibrary/1.0'})
        with urllib.request.urlopen(request,timeout=60) as source,temporary.open('wb') as target:
            size=int(source.headers.get('Content-Length','0'))
            reserve=size*reserve_factor+(512*1024**2 if reserve_factor<6 else 0)
            if size and shutil.disk_usage(folder).free<reserve:
                raise ValueError(f'Not enough free space for this source and cropping workspace (need at least {reserve/1024**3:.2f} GiB).')
            received=0
            while chunk:=source.read(1024*1024):
                if stop.is_set():return None
                target.write(chunk);received+=len(chunk)
                with closing(context.connect()) as db:
                    changed=db.execute('UPDATE network_state SET progress_done=?,progress_total=? WHERE staging_generation=?',(received,size,generation)).rowcount;db.commit()
                    if not changed:return None
            if size and received!=size:raise ValueError('Incomplete regional download; retry setup.')
        temporary.replace(path)
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'id':region['id'],'url':url,'bytes':path.stat().st_size,'sha256':digest,
            'source':str(path.relative_to(context.settings.network_path))}


def download_regions(context,generation,regions,stop):
    result=[]
    for index,region in enumerate(regions):
        if stop.is_set():return None
        item=download_region(context,generation,index,region,stop)
        if item is None:return None
        result.append(item)
    return result


def download_track_regions(context,generation,regions,stop,existing):
    """Download/crop/delete one source at a time; retain only compact crops for the build."""
    result=[]
    root=context.settings.network_path
    for index,region in enumerate(regions):
        if stop.is_set():return None
        previous=existing[index] if index<len(existing) else None
        if previous and previous.get('cropped_bytes') is not None and (root/previous['source']).is_file():
            result.append(previous);continue
        with closing(context.connect()) as db:
            db.execute("UPDATE network_state SET phase='downloading',progress_done=0,progress_total=0 WHERE staging_generation=?",(generation,));db.commit()
        item=download_region(context,generation,index,region,stop,2.5)
        if item is None:return None
        with closing(context.connect()) as db:
            db.execute("UPDATE network_state SET phase='cropping source',progress_done=?,progress_total=? WHERE staging_generation=?",(index,len(regions),generation));db.commit()
        engine=context.settings.network_engine_url
        request_json(engine+'/crop',{'generation':generation,'source':item['source'],'index':index})
        while not stop.wait(1):
            with closing(context.connect()) as db:
                if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:return None
            state=request_json(engine+'/crop/'+generation+'/'+str(index))
            if state['status']=='failed':raise ValueError(state.get('error','Cropping failed'))
            if state['status']=='ready':break
        if stop.is_set():return None
        cropped=root/'generations'/generation/'crops'/f'{index}.osm.pbf'
        with cropped.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        result.append({**item,'download_sha256':item['sha256'],'sha256':digest,
                       'cropped_bytes':cropped.stat().st_size,'source':str(cropped.relative_to(root))})
        # This source belongs exclusively to this generation and is no longer needed.
        (root/item['source']).unlink(missing_ok=True)
        with closing(context.connect()) as db:
            db.execute('UPDATE network_generations SET source_manifest=? WHERE id=?',(json.dumps(result),generation));db.commit()
    return result


def import_network(context,generation,source,stop):
    import osmium
    cache=context.settings.network_path/'generations'/generation/'node-cache'
    cache.parent.mkdir(parents=True,exist_ok=True)
    with closing(context.connect()) as db:
        # Clear only the staged generation, yielding between bounded deletes.
        while True:
            if stop.is_set():return False
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:
                db.rollback();return False
            db.execute("UPDATE network_state SET phase='clearing previous network import' WHERE staging_generation=?",(generation,))
            removed=db.execute('DELETE FROM network_sections WHERE id IN (SELECT id FROM network_sections WHERE generation=? LIMIT 1000)',(generation,)).rowcount
            db.commit()
            if not removed:break
            if stop.wait(.02):return False
        db.execute("UPDATE network_state SET phase='importing network',progress_done=0,progress_total=0 WHERE staging_generation=?",(generation,));db.commit()
        import shapely
        from shapely.geometry import shape,LineString
        row=db.execute('SELECT mode,footprint FROM network_generations WHERE id=?',(generation,)).fetchone()
        mask=shape(json.loads(row['footprint'])) if row and row['mode']=='tracks' and row['footprint'] else None
        if mask is not None:shapely.prepare(mask)
        count=0;entities=0;pending=[];last_flush=time.monotonic()

        def flush():
            nonlocal count,last_flush
            if not pending:return True
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:
                db.rollback();return False
            # All geometry work has already completed outside this transaction.
            for values,bounds in pending:
                cur=db.execute('''INSERT INTO network_sections(generation,way_id,ordinal,name,road_class,cycling,running,geometry,length_m)
                    VALUES(?,?,?,?,?,?,?,?,?)''',values)
                db.execute('INSERT INTO network_bounds VALUES(?,?,?,?,?)',(cur.lastrowid,*bounds))
            count+=len(pending)
            db.execute('UPDATE network_state SET progress_done=?,progress_total=0 WHERE staging_generation=?',(count,generation))
            db.commit();pending.clear();last_flush=time.monotonic()
            return not stop.wait(.02)

        try:
            processor=osmium.FileProcessor(str(source)).with_locations('sparse_file_array,'+str(cache))
            for way in processor:
                if stop.is_set():return False
                entities+=1
                if entities%10000==0:
                    if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:return False
                if not isinstance(way,osmium.osm.Way):continue
                tags=dict(way.tags);cycling,running=access_flags(tags)
                if not (cycling or running):continue
                if any(not n.location.valid() for n in way.nodes):continue
                coords=[[n.lon,n.lat] for n in way.nodes]
                if len(coords)<2:continue
                line=LineString(coords)
                # Clip once per source way, rather than against the full complex
                # corridor mask for every 25-metre section.
                if mask is None or mask.covers(line):pieces=[line]
                elif not mask.intersects(line):continue
                else:
                    clipped=line.intersection(mask)
                    pieces=list(clipped.geoms) if hasattr(clipped,'geoms') else [clipped]
                ordinal=0
                for piece in pieces:
                    if piece.geom_type!='LineString' or piece.is_empty:continue
                    for points,length in split_way([list(point) for point in piece.coords]):
                        values=(generation,way.id,ordinal,tags.get('name','Unnamed '+tags['highway']),tags['highway'],cycling,running,json.dumps(points),length)
                        ordinal+=1
                        xs=[p[0] for p in points];ys=[p[1] for p in points]
                        pending.append((values,(min(xs),max(xs),min(ys),max(ys))))
                        if len(pending)>=500 or time.monotonic()-last_flush>=1:
                            if not flush():return False
            if stop.is_set():return False
            if not flush():return False
        finally:
            cache.unlink(missing_ok=True)
        return True


def build_generation(context,generation,stop):
    with closing(context.connect()) as db:
        row=db.execute('SELECT * FROM network_generations WHERE id=?',(generation,)).fetchone()
    regions=json.loads(row['regions'])
    manifest=json.loads(row['source_manifest']) if row['source_manifest'] else None
    def phase(value):
        with closing(context.connect()) as db:
            db.execute('UPDATE network_state SET phase=?,progress_done=0,progress_total=0 WHERE staging_generation=?',(value,generation));db.commit()
    if row['mode']=='tracks':
        phase('preparing cropped sources')
        folder=context.settings.network_path/'generations'/generation;folder.mkdir(parents=True,exist_ok=True)
        (folder/'clip.geojson').write_text(json.dumps({'type':'Feature','properties':{},'geometry':json.loads(row['context_geometry'])}))
        manifest=download_track_regions(context,generation,regions,stop,manifest or [])
        if manifest is None:return
        size=sum(item['cropped_bytes'] for item in manifest)
        if shutil.disk_usage(folder).free<size*5+512*1024**2:
            raise ValueError('Not enough free space to build the cropped routing graph; free space and retry.')
    elif manifest is None:
        phase('downloading');manifest=download_regions(context,generation,regions,stop)
        if manifest is None:return
        with closing(context.connect()) as db:
            db.execute('UPDATE network_generations SET source_manifest=? WHERE id=?',(json.dumps(manifest),generation));db.commit()
    with closing(context.connect()) as db:
        db.execute('UPDATE network_generations SET source_manifest=? WHERE id=?',(json.dumps(manifest),generation));db.commit()
    phase('building graph')
    url=context.settings.network_engine_url
    result=request_json(url+'/build/'+generation)
    if result['status']!='ready':
        request_json(url+'/build',{'generation':generation,'sources':[m['source'] for m in manifest]})
        while not stop.wait(2):
            with closing(context.connect()) as db:
                if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:return
            result=request_json(url+'/build/'+generation)
            if result['status']=='failed':raise ValueError(result.get('error','Graph build failed'))
            if result['status']=='ready':break
        if stop.is_set():return
    phase('importing network')
    source=context.settings.network_path/'generations'/generation/'merged.osm.pbf'
    if not import_network(context,generation,source,stop):return
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT staging_generation FROM network_state WHERE id=1').fetchone()[0]!=generation:
            db.rollback();return
        db.execute("UPDATE network_generations SET status='matching' WHERE id=?",(generation,))
        db.execute('''INSERT OR IGNORE INTO network_source_versions(activity_id)
            SELECT id FROM activities''')
        db.execute('''INSERT OR REPLACE INTO network_jobs(generation,activity_id,revision)
            SELECT ?,a.id,v.revision FROM activities a JOIN network_source_versions v ON v.activity_id=a.id
            WHERE a.sport IN ('cycling','running')''',(generation,))
        db.execute("UPDATE network_state SET status='processing',phase='matching activities',progress_done=0,progress_total=(SELECT COUNT(*) FROM network_jobs WHERE generation=?) WHERE staging_generation=?",(generation,generation))
        db.commit()
    from garmin_library.network_cleanup import clean_intermediates
    clean_intermediates(context,generation)
