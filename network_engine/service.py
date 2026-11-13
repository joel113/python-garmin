"""Internal-only Valhalla build/matching service; never mounted to personal SQLite."""
import json
import os
import re
import shutil
import signal
import gc
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT=Path(os.environ.get('NETWORK_ROOT','/network'))
LOCK=threading.Lock()
ACTORS={}
BUILD_LOCK=threading.Lock()
PROCESSES={}
CANCELLED=set()


def folder(generation):
    if not isinstance(generation,str) or not re.fullmatch('[a-f0-9]{32}',generation):
        raise ValueError('Invalid generation')
    path=ROOT/'generations'/generation
    if path.is_symlink() or not path.resolve().is_relative_to((ROOT/'generations').resolve()):raise ValueError('Invalid generation path')
    return path


def save_status(path, value):
    target=path/'engine-status.json'
    temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(target)


def command(generation,args,log):
    with LOCK:
        if generation in CANCELLED:raise RuntimeError('Build cancelled')
        process=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        PROCESSES[generation]=process
    try:
        result=process.wait(timeout=7200)
        if result:raise RuntimeError(f'{args[0]} exited with code {result}')
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.wait();raise
    finally:
        with LOCK:PROCESSES.pop(generation,None)


def crop(generation,source,index):
    path=folder(generation);output=path/'crops'/f'{index}.osm.pbf'
    try:
        with BUILD_LOCK,(path/f'crop-{index}.log').open('w') as log:
            output.parent.mkdir(exist_ok=True)
            command(generation,['osmium','extract','--polygon',str(path/'clip.geojson'),'--strategy','complete_ways',
                str(source),'--overwrite','-o',str(output)],log)
            save_named_status(path/f'crop-{index}-status.json',{'status':'ready','bytes':output.stat().st_size})
    except Exception as error:
        save_named_status(path/f'crop-{index}-status.json',{'status':'failed','error':str(error)[:500]})


def save_named_status(target,value):
    temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(target)


def build(generation, sources):
    with BUILD_LOCK:
        build_graph(generation,sources)


def build_graph(generation, sources):
    path=folder(generation)
    try:
        with (path/'build.log').open('w') as log:
            history=path/'merged.osh.pbf';merged=path/'merged.osm.pbf'
            if len(sources)==1:
                shutil.copyfile(sources[0],merged)
            else:
                command(generation,['osmium','merge','--with-history',*sources,'--overwrite','-o',str(history)],log)
                command(generation,['osmium','time-filter',str(history),datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),'--overwrite','-o',str(merged)],log)
                history.unlink(missing_ok=True)
            config=json.loads(subprocess.check_output(['valhalla_build_config'],text=True))
            config['mjolnir'].update(tile_dir=str(path/'tiles'),tile_extract='',concurrency=2,
                                      admin=str(path/'admins.sqlite'),timezone=str(path/'timezones.sqlite'),
                                      max_cache_size=134217728,keep_osm_node_ids=True)
            config['meili']['default'].update(search_radius=50,gps_accuracy=20)
            for value in config.get('service_limits',{}).values():
                if isinstance(value,dict) and 'max_shape' in value:value['max_shape']=2000
            config_path=path/'valhalla.json';config_path.write_text(json.dumps(config))
            command(generation,['valhalla_build_tiles','-c',str(config_path),str(merged)],log)
            import valhalla
            actor=valhalla.Actor(str(config_path))
            with LOCK:
                if generation in CANCELLED:raise RuntimeError('Build cancelled')
                ACTORS[generation]=actor
                while len(ACTORS)>2:ACTORS.pop(next(iter(ACTORS)))
            save_status(path,{'status':'ready'})
    except Exception as error:
        save_status(path,{'status':'failed','error':str(error)[:500]})


