import http.cookiejar
import json
import threading
import time
import tempfile
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from public_server import make_server
from shuttle.data import defaults, make_scenario, estimated_matrix


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(0, 'https://demo.example.com', roads_path=None)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.a = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.b = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, client, path, body=None, **headers):
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', **headers})
        with client.open(request, timeout=10) as response:
            return json.load(response)

    def test_public_sessions_and_calculation(self):
        with patch('app.load_keys', side_effect=AssertionError('public server must not read keys')):
            boot = self.request(self.a, '/api/bootstrap')
        self.assertTrue(boot['public_mode'])
        self.assertFalse(boot['rest_key_set'])
        self.assertEqual(boot['js_key'], '')
        config = boot['config']
        config.update(school_count=4, student_count=20)
        scenario = self.request(self.a, '/api/generate', {'config': config}, Origin='https://demo.example.com')
        with patch('app.build_matrix', side_effect=AssertionError('Kakao must not be called')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(self.a, '/api/matrix', {'source': 'kakao', 'scenario': scenario})
            self.assertEqual(error.exception.code, 403)
        self.request(self.a, '/api/matrix', {'source': 'estimated', 'scenario': scenario})
        self.assertEqual(self.request(self.a, '/api/matrix')['source'], 'estimated')
        self.assertIn('error', self.request(self.b, '/api/matrix'))
        job = self.request(self.a, '/api/solve', {'source': 'estimated', 'scenario': scenario})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(self.b, '/api/jobs/' + job['job_id'])
        self.assertEqual(error.exception.code, 404)
        for _ in range(100):
            result = self.request(self.a, '/api/jobs/' + job['job_id'])
            if result['status'] != 'running':
                break
            time.sleep(.05)
        self.assertEqual(result['status'], 'done')
        export = self.request(self.a, '/api/export', {'kind': 'results', 'data': result['result']})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(self.b, export['url'])
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(self.request(self.a, export['url']), result['result'])

    def test_public_map_key_does_not_enable_routing(self):
        self.server.map_js_key = 'public-javascript-test-key'
        boot = self.request(self.a, '/api/bootstrap')
        self.assertEqual(boot['js_key'], 'public-javascript-test-key')
        self.assertFalse(boot['rest_key_set'])
        with patch('app.build_matrix', side_effect=AssertionError('Kakao must not be called')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(self.a, '/api/matrix', {'source': 'kakao', 'scenario': make_scenario(boot['config'])})
            self.assertEqual(error.exception.code, 403)

    def test_reject_foreign_origin_host_and_private_files(self):
        for path, body, headers in [('/api/bootstrap', None, {'Host': 'evil.example'}),
                                    ('/api/generate', {}, {'Origin': 'https://evil.example'}),
                                    ('/.local/kakao-keys.json', None, {})]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(self.a, path, body, **headers)
            self.assertIn(error.exception.code, (403, 404))

    def test_default_roads_match_initial_school_selection(self):
        config = defaults()
        config.update(school_count=4, student_count=20)
        scenario = make_scenario(config)
        matrix = estimated_matrix(scenario['schools'], config)
        matrix['source'] = 'kakao'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roads.json'
            path.write_text(json.dumps({'format': 'shuttle-roads', 'version': 1, 'matrix': matrix}), encoding='utf-8')
            self.server.shutdown()
            self.server.server_close()
            self.server = make_server(0, 'https://demo.example.com', path)
            self.base = f'http://127.0.0.1:{self.server.server_port}'
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            boot = self.request(self.a, '/api/bootstrap')
            self.assertEqual({s['id'] for s in boot['default_scenario']['schools']}, {s['id'] for s in matrix['schools']})
            self.assertEqual(self.request(self.a, '/api/matrix'), matrix)
            self.request(self.a, '/api/matrix', {'scenario': scenario, 'source': 'estimated'})
            self.assertEqual(self.request(self.a, '/api/matrix')['source'], 'estimated')
            self.assertEqual(self.request(self.b, '/api/matrix')['source'], 'kakao')
