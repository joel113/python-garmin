"""Street/path coverage endpoints; local setup mutations share origin checks."""
import json
import logging
import sqlite3
import urllib.request
from contextlib import closing
from urllib.parse import parse_qs,urlsplit

from garmin_library.errors import AppError
from garmin_library.network_coverage import cancel_staging,retry,section_detail,start_setup,start_track_setup,preview_track_setup,status,vector_tile
from garmin_library.network_maps import catalog

LOG=logging.getLogger(__name__)


def handle(handler,method):
    context=handler.context
    try:
        parsed=urlsplit(handler.path);parts=parsed.path.strip('/').split('/');query=parse_qs(parsed.query)
        sport=query.get('sport',['all'])[0]
        if sport not in {'all','cycling','running'}:raise AppError(400,'Choose cycling, running, or all sports.')
        if method=='GET':
            if parts==['api','network-coverage','regions']:
                return handler.send_json([{'id':r['id'],'name':r['name'],'parent':r['parent']} for r in catalog(context)])
            if parts==['api','network-coverage','region-size']:
                rid=query.get('id',[''])[0];region=next((r for r in catalog(context) if r['id']==rid),None)
                if region is None:raise AppError(404,'Region not found.')
                with urllib.request.urlopen(urllib.request.Request(region['url'],method='HEAD'),timeout=30) as response:
                    size=int(response.headers.get('Content-Length','0'))
                return handler.send_json({'id':rid,'bytes':size,'reserve_bytes':size*6})
            with closing(context.connect()) as db:
                db.execute('BEGIN')
                if len(parts)==2:return handler.send_json(status(db))
                if len(parts)==4 and parts[2]=='sections':
                    try:sid=int(parts[3]);page=max(0,int(query.get('page',[0])[0]))
                    except ValueError:raise AppError(400,'Invalid section or page.')
                    return handler.send_json(section_detail(db,sid,sport,page))
                if len(parts)==6 and parts[2]=='tiles':
                    try:z,x,y=int(parts[3]),int(parts[4]),int(parts[5].removesuffix('.pbf'))
                    except ValueError:raise AppError(400,'Invalid tile.')
                    if not 0<=z<=20 or not 0<=x<2**z or not 0<=y<2**z:raise AppError(400,'Invalid tile.')
                    data=vector_tile(context,db,z,x,y,sport,query.get('unvisited',['1'])[0]=='1',query.get('generation',[None])[0])
                    handler.send_response(200);handler.send_header('Content-Type','application/vnd.mapbox-vector-tile')
                    handler.send_header('Content-Length',str(len(data)));handler.end_headers();handler.wfile.write(data)
                    return
            raise AppError(404,'Coverage endpoint not found.')
        if method!='POST':raise AppError(405,'Method not allowed.')
        payload=handler.mutation_payload()
        if parts[2:]==['preview']:
            return handler.send_json(preview_track_setup(context,payload.get('buffer_km',2)))
        if parts[2:]==['setup']:
            generation=(start_track_setup(context,payload.get('preview_id'),payload.get('revision')) if payload.get('mode')=='tracks' else start_setup(context,payload.get('regions'),payload.get('revision')))
            return handler.send_json({'generation':generation},202)
        if parts[2:]==['retry']:retry(context,payload.get('revision'))
        elif parts[2:]==['cancel']:cancel_staging(context,payload.get('revision'))
        else:raise AppError(404,'Coverage endpoint not found.')
        with closing(context.connect()) as db:return handler.send_json(status(db),202)
    except AppError as error:handler.send_json({'error':error.message},error.status)
    except sqlite3.OperationalError as error:
        if 'locked' in str(error).lower():
            LOG.warning('Coverage database is busy: %s',error)
            handler.send_json({'error':'The activity database is busy importing data. Status will refresh automatically; the import continues.'},503)
        else:
            LOG.exception('Coverage database request failed')
            handler.send_json({'error':'Coverage database request failed. Check the app logs for details.'},503)
    except Exception:
        LOG.exception('Coverage request failed')
        handler.send_json({'error':'Coverage request failed. Check the local matching service and retry.'},503)
