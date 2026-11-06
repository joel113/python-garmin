"""HTTP compatibility and mutations against ephemeral localhost servers."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from garmin_library.http import Handler
from garmin_library.server import ApplicationServer
from support import GPX, TemporaryLibrary, seed_baseline


class HTTPTests(TemporaryLibrary, unittest.TestCase):
    def setUp(self):
        super().setUp()
        seed_baseline(self.context)
        self.root.joinpath('web').mkdir()
        self.root.joinpath('web/index.html').write_text('<html>Temporary library</html>')
        logger = patch.object(Handler, 'log_message')
        logger.start(); self.addCleanup(logger.stop)
        self.server = ApplicationServer(('127.0.0.1', 0), self.context)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, method='GET', body=None, headers=None):
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        request = urllib.request.Request(self.base+path, data=body, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, json.loads(data) if response.headers.get_content_type()=='application/json' else data, response.headers

    def test_baseline_read_contracts(self):
        expected = json.loads(Path(__file__).with_name('fixtures').joinpath('api_baseline.json').read_text())
        def normalize(value):
            if isinstance(value,dict):
                return {k:normalize(v) for k,v in value.items() if k not in {'created_at','updated_at','imported_at','changed_at'}}
            if isinstance(value,list):return [normalize(v) for v in value]
            return value
        for path, baseline in expected.items():
            with self.subTest(path=path):
                status, data, headers = self.request(path)
                self.assertEqual({'status':status,'body':normalize(data),'cache':headers.get('Cache-Control')},baseline)
        status, body, _ = self.request('/')
        self.assertEqual(status,200)
        self.assertIn(b'Temporary library',body)

    def test_route_upload_collection_order_notes_and_download(self):
        status, collection, _ = self.request('/api/route-collections','POST',{'name':'Holiday'})
        self.assertEqual(status,201)
        cid=collection['id']
        status, uploaded, _ = self.request('/api/routes/upload?filename=holiday.gpx&collection_id='+cid,'POST',GPX,{'Content-Type':'application/gpx+xml'})
        self.assertEqual(status,201)
        route=uploaded['route'];rid=route['id']
        self.assertTrue(self.context.settings.routes_path.joinpath(rid+'.gpx').is_file())
        status, saved, _ = self.request('/api/routes/'+rid,'PUT',{'revision':route['revision'],'name':'Stage one','sport':'cycling','notes':'Pack lunch'})
        self.assertEqual(status,200)
        self.assertEqual(saved['notes'],'Pack lunch')
        self.assertEqual(self.request('/api/routes/'+rid+'/download')[1],GPX)
        self.assertIn(b'Pack lunch',self.request('/api/routes/'+rid+'/notes')[1])
        collection=self.request('/api/route-collections/'+cid)[1]
        self.assertEqual(self.request('/api/route-collections/'+cid+'/order','PUT',{'revision':collection['revision'],'route_ids':[rid]})[0],200)
        self.assertEqual(self.request('/api/route-collections/'+cid+'/order','PUT',{'revision':collection['revision'],'route_ids':[rid]})[0],409)
        self.assertEqual(self.request('/api/routes/'+rid,'DELETE',{'revision':saved['revision']})[0],200)
        self.assertFalse(self.context.settings.routes_path.joinpath(rid+'.gpx').exists())

    def test_segment_and_destination_mutations(self):
        status, segment, _ = self.request('/api/segments','POST',{'name':'Hill','sport':'cycling','radius_m':30,'checkpoints':[[48,11],[48.002,11.002]]})
        self.assertEqual(status,201)
        self.assertEqual(self.request('/api/segments/'+str(segment['id']),'DELETE',{'revision':segment['revision']})[0],200)
        self.assertEqual(self.request('/api/destinations/settings','PUT',{'epsilon_m':80,'min_trips':3})[0],202)

    def test_matching_settings_revision_and_security(self):
        state=self.request('/api/route-matching')[1]
        body={'revision':state['revision'],'tolerance_m':60,'partial_percent':25,'full_percent':95}
        self.assertEqual(self.request('/api/route-matching','PUT',body,{'Origin':'https://untrusted.invalid'})[0],403)
        self.assertEqual(self.request('/api/route-matching','PUT',body,{'Host':'untrusted.invalid'})[0],403)
        self.assertEqual(self.request('/api/route-matching','PUT',body,{'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/api/route-matching','PUT',body,{'Origin':self.base})[0],200)
        self.assertEqual(self.request('/api/route-matching','PUT',body)[0],409)
        self.assertEqual(self.request('/api/route-matching','PUT',b'{broken',{'Content-Type':'application/json'})[0],400)
        self.assertEqual(self.request('/api/route-matching','PUT',b'no',{'Content-Type':'text/plain'})[0],415)

    def test_distance_settings_validation_disable_and_preserve_omitted_values(self):
        def save(**changes):
            state=self.request('/api/route-matching')[1]
            body={key:state[key] for key in ('revision','tolerance_m','partial_percent','full_percent')}
            body.update(changes)
            return self.request('/api/route-matching','PUT',body)
        for changes in ({'partial_km':0},{'partial_km':-1},{'partial_km':True},
                        {'partial_km':'5'},{'partial_km':float('inf')},
                        {'partial_km':20,'full_km':5}):
            with self.subTest(changes=changes):self.assertEqual(save(**changes)[0],400)
        status,state,_=save(partial_km=5,full_km=50)
        self.assertEqual(status,200)
        status,state,_=save(tolerance_m=60)
        self.assertEqual((state['partial_km'],state['full_km']),(5,50))
        status,state,_=save(partial_km=None,full_km=None)
        self.assertEqual(status,200)
        self.assertIsNone(state['partial_km']);self.assertIsNone(state['full_km'])
        self.assertEqual(state['status'],'pending')
