"""Opt-in local Valhalla smoke check. Uses only the synthetic OSM fixture and a temporary DB."""
import argparse
import json
import math
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from garmin_library.config import Settings
from garmin_library.context import AppContext
from garmin_library.network_maps import import_network,request_json
from garmin_library.network_matching import process_activity_job
from garmin_library.network_coverage import aggregate_dirty,refresh_publication,status,vector_tile


def main():
    import osmium
    import mapbox_vector_tile
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shared-root',type=Path,required=True,help='Scratch directory mounted as /network in a dedicated test engine')
    parser.add_argument('--engine-url',default='http://127.0.0.1:18002')
    parser.add_argument('--track-crop',action='store_true',help='Build only a corridor, then test cleanup and graph reuse')
    args=parser.parse_args();root=args.shared_root.resolve();generation=uuid.uuid4().hex
    source=root/'sources'/('fixture-'+generation+'.osm.pbf');source.parent.mkdir(parents=True,exist_ok=True)
    with osmium.SimpleWriter(str(source)) as writer:
        for obj in osmium.FileProcessor(str(Path(__file__).with_name('fixtures')/'network.osm')):writer.add(obj)
    inputs=[str(source.relative_to(root))];footprint=context_geometry=None
    if args.track_crop:
        from garmin_library.network_footprint import local_buffers
        from shapely.geometry import mapping
        footprint,context_geometry=local_buffers([[11.001,48.004],[11.008,48.004]],.25)
        folder=root/'generations'/generation;folder.mkdir(parents=True,exist_ok=True)
        (folder/'clip.geojson').write_text(json.dumps({'type':'Feature','properties':{},'geometry':mapping(context_geometry)}))
        request_json(args.engine_url+'/crop',{'generation':generation,'source':inputs[0],'index':0})
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            crop=request_json(args.engine_url+'/crop/'+generation+'/0')
            if crop['status']=='failed':raise RuntimeError(crop['error'])
            if crop['status']=='ready':break
            time.sleep(.2)
        else:raise TimeoutError('Synthetic cropping did not finish')
        inputs=[str((folder/'crops/0.osm.pbf').relative_to(root))]
        source.unlink()
    request_json(args.engine_url+'/build',{'generation':generation,'sources':inputs})
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        result=request_json(args.engine_url+'/build/'+generation)
        if result['status']=='failed':raise RuntimeError(result.get('error'))
        if result['status']=='ready':break
        time.sleep(.5)
    else:raise TimeoutError('Synthetic graph did not finish in 120 seconds')
    with tempfile.TemporaryDirectory() as temporary:
        context=AppContext(Settings.from_environment({'GARMIN_NETWORK_ENGINE_URL':args.engine_url},root=Path(temporary)))
        with closing(context.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            regions=[{'id':'synthetic','name':'Synthetic fixture','geometry':{'type':'Polygon','coordinates':[[[11,48],[11.01,48],[11.01,48.01],[11,48.01],[11,48]]]}}]
            db.execute('INSERT INTO network_generations(id,regions,status) VALUES(?,?,?)',(generation,json.dumps(regions),'building'))
            if args.track_crop:
                db.execute("UPDATE network_generations SET mode='tracks',buffer_km=.25,footprint=?,context_geometry=? WHERE id=?",(json.dumps(mapping(footprint)),json.dumps(mapping(context_geometry)),generation))
            db.execute("UPDATE network_state SET staging_generation=?,status='processing' WHERE id=1",(generation,));db.commit()
        assert import_network(context,generation,root/'generations'/generation/'merged.osm.pbf',threading.Event())
        with closing(context.connect()) as db:
            db.execute("UPDATE network_generations SET status='matching' WHERE id=?",(generation,))
            db.execute("INSERT INTO activities(id,file_path,sport) VALUES('fixture','storage/activities/fixture.fit','cycling')")
            db.executemany('INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES(?,?,?,?,?)',
                [('fixture',i,48.00404,11.001+i*.0001,'2026-01-01T10:'+str((i*2)//60).zfill(2)+':'+str((i*2)%60).zfill(2)) for i in range(71)])
            db.execute("INSERT INTO activities(id,file_path,sport) VALUES('runfixture','storage/activities/runfixture.fit','running')")
            db.executemany('INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES(?,?,?,?,?)',
                [('runfixture',i,48.004,11.008-i*.0001,'2026-01-01T11:'+str((i*5)//60).zfill(2)+':'+str((i*5)%60).zfill(2)) for i in range(71)])
            db.commit()
        assert process_activity_job(context,threading.Event())
        assert process_activity_job(context,threading.Event())
        with closing(context.connect()) as db:
            jobs=[dict(row) for row in db.execute('SELECT * FROM network_jobs')]
            assert all(job['status']=='completed' and job['matched_points']==71 for job in jobs),jobs
            job=jobs[0]
            assert job['status']=='completed',job
            assert job['matched_points']==71,job
            while aggregate_dirty(db):pass
            refresh_publication(db);value=status(db)
            assert value['active_generation']==generation,value
            count=db.execute('SELECT COUNT(*) FROM network_stats WHERE activity_count>0').fetchone()[0]
            assert count>0
            assert db.execute("SELECT MAX(activity_count) FROM network_stats WHERE sport='all'").fetchone()[0]==2
            assert value['scales']['cycling']['speed_max']>value['scales']['running']['speed_max']
            z=15;x=int((11.004+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(48.004)))/math.pi)/2*2**z)
            features=mapbox_vector_tile.decode(vector_tile(context,db,z,x,y,'cycling',True))['coverage']['features']
            assert any(f['properties']['visited'] for f in features)
            assert any(not f['properties']['visited'] for f in features)
            if args.track_crop:
                from shapely.geometry import LineString
                for row in db.execute('SELECT geometry FROM network_sections'):
                    assert footprint.buffer(1e-8).covers(LineString(json.loads(row[0])))
                request_json(args.engine_url+'/cleanup',{'generation':generation})
                assert not (root/'generations'/generation/'merged.osm.pbf').exists()
                response=request_json(args.engine_url+'/trace',{'generation':generation,'request':{'shape':[{'lat':48.004,'lon':11.001+i*.0001} for i in range(71)],'costing':'bicycle','shape_match':'map_snap'}})
                assert response.get('edges'),'Graph must remain usable after intermediate cleanup'
                print('Cropping, SQL clipping, and intermediate cleanup preserved the working graph.')
            print('Passed: 142 matched synthetic GPS points (noisy cycling + reverse running);',count,'aggregate rows;',len(features),'vector-tile features.')


if __name__=='__main__':main()
