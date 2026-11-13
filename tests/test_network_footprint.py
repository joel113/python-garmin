"""Small synthetic corridors, automatic sources, setup guards, clipping, and cleanup."""
import json
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
from shapely.geometry import shape,Point,box,mapping

from garmin_library.network_footprint import local_buffers,track_corridors,choose_regions
from garmin_library.network_coverage import preview_track_setup,start_track_setup
from garmin_library.network_cleanup import maintain_network_storage
from garmin_library.network_maps import import_network
from garmin_library.errors import AppError
from support import TemporaryLibrary


class TrackFootprintTests(TemporaryLibrary,unittest.TestCase):
    def seed(self,db,aid='one',lat=48,lon=11):
        db.execute("INSERT INTO activities(id,file_path,sport,start_time) VALUES(?,?,'cycling','2026-01-01T10:00:00')",(aid,'storage/activities/'+aid+'.fit'))
        db.executemany('INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES(?,?,?,?,?)',
            [(aid,0,lat,lon,'2026-01-01T10:00:00'),(aid,1,lat,lon+.003,'2026-01-01T10:01:00')])

    def regions(self):
        def region(id,parent,bounds):return {'id':id,'parent':parent,'name':id,'url':'https://download.geofabrik.de/'+id+'.osm.pbf','geometry':mapping(box(*bounds))}
        return [region('country',None,[10,47,13,50]),region('local','country',[10.8,47.8,11.2,48.2]),region('far','country',[12,49,13,50])]

    def test_scattered_tracks_have_local_corridors_not_a_global_box(self):
        with closing(self.context.connect()) as db:
            self.seed(db);self.seed(db,'two',49,13)
            db.execute("INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES('one',-1,30,-100,'2025-01-01T00:00:00')")
            db.commit();result=track_corridors(db,2)
        mask=shape(result['footprint'])
        self.assertTrue(mask.covers(Point(11,48)))
        self.assertTrue(mask.covers(Point(13,49)))
        self.assertFalse(mask.covers(Point(12,48.5)))
        self.assertFalse(mask.covers(Point(-100,30)))
        self.assertEqual(result['activity_count'],2)
        self.assertLess(result['area_km2'],100)

    def test_dateline_and_buffer_limits(self):
        target,_=local_buffers([[179.999,10],[-179.999,10]],2)
        self.assertTrue(target.covers(Point(179.999,10)))
        self.assertTrue(target.covers(Point(-179.999,10)))
        self.assertFalse(target.covers(Point(0,10)))
        with closing(self.context.connect()) as db:
            self.seed(db);db.commit()
            for value in (0,-1,True,100,float('inf')):
                with self.assertRaises(AppError):track_corridors(db,value)

    def test_small_sources_and_stale_preview_protection(self):
        with closing(self.context.connect()) as db:self.seed(db);db.commit()
        with patch('garmin_library.network_coverage.catalog',return_value=self.regions()),patch('garmin_library.network_maps.region_size',return_value=10000):
            proposal=preview_track_setup(self.context,2)
            self.assertEqual([r['id'] for r in proposal['regions']],['local'])
            self.assertEqual(proposal['download_bytes'],10000)
            with closing(self.context.connect()) as db:revision=db.execute('SELECT revision FROM network_state').fetchone()[0]
            with closing(self.context.connect()) as db:self.seed(db,'later');db.commit()
            with self.assertRaises(AppError):start_track_setup(self.context,proposal['id'],revision)
            proposal=preview_track_setup(self.context,2)
            generation=start_track_setup(self.context,proposal['id'],revision)
        with closing(self.context.connect()) as db:
            row=db.execute('SELECT * FROM network_generations WHERE id=?',(generation,)).fetchone()
            self.assertEqual(row['mode'],'tracks');self.assertEqual(row['buffer_km'],2)
            self.assertTrue(shape(json.loads(row['footprint'])).covers(Point(11,48)))

    def test_sql_import_clips_complete_ways_to_display_mask(self):
        from pathlib import Path
        generation='c'*32;mask=box(11.002,48.0038,11.006,48.0042)
        with closing(self.context.connect()) as db:
            db.execute("INSERT INTO network_generations(id,regions,mode,footprint) VALUES(?,'[]','tracks',?)",(generation,json.dumps(mapping(mask))))
            db.execute('UPDATE network_state SET staging_generation=?',(generation,));db.commit()
        source=Path(__file__).with_name('fixtures')/'network.osm'
        self.assertTrue(import_network(self.context,generation,source,threading.Event()))
        with closing(self.context.connect()) as db:
            rows=db.execute('SELECT geometry FROM network_sections').fetchall()
            self.assertTrue(rows)
            for row in rows:self.assertTrue(mask.buffer(1e-10).covers(shape({'type':'LineString','coordinates':json.loads(row[0])})))

    def test_cleanup_never_prunes_active_or_staging_graphs(self):
        active='a'*32;staging='b'*32;old='c'*32
        with closing(self.context.connect()) as db:
            for gid,status in ((active,'published'),(staging,'matching'),(old,'retired')):
                db.execute("INSERT INTO network_generations(id,regions,status,created_at,published_at) VALUES(?,'[]',?,'2000-01-01','2000-01-01')",(gid,status))
            self.seed(db)
            db.execute("INSERT INTO network_sections(generation,way_id,ordinal,name,road_class,cycling,running,geometry,length_m) VALUES(?,1,0,'old','residential',1,1,'[[11,48],[11.001,48]]',75)",(old,))
            sid=db.execute('SELECT id FROM network_sections').fetchone()[0]
            db.execute("INSERT INTO network_contributions VALUES(?,'one',?,'[[0,1]]',75,30)",(old,sid))
            db.execute('UPDATE network_state SET active_generation=?,staging_generation=?',(active,staging));db.commit()
        calls=[]
        def cleanup(url,body,timeout):calls.append((url,body['generation']));return {'status':'cleaned'}
        with patch('garmin_library.network_cleanup.request_json',side_effect=cleanup):maintain_network_storage(self.context)
        self.assertEqual([gid for url,gid in calls if url.endswith('/prune')],[old])
        with closing(self.context.connect()) as db:
            self.assertEqual({r[0] for r in db.execute('SELECT id FROM network_generations')},{active,staging})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM network_sections').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activities').fetchone()[0],1)

    def test_sources_are_cropped_and_deleted_one_at_a_time(self):
        from types import SimpleNamespace
        from pathlib import Path
        from garmin_library.network_maps import download_track_regions
        generation='d'*32;root=self.context.settings.network_path
        source_dir=root/'sources';source_dir.mkdir(parents=True)
        sentinel=source_dir/'unrelated.osm.pbf';sentinel.write_bytes(b'preserve')
        with closing(self.context.connect()) as db:
            db.execute("INSERT INTO network_generations(id,regions,mode) VALUES(?,'[]','tracks')",(generation,))
            db.execute('UPDATE network_state SET staging_generation=?',(generation,));db.commit()
        def download(context,gid,index,region,stop,reserve_factor):
            if index:self.assertFalse((source_dir/(gid+'-0.osm.pbf')).exists())
            path=source_dir/(gid+'-'+str(index)+'.osm.pbf');path.write_bytes(b'full-source'*100)
            return {'id':region['id'],'url':region['url'],'bytes':path.stat().st_size,'sha256':'download-hash','source':str(path.relative_to(root))}
        def engine(url,body=None):
            if body:
                path=root/'generations'/generation/'crops'/str(body['index'])
                path=path.with_suffix('.osm.pbf');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'crop')
            return {'status':'ready'}
        stop=SimpleNamespace(is_set=lambda:False,wait=lambda timeout:False)
        with patch('garmin_library.network_maps.download_region',side_effect=download),patch('garmin_library.network_maps.request_json',side_effect=engine):
            manifest=download_track_regions(self.context,generation,self.regions()[:2],stop,[])
        self.assertEqual(len(manifest),2)
        self.assertTrue(all(row['cropped_bytes']==4 for row in manifest))
        self.assertTrue(all((root/row['source']).is_file() for row in manifest))
        self.assertEqual(sentinel.read_bytes(),b'preserve')
