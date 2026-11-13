"""Synthetic street coverage, speed aggregation, incremental jobs, and tiles."""
import json
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import mapbox_vector_tile
from garmin_library.network_coverage import aggregate_dirty,refresh_publication,status,vector_tile
from garmin_library.network_geometry import merge_ranges,complement_ranges,split_way
from garmin_library.network_maps import access_flags
from garmin_library.network_matching import continuous_samples,trace_chunks,match_activity
from support import TemporaryLibrary


def encode_polyline(points):
    result=[];previous=[0,0]
    for lon,lat in points:
        current=[round(lat*1e6),round(lon*1e6)]
        for axis in (0,1):
            value=current[axis]-previous[axis];value=~(value<<1) if value<0 else value<<1
            while value>=32:result.append(chr((32|(value&31))+63));value>>=5
            result.append(chr(value+63))
        previous=current
    return ''.join(result)


class NetworkCoverageTests(TemporaryLibrary,unittest.TestCase):
    generation='a'*32
    def setUp(self):
        super().setUp()
        with closing(self.context.connect()) as db:
            db.execute("INSERT INTO network_generations(id,regions,status) VALUES(?,'[]','published')",(self.generation,))
            db.execute("UPDATE network_state SET active_generation=?,status='ready' WHERE id=1",(self.generation,))
            db.execute('''INSERT INTO network_sections(generation,way_id,ordinal,name,road_class,cycling,running,geometry,length_m)
                VALUES(?,100,0,'Synthetic road','residential',1,1,?,22.3)''',(self.generation,json.dumps([[11,48],[11.0003,48]])))
            self.sid=db.execute('SELECT id FROM network_sections').fetchone()[0]
            db.execute('INSERT INTO network_bounds VALUES(?,11,11.0003,48,48)',(self.sid,));db.commit()

    def activity(self,db,aid,sport='cycling'):
        db.execute('INSERT INTO activities(id,file_path,sport) VALUES(?,?,?)',(aid,'storage/activities/'+aid+'.fit',sport))

    def contribution(self,db,aid,timed,duration,ranges):
        db.execute('INSERT INTO network_contributions VALUES(?,?,?,?,?,?)',(self.generation,aid,self.sid,json.dumps(ranges),timed,duration))

    def test_ground_distance_sections_and_partial_intervals(self):
        sections=list(split_way([[11,48],[11.01,48]]))
        self.assertGreater(len(sections),20)
        self.assertTrue(all(0<length<=25.0001 for _,length in sections))
        self.assertEqual(merge_ranges([[.2,.4],[.3,.6],[.8,1]]),[[.2,.6],[.8,1]])
        self.assertEqual(complement_ranges([[.2,.6],[.8,1]]),[[0,.2],[.6,.8]])

    def test_access_and_parallel_ways_do_not_merge(self):
        self.assertEqual(access_flags({'highway':'residential'}),(1,1))
        self.assertEqual(access_flags({'highway':'motorway'}),(0,0))
        self.assertEqual(access_flags({'highway':'path','access':'private'}),(0,0))
        self.assertEqual(access_flags({'highway':'footway','bicycle':'yes'}),(1,1))
        self.assertEqual(access_flags({'highway':'cycleway'}),(1,0))
        from garmin_library.network_matching import SectionProjector
        with closing(self.context.connect()) as db:
            self.assertFalse(SectionProjector(db,self.generation,'cycling').intersect(999,[[11,48],[11.0003,48]]))

    def test_distinct_activity_median_speed_and_sport_separation(self):
        with closing(self.context.connect()) as db:
            for aid,sport in [('one','cycling'),('two','cycling'),('run','running'),('unknown','gpx')]:self.activity(db,aid,sport)
            # A repeated lap contributes total distance/time, but still only one activity.
            self.contribution(db,'one',200,72,[[0,.5]])
            self.contribution(db,'two',100,18,[[.5,1]])
            self.contribution(db,'run',100,72,[[.1,.4]])
            db.commit();aggregate_dirty(db)
            cycling=db.execute("SELECT * FROM network_stats WHERE sport='cycling'").fetchone()
            self.assertEqual(cycling['activity_count'],2)
            self.assertAlmostEqual(cycling['speed_kmh'],15)
            self.assertEqual(json.loads(cycling['ranges']),[[0,1]])
            running=db.execute("SELECT * FROM network_stats WHERE sport='running'").fetchone()
            self.assertAlmostEqual(running['speed_kmh'],5)
            combined=db.execute("SELECT * FROM network_stats WHERE sport='all'").fetchone()
            self.assertEqual(combined['activity_count'],3);self.assertIsNone(combined['speed_kmh'])
            self.assertFalse(db.execute("SELECT 1 FROM network_jobs WHERE activity_id='unknown'").fetchone())
            jobs=[tuple(row) for row in db.execute('SELECT activity_id,revision FROM network_jobs ORDER BY activity_id')]
            self.assertEqual(len(jobs),3)
            old_revision=dict(jobs)['one']
            db.execute("DELETE FROM activities WHERE id='one'");self.activity(db,'one');db.commit();aggregate_dirty(db)
            self.assertGreater(db.execute("SELECT revision FROM network_jobs WHERE activity_id='one'").fetchone()[0],old_revision)
            self.assertEqual(db.execute("SELECT activity_count FROM network_stats WHERE sport='cycling'").fetchone()[0],1)

    def test_atomic_generation_publication_and_missing_speed(self):
        staged='b'*32
        with closing(self.context.connect()) as db:
            self.activity(db,'untimed');self.contribution(db,'untimed',0,0,[[0,.3]])
            db.execute("INSERT INTO network_generations(id,regions,status) VALUES(?,'[]','matching')",(staged,))
            db.execute("UPDATE network_state SET staging_generation=? WHERE id=1",(staged,))
            db.execute("INSERT INTO network_jobs(generation,activity_id,revision,status,error,matched_points,unmatched_points) VALUES(?,'untimed',1,'failed','synthetic',0,0)",(staged,));db.commit()
            aggregate_dirty(db);refresh_publication(db)
            self.assertEqual(status(db)['active_generation'],self.generation)
            self.assertIsNone(db.execute("SELECT speed_kmh FROM network_stats WHERE sport='cycling'").fetchone()[0])
            db.execute("UPDATE network_jobs SET status='completed' WHERE generation=?",(staged,));db.commit();refresh_publication(db)
            self.assertEqual(status(db)['active_generation'],staged)
            self.assertIsNone(status(db)['staging_generation'])

    def test_partial_coverage_vector_tile_contains_gray_remainder(self):
        with closing(self.context.connect()) as db:
            self.activity(db,'partial');self.contribution(db,'partial',10,5,[[0,.4]]);db.commit();aggregate_dirty(db)
            import math
            z=16;x=int((11+180)/360*2**z);y=int((1-math.asinh(math.tan(math.radians(48)))/math.pi)/2*2**z)
            data=vector_tile(self.context,db,z,x,y,'cycling',True)
            features=mapbox_vector_tile.decode(data)['coverage']['features']
            self.assertTrue(any(f['properties']['visited'] for f in features))
            self.assertTrue(any(not f['properties']['visited'] for f in features))
            visited=mapbox_vector_tile.decode(vector_tile(self.context,db,z,x,y,'cycling',False))['coverage']['features']
            self.assertTrue(all(f['properties']['visited'] for f in visited))

    def test_recorded_stops_and_laps_use_time_not_engine_speeds(self):
        points=[[11,48],[11.0003,48],[11.0003,48],[11,48]]
        response={'shape':encode_polyline([points[0],points[1],points[0]]),
            'edges':[{'way_id':100,'begin_shape_index':0,'end_shape_index':1,'speed':999},
                     {'way_id':100,'begin_shape_index':1,'end_shape_index':2,'speed':999}],
            'matched_points':[{'lon':p[0],'lat':p[1],'type':'matched','edge_index':0 if i<3 else 1} for i,p in enumerate(points)]}
        with closing(self.context.connect()) as db:
            self.activity(db,'laps')
            db.executemany('INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES(?,?,?,?,?)',
                [('laps',i,p[1],p[0],'2026-01-01T10:00:'+str(t).zfill(2)) for i,(p,t) in enumerate(zip(points,[0,8,30,38]))]);db.commit()
            with patch('garmin_library.network_matching.request_json',return_value=response):
                result,matched,unmatched=match_activity(self.context,db,self.generation,'laps','cycling',threading.Event())
            self.assertEqual((matched,unmatched),(4,0));self.assertEqual(len(result),1)
            item=result[self.sid];self.assertGreater(item['elapsed_s'],30)
            self.assertLess(item['timed_m']/item['elapsed_s']*3.6,6)
            self.assertEqual(item['ranges'],[[0,1]])

    def test_chunk_overlap_and_discontinuity_boundaries(self):
        samples=[{'seq':i} for i in range(2100)]
        chunks=list(trace_chunks(samples));self.assertEqual(chunks[0][-20:],chunks[1][:20])
        rows=[{'seq':i,'lat':48,'lon':11+i*.0001,'timestamp':t} for i,t in enumerate(['2026-01-01T10:00:00','2026-01-01T10:00:10','2026-01-01T09:00:00','2026-01-01T09:00:10'])]
        self.assertEqual(len(continuous_samples(rows)),2)

    def test_overlapping_chunks_do_not_duplicate_lap_time(self):
        from datetime import datetime,timedelta
        count=2100;start=datetime(2026,1,1,10)
        coords=[[11 if i%2==0 else 11.0003,48] for i in range(count)]
        def response(url,body,timeout):
            points=[[p['lon'],p['lat']] for p in body['request']['shape']]
            return {'shape':encode_polyline(points),'confidence_score':1,
                'edges':[{'way_id':100,'begin_shape_index':i,'end_shape_index':i+1} for i in range(len(points)-1)],
                'matched_points':[{'lat':p[1],'lon':p[0],'type':'matched','edge_index':min(i,len(points)-2)} for i,p in enumerate(points)]}
        with closing(self.context.connect()) as db:
            self.activity(db,'chunks')
            db.executemany('INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES(?,?,?,?,?)',
                [('chunks',i,p[1],p[0],(start+timedelta(seconds=i*10)).isoformat()) for i,p in enumerate(coords)]);db.commit()
            with patch('garmin_library.network_matching.request_json',side_effect=response):
                result,matched,_=match_activity(self.context,db,self.generation,'chunks','cycling',threading.Event())
            self.assertEqual(matched,count)
            self.assertGreater(result[self.sid]['elapsed_s'],20900)
            self.assertLess(result[self.sid]['elapsed_s'],21000)

    def test_cancelled_and_replaced_jobs_cannot_publish_old_contributions(self):
        from garmin_library.network_matching import process_activity_job
        from garmin_library.network_coverage import cancel_staging
        for cancel in (False,True):
            with self.subTest(cancel=cancel),closing(self.context.connect()) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute("DELETE FROM activities WHERE id='race'")
                self.activity(db,'race')
                db.executemany("INSERT INTO track_points(activity_id,seq,lat,lon,timestamp) VALUES('race',?,?,?,?)",
                    [(0,48,11,'2026-01-01T10:00:00'),(1,48,11.0003,'2026-01-01T10:00:08')]);db.commit()
                if cancel:
                    db.execute("UPDATE network_generations SET status='matching'")
                    db.execute("UPDATE network_state SET active_generation=NULL,staging_generation=?,status='processing' WHERE id=1",(self.generation,));db.commit()
                def invalidate(*args,**kwargs):
                    if cancel:
                        with closing(self.context.connect()) as write:revision=write.execute('SELECT revision FROM network_state').fetchone()[0]
                        cancel_staging(self.context,revision)
                    else:
                        with closing(self.context.connect()) as write:
                            write.execute("DELETE FROM activities WHERE id='race'");self.activity(write,'race');write.commit()
                    return {'shape':encode_polyline([[11,48],[11.0003,48]]),
                        'edges':[{'way_id':100,'begin_shape_index':0,'end_shape_index':1}],
                        'matched_points':[{'lon':lon,'lat':48,'type':'matched','edge_index':0} for lon in (11,11.0003)]}
                with patch('garmin_library.network_matching.request_json',side_effect=invalidate):
                    self.assertTrue(process_activity_job(self.context,threading.Event()))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM network_contributions WHERE activity_id='race'").fetchone()[0],0)
                self.assertEqual(db.execute("SELECT status FROM network_jobs WHERE activity_id='race'").fetchone()[0],'cancelled' if cancel else 'pending')
