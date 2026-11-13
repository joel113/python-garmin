"""Local-only track corridors and automatic selection of small regional extracts."""
import hashlib
import json
import math
from contextlib import closing
from functools import lru_cache

from garmin_library.errors import AppError
from garmin_library.geometry import gps_discontinuity,point_time


def track_fingerprint(db):
    rows=db.execute("""SELECT a.id,COALESCE(v.revision,0) FROM activities a
        LEFT JOIN network_source_versions v ON v.activity_id=a.id
        WHERE a.sport IN ('cycling','running') ORDER BY a.id""")
    return hashlib.sha256(json.dumps([list(row) for row in rows]).encode()).hexdigest()


def normalize_longitudes(geometry):
    from shapely import affinity
    from shapely.geometry import box
    from shapely.ops import unary_union
    pieces=[]
    for offset in (-360,0,360):
        piece=geometry.intersection(box(-180+offset,-90,180+offset,90))
        if not piece.is_empty and piece.area>0:pieces.append(affinity.translate(piece,xoff=-offset))
    return unary_union(pieces)


def local_buffers(points,buffer_km):
    import numpy as np
    from pyproj import CRS,Transformer
    from shapely import transform
    from shapely.geometry import LineString,Point
    # Work in metre coordinates per continuous part, keeping dateline crossings local.
    xs=np.degrees(np.unwrap(np.radians([p[0] for p in points])))
    ys=np.array([p[1] for p in points])
    center_lon=float((xs.min()+xs.max())/2);center_lat=float((ys.min()+ys.max())/2)
    crs=CRS.from_proj4(f'+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +datum=WGS84 +units=m +over')
    forward=Transformer.from_crs('EPSG:4326',crs,always_xy=True,force_over=True)
    backward=Transformer.from_crs(crs,'EPSG:4326',always_xy=True,force_over=True)
    x,y=forward.transform(xs,ys)
    line=LineString(zip(x,y)) if len(points)>1 else Point(x[0],y[0])
    line=line.simplify(20,preserve_topology=True)
    target=line.buffer(buffer_km*1000,quad_segs=4).simplify(10,preserve_topology=True)
    context=line.buffer((buffer_km+1)*1000,quad_segs=4).simplify(10,preserve_topology=True)
    return (normalize_longitudes(transform(target,backward.transform,interleaved=False)),
            normalize_longitudes(transform(context,backward.transform,interleaved=False)))


def validate_buffer_km(buffer_km):
    if isinstance(buffer_km,bool) or not isinstance(buffer_km,(int,float)) or not math.isfinite(buffer_km) or not .25<=buffer_km<=20:
        raise AppError(400,'Track buffer must be between 0.25 and 20 km.')


def track_corridors(db,buffer_km=2):
    from itertools import groupby
    from shapely.geometry import mapping
    from shapely.ops import unary_union
    validate_buffer_km(buffer_km)
    targets=[];contexts=[];batch_targets=[];batch_contexts=[];activity_count=0
    rows=db.execute("""SELECT p.activity_id,p.lat,p.lon,p.timestamp,a.start_time FROM track_points p
        JOIN activities a ON a.id=p.activity_id WHERE a.sport IN ('cycling','running')
        ORDER BY p.activity_id,p.seq""")
    def collect(part):
        if len(part)<2:return False
        # Bound projection extent even for exceptionally long continuous tours.
        for start in range(0,len(part)-1,999):
            section=part[start:start+1000]
            if len(section)<2:continue
            target,context=local_buffers(section,buffer_km)
            batch_targets.append(target);batch_contexts.append(context)
        return True
    for _,records in groupby(rows,lambda row:row['activity_id']):
        part=[];previous=None;usable=False
        for row in records:
            point=(row['lat'],row['lon'],point_time(row['timestamp']))
            activity_start=point_time(row['start_time'])
            if activity_start is not None and point[2] is not None and point[2]<activity_start:
                usable=collect(part) or usable;part=[];previous=None;continue
            if not math.isfinite(point[0]) or not math.isfinite(point[1]) or not -85<=point[0]<=85 or not -180<=point[1]<=180:
                usable=collect(part) or usable;part=[];previous=None;continue
            if previous and gps_discontinuity(previous,point):
                usable=collect(part) or usable;part=[]
            part.append([point[1],point[0]]);previous=point
        usable=collect(part) or usable
        activity_count+=int(usable)
        if len(batch_targets)>=64:
            targets.append(unary_union(batch_targets));contexts.append(unary_union(batch_contexts))
            batch_targets=[];batch_contexts=[]
    if batch_targets:targets.append(unary_union(batch_targets));contexts.append(unary_union(batch_contexts))
    if not targets:raise AppError(400,'No usable cycling/running tracks are available to define coverage.')
    target=unary_union(targets);context=unary_union(contexts)
    from pyproj import Geod
    area=abs(Geod(ellps='WGS84').geometry_area_perimeter(target)[0])/1e6
    return {'footprint':mapping(target),'context_geometry':mapping(context),'area_km2':round(area,1),'activity_count':activity_count,'buffer_km':buffer_km}


def choose_regions(geometry,regions):
    from shapely.geometry import shape
    # Greedy smallest-region cover, then discard redundant children when a parent was needed.
    target=shape(geometry);remaining=target
    choices=[]
    for region in regions:
        polygon=shape(region['geometry'])
        if polygon.intersects(target):choices.append((polygon.area,region['id'],polygon,region))
    selected=[]
    for _,_,polygon,region in sorted(choices,key=lambda entry:(entry[0],entry[1])):
        if remaining.is_empty:break
        overlap=remaining.intersection(polygon)
        if overlap.area<=1e-10:continue
        selected.append((polygon,region));remaining=remaining.difference(polygon)
    result=[]
    for polygon,region in selected:
        other=[p for p,r in selected if p.area>polygon.area]
        from shapely.ops import unary_union
        if other and target.intersection(polygon).difference(unary_union(other)).area<=1e-10:continue
        result.append(region)
    if not result:raise AppError(400,'No regional extracts cover these tracks.')
    if len(result)>100:raise AppError(400,'Tracks span more than 100 regional extracts; use manual regions or a smaller activity archive.')
    return sorted(result,key=lambda r:r['id']),not remaining.is_empty


@lru_cache(maxsize=4)
def footprint_shape(encoded):
    from shapely.geometry import shape
    return shape(json.loads(encoded))


def activity_outside_corridor(db,generation,activity_id):
    import numpy as np
    from shapely import intersects_xy
    row=db.execute('SELECT mode,footprint FROM network_generations WHERE id=?',(generation,)).fetchone()
    if not row or row[0]!='tracks' or not row[1]:return False
    from garmin_library.network_matching import continuous_samples
    records=db.execute("""SELECT p.seq,p.lon,p.lat,p.timestamp,a.start_time FROM track_points p
        JOIN activities a ON a.id=p.activity_id WHERE p.activity_id=? ORDER BY p.seq""",(activity_id,)).fetchall()
    records=[r for r in records if point_time(r['start_time']) is None or point_time(r['timestamp']) is None or point_time(r['timestamp'])>=point_time(r['start_time'])]
    points=[[p['lon'],p['lat']] for part in continuous_samples(records) for p in part]
    if not points:return False
    mask=footprint_shape(row[1])
    values=np.array([tuple(p) for p in points],dtype=float)
    valid=np.isfinite(values).all(axis=1)&(np.abs(values[:,1])<=85)&(np.abs(values[:,0])<=180)
    return bool(valid.any() and not intersects_xy(mask,values[valid,0],values[valid,1]).all())
