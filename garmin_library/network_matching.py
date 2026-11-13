"""Map-match recorded samples, then project matched OSM ways onto coverage sections."""
import json
import math
import urllib.error
from collections import OrderedDict
from contextlib import closing

from garmin_library.geometry import distance_between, gps_discontinuity, point_time
from garmin_library.network_geometry import decode_polyline, merge_ranges, projected_line, projected_point
from garmin_library.network_maps import request_json


class CoverageCancelled(Exception):
    pass


def continuous_samples(rows):
    result=[];part=[];previous=None
    for row in rows:
        current=(row['lat'],row['lon'],point_time(row['timestamp']))
        if not all(math.isfinite(v) for v in current[:2]) or not -90<=current[0]<=90 or not -180<=current[1]<=180:
            if len(part)>1:result.append(part)
            part=[];previous=None;continue
        if previous and gps_discontinuity(previous,current):
            if len(part)>1:result.append(part)
            part=[]
        part.append({'lat':current[0],'lon':current[1],'time':current[2],'seq':row['seq']})
        previous=current
    if len(part)>1:result.append(part)
    return result


def trace_chunks(samples,maximum=1000,overlap=20):
    for start in range(0,len(samples)-1,maximum-overlap):
        chunk=samples[start:start+maximum]
        if len(chunk)>1:yield chunk


def extract_pieces(response, first, second):
    """Get ordered matched edge pieces between two observed points, never model speeds."""
    from shapely.ops import substring
    shape=decode_polyline(response['shape']);edges=response['edges']
    start,end=first['edge_index'],second['edge_index']
    if not 0<=start<=end<len(edges):return []
    pieces=[]
    for index in range(start,end+1):
        edge=edges[index]
        coordinates=shape[edge['begin_shape_index']:edge['end_shape_index']+1]
        if len(coordinates)<2:continue
        line,scale=projected_line(coordinates)
        a=line.project(projected_point([first['lon'],first['lat']],scale),normalized=True) if index==start else 0
        b=line.project(projected_point([second['lon'],second['lat']],scale),normalized=True) if index==end else 1
        if b<a-1e-5:return []
        if b-a<1e-9:continue
        section=substring(line,a,b,normalized=True)
        points=[[x/scale,y/111195] for x,y in section.coords]
        length=sum(distance_between(p[::-1],q[::-1]) for p,q in zip(points,points[1:]))
        pieces.append((int(edge['way_id']),points,length))
    return pieces


class SectionProjector:
    def __init__(self,db,generation,sport):
        self.db=db;self.generation=generation;self.sport=sport;self.cache=OrderedDict()

    def sections(self,way_id):
        if way_id not in self.cache:
            rows=self.db.execute('''SELECT id,geometry,length_m FROM network_sections
                WHERE generation=? AND way_id=? AND '''+self.sport+'=1',(self.generation,way_id)).fetchall()
            self.cache[way_id]=[(row['id'],*projected_line(json.loads(row['geometry'])),row['length_m']) for row in rows]
            if len(self.cache)>64:self.cache.popitem(last=False)
        else:self.cache.move_to_end(way_id)
        return self.cache[way_id]

    def intersect(self,way_id,coordinates):
        from shapely.geometry import LineString
        result=[]
        for sid,line,scale,length in self.sections(way_id):
            piece=LineString([(p[0]*scale,p[1]*111195) for p in coordinates])
            if not line.envelope.buffer(2).intersects(piece.envelope):continue
            intersection=line.intersection(piece.buffer(1.5,cap_style=2))
            components=list(intersection.geoms) if hasattr(intersection,'geoms') else [intersection]
            ranges=[]
            for component in components:
                if component.geom_type!='LineString' or component.length<.001:continue
                points=list(component.coords)
                from shapely.geometry import Point
                ranges.append([line.project(Point(points[0]),normalized=True),line.project(Point(points[-1]),normalized=True)])
            for low,high in merge_ranges(ranges):result.append((sid,low,high,(high-low)*length))
        return result

    def stationary(self,way_id,point):
        best=None
        for sid,line,scale,_ in self.sections(way_id):
            distance=line.distance(projected_point(point,scale))
            if distance<=3 and (best is None or distance<best[0]):best=(distance,sid)
        return best[1] if best else None