def cleanup(generation,whole=False):
    path=folder(generation)
    if not BUILD_LOCK.acquire(blocking=False):raise ValueError('Build/crop in progress; cleanup will retry later')
    try:
        with LOCK:
            if whole:ACTORS.pop(generation,None)
            if generation in PROCESSES:raise ValueError('Generation is still processing')
        if whole:
            gc.collect()
            if path.exists():shutil.rmtree(path)
        else:
            for filename in ('merged.osh.pbf','merged.osm.pbf'):(path/filename).unlink(missing_ok=True)
            if (path/'crops').is_dir():shutil.rmtree(path/'crops')
        # Delete only files allocated by this app to this generation, never arbitrary OSM inputs.
        for source in (ROOT/'sources').glob(generation+'-*'):
            if re.fullmatch(generation+r'-[0-9]+\.(osm\.pbf|osm\.part)',source.name):source.unlink(missing_ok=True)
        retained=sum(item.stat().st_size for item in path.rglob('*') if item.is_file()) if path.exists() else 0
        return {'status':'cleaned','retained_bytes':retained}
    finally:BUILD_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    def answer(self,value,status=200):
        data=json.dumps(value).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)

    def do_GET(self):
        try:
            if self.path=='/status':return self.answer({'status':'ready','engine':'Valhalla 3.6.3'})
            if self.path.startswith('/crop/'):
                _,_,generation,index=self.path.split('/')
                index=int(index)
                target=folder(generation)/f'crop-{index}-status.json'
                return self.answer(json.loads(target.read_text()) if target.exists() else {'status':'missing'})
            if self.path.startswith('/build/'):
                target=folder(self.path.split('/')[-1])/'engine-status.json'
                return self.answer(json.loads(target.read_text()) if target.exists() else {'status':'missing'})
            self.answer({'error':'Not found'},404)
        except ValueError as error:self.answer({'error':str(error)},400)

    def do_POST(self):
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2*1024*1024:raise ValueError('Invalid body size')
            body=json.loads(self.rfile.read(length));generation=body['generation'];path=folder(generation)
            if self.path=='/cancel':
                with LOCK:
                    CANCELLED.add(generation)
                    process=PROCESSES.get(generation)
                    if process is not None and process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
                return self.answer({'status':'cancelled'})
            if self.path in {'/cleanup','/prune'}:
                return self.answer(cleanup(generation,self.path=='/prune'))
            if self.path=='/crop':
                index=body.get('index')
                if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<1000:raise ValueError('Invalid crop index')
                source=(ROOT/body['source']).resolve()
                if not source.is_relative_to((ROOT/'sources').resolve()) or not source.is_file():raise ValueError('Invalid crop source')
                if not (path/'clip.geojson').is_file():raise ValueError('Missing crop polygon')
                target=path/f'crop-{index}-status.json'
                with LOCK:
                    current=json.loads(target.read_text()) if target.exists() else {}
                    if current.get('status')=='building':return self.answer(current,202)
                    if current.get('status')=='ready' and (path/'crops'/f'{index}.osm.pbf').is_file():return self.answer(current)
                    CANCELLED.discard(generation)
                    save_named_status(target,{'status':'building'})
                threading.Thread(target=crop,args=(generation,source,index),daemon=True).start()
                return self.answer({'status':'building'},202)
            if self.path=='/build':
                sources=[]
                for relative in body['sources']:
                    source=(ROOT/relative).resolve()
                    if not (source.is_relative_to((ROOT/'sources').resolve()) or source.is_relative_to((path/'crops').resolve())) or not source.is_file():raise ValueError('Invalid source file')
                    sources.append(str(source))
                if not sources:raise ValueError('No sources selected')
                path.mkdir(parents=True,exist_ok=True)
                status_path=path/'engine-status.json'
                with LOCK:
                    status=json.loads(status_path.read_text()) if status_path.exists() else {}
                    if status.get('status')=='building':return self.answer(status,202)
                    CANCELLED.discard(generation)
                    save_status(path,{'status':'building'})
                threading.Thread(target=build,args=(generation,sources),daemon=True).start()
                return self.answer({'status':'building'},202)
            if self.path=='/trace':
                import valhalla
                with LOCK:
                    if generation not in ACTORS:
                        if not (path/'valhalla.json').is_file():raise ValueError('Graph not built')
                        ACTORS[generation]=valhalla.Actor(str(path/'valhalla.json'))
                        while len(ACTORS)>2:ACTORS.pop(next(iter(ACTORS)))
                    try:result=ACTORS[generation].trace_attributes(body['request'])
                    except Exception as error:return self.answer({'error':'Unmatchable trace','detail':str(error)[:500]},422)
                return self.answer(json.loads(result) if isinstance(result,str) else result)
            self.answer({'error':'Not found'},404)
        except (ValueError,KeyError,TypeError) as error:self.answer({'error':str(error)},400)
        except Exception as error:self.answer({'error':str(error)[:500]},503)


if __name__=='__main__':
    ROOT.mkdir(parents=True,exist_ok=True)
    # A killed build never pretends to remain running indefinitely after restart.
    for status in list(ROOT.glob('generations/*/engine-status.json'))+list(ROOT.glob('generations/*/crop-*-status.json')):
        if json.loads(status.read_text()).get('status')=='building':
            save_named_status(status,{'status':'failed','error':'Build interrupted; retry setup.'})
    ThreadingHTTPServer(('0.0.0.0',8002),Handler).serve_forever()
