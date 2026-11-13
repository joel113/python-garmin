"""Small-section geometry and interval operations for street coverage."""
import math

from garmin_library.geometry import distance_between


def merge_ranges(ranges):
    result=[]
    for start,end in sorted((max(0,min(a,b)),min(1,max(a,b))) for a,b in ranges):
        if end-start<1e-8:continue
        if result and start<=result[-1][1]+1e-7:result[-1][1]=max(result[-1][1],end)
        else:result.append([start,end])
    return result


def complement_ranges(ranges):
    result=[];start=0
    for low,high in merge_ranges(ranges):
        if low>start+1e-7:result.append([start,low])
        start=high
    if start<1-1e-7:result.append([start,1])
    return result


def split_way(coordinates, maximum=25):
    """Split [lon,lat] ways by ground distance, retaining corners and tiny end parts."""
    if len(coordinates)<2:return
    part=[coordinates[0]];length=0.0
    for target in coordinates[1:]:
        current=part[-1]
        remaining=distance_between(current[::-1],target[::-1])
        if remaining<.001:continue
        while length+remaining>maximum+1e-6:
            fraction=(maximum-length)/remaining
            middle=[current[i]+(target[i]-current[i])*fraction for i in (0,1)]
            part.append(middle);yield part,maximum
            part=[middle];current=middle;remaining=distance_between(current[::-1],target[::-1]);length=0
        part.append(target);length+=remaining
    if length>.001:yield part,length


def projected_line(coordinates):
    from shapely.geometry import LineString
    lat=sum(p[1] for p in coordinates)/len(coordinates)
    scale=111195*math.cos(math.radians(lat))
    return LineString([(p[0]*scale,p[1]*111195) for p in coordinates]),scale


def projected_point(point,scale):
    from shapely.geometry import Point
    return Point(point[0]*scale,point[1]*111195)


def decode_polyline(encoded,precision=6):
    result=[];index=lat=lon=0
    while index<len(encoded):
        values=[]
        for _ in range(2):
            shift=value=0
            while True:
                if index>=len(encoded):raise ValueError('Incomplete matched geometry')
                byte=ord(encoded[index])-63;index+=1;value|=(byte&31)<<shift;shift+=5
                if byte<32:break
                if shift>35:raise ValueError('Invalid matched geometry')
            values.append(~(value>>1) if value&1 else value>>1)
        lat+=values[0];lon+=values[1];result.append([lon/10**precision,lat/10**precision])
    return result