def match_activity(context,db,generation,activity_id,sport,stop,check_current=lambda:True):
    samples=continuous_samples(db.execute('SELECT seq,lat,lon,timestamp FROM track_points WHERE activity_id=? ORDER BY seq',(activity_id,)))
    projector=SectionProjector(db,generation,sport)
    output={};seen=set();matched=set();all_points={p['seq'] for part in samples for p in part}
    def entry(sid):return output.setdefault(sid,{'ranges':[],'timed_m':0.0,'elapsed_s':0.0})
    for part in samples:
        for chunk in trace_chunks(part):
            if stop.is_set() or not check_current():raise CoverageCancelled()
            shape=[{'lat':p['lat'],'lon':p['lon']} for p in chunk]
            request={'shape':shape,'costing':'bicycle' if sport=='cycling' else 'pedestrian','shape_match':'map_snap',
                'filters':{'action':'include','attributes':['shape','edge.way_id','edge.begin_shape_index','edge.end_shape_index',
                    'matched.point','matched.type','matched.edge_index','matched.begin_route_discontinuity',
                    'matched.end_route_discontinuity','matched.distance_from_trace_point']}}
            response=None
            for attempt in range(3):
                try:
                    response=request_json(context.settings.network_engine_url+'/trace',{'generation':generation,'request':request},timeout=90)
                    break
                except urllib.error.HTTPError as error:
                    if error.code==422:break
                    if attempt==2:raise
                    if stop.wait(2**attempt):raise CoverageCancelled()
                except Exception:
                    if attempt==2:raise
                    if stop.wait(2**attempt):raise CoverageCancelled()
            if response is None or response.get('confidence_score',1)<.5:continue
            points=response.get('matched_points',[])
            if len(points)!=len(chunk):raise ValueError('Matching engine returned an unexpected point count')
            valid=[]
            for raw,point in zip(chunk,points):
                good=(point.get('type') in {'matched','interpolated'} and point.get('edge_index') is not None
                    and 'lat' in point and 'lon' in point and distance_between((raw['lat'],raw['lon']),(point['lat'],point['lon']))<=50)
                valid.append(good)
                if good:matched.add(raw['seq'])
            for index,(raw_a,raw_b) in enumerate(zip(chunk,chunk[1:])):
                pair=(raw_a['seq'],raw_b['seq'])
                if pair in seen:continue
                a,b=points[index:index+2]
                if not valid[index] or not valid[index+1] or a.get('end_route_discontinuity') or b.get('begin_route_discontinuity'):continue
                dt=(raw_b['time']-raw_a['time']) if raw_a['time'] is not None and raw_b['time'] is not None else None
                raw_distance=distance_between((raw_a['lat'],raw_a['lon']),(raw_b['lat'],raw_b['lon']))
                if dt is not None and (dt<=0 or dt>300):continue
                if dt is None and raw_distance>300:continue
                pieces=extract_pieces(response,a,b)
                length=sum(p[2] for p in pieces)
                if length>raw_distance*3+100:continue
                if length<.1 and raw_distance<3 and dt:
                    edges=response.get('edges',[]);edge_index=a.get('edge_index',-1)
                    if 0<=edge_index<len(edges):
                        sid=projector.stationary(int(edges[edge_index]['way_id']),[a['lon'],a['lat']])
                        if sid is not None:entry(sid)['elapsed_s']+=dt;seen.add(pair)
                    continue
                intersections=[]
                for way_id,coordinates,_ in pieces:intersections.extend(projector.intersect(way_id,coordinates))
                if intersections:seen.add(pair)
                denominator=max(length,sum(p[3] for p in intersections),.001)
                valid_speed=dt is not None and length/dt<=(55 if sport=='cycling' else 15)
                for sid,low,high,covered in intersections:
                    item=entry(sid);item['ranges'].append([low,high])
                    if valid_speed:item['timed_m']+=covered;item['elapsed_s']+=dt*covered/denominator
    result={sid:{**item,'ranges':merge_ranges(item['ranges'])} for sid,item in output.items() if item['ranges']}
    return result,len(matched),len(all_points-matched)


def process_activity_job(context,stop):
    with closing(context.connect()) as progress,closing(context.connect()) as db:
        progress.execute('BEGIN IMMEDIATE')
        job=progress.execute('''SELECT j.*,a.sport FROM network_jobs j JOIN activities a ON a.id=j.activity_id
            JOIN network_generations g ON g.id=j.generation WHERE j.status='pending'
            AND g.status IN ('matching','published') ORDER BY (g.status='published') DESC,j.generation,j.activity_id LIMIT 1''').fetchone()
        if job is None:progress.rollback();return False
        generation,aid,revision=job['generation'],job['activity_id'],job['revision']
        progress.execute("UPDATE network_jobs SET status='processing',error=NULL WHERE generation=? AND activity_id=?",(generation,aid));progress.commit()
        def current():
            row=progress.execute('SELECT revision,status FROM network_jobs WHERE generation=? AND activity_id=?',(generation,aid)).fetchone()
            return row is not None and tuple(row)==(revision,'processing')
        try:
            db.execute('BEGIN')
            from garmin_library.network_footprint import activity_outside_corridor
            outside=activity_outside_corridor(db,generation,aid)
            result,matched,unmatched=match_activity(context,db,generation,aid,job['sport'],stop,current)
            db.rollback();progress.execute('BEGIN IMMEDIATE')
            if stop.is_set() or not current():raise CoverageCancelled()
            progress.execute('DELETE FROM network_contributions WHERE generation=? AND activity_id=?',(generation,aid))
            progress.executemany('INSERT INTO network_contributions VALUES(?,?,?,?,?,?)',
                [(generation,aid,sid,json.dumps(item['ranges']),item['timed_m'],item['elapsed_s']) for sid,item in result.items()])
            progress.execute("UPDATE network_jobs SET status='completed',matched_points=?,unmatched_points=?,outside_coverage=? WHERE generation=? AND activity_id=?",(matched,unmatched,int(outside),generation,aid));progress.commit()
        except CoverageCancelled:
            db.rollback();progress.rollback()
            progress.execute("UPDATE network_jobs SET status='pending' WHERE generation=? AND activity_id=? AND revision=? AND status='processing'",(generation,aid,revision));progress.commit()
        except Exception as error:
            db.rollback();progress.rollback()
            progress.execute("UPDATE network_jobs SET status='failed',error=? WHERE generation=? AND activity_id=? AND revision=? AND status='processing'",(str(error)[:500],generation,aid,revision));progress.commit()
        return True
