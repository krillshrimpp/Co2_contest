import json
import tempfile
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_keys = tempfile.TemporaryDirectory()
        cls.key_patch = patch.object(app, "KEY_FILE", Path(cls.temp_keys.name) / "keys.json")
        cls.key_patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.key_patch.stop()
        cls.temp_keys.cleanup()

    def setUp(self):
        app.KEY_FILE.write_text('{}', encoding='utf-8')

    def request(self, path, body=None, **headers):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_full_http_flow(self):
        boot = self.request("/api/bootstrap")
        self.assertEqual(len(boot["schools"]), 68)
        config = boot["config"]
        config["student_count"] = 30
        scenario = self.request("/api/generate", {"config": config})
        self.request("/api/matrix", {"scenario": scenario, "source": "estimated"})
        result = self.request("/api/solve", {"scenario": scenario})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.request("/api/jobs/" + result["job_id"])
            if job["status"] != "running":
                break
            time.sleep(.05)
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["result"]["plans"]), 3)
        self.assertTrue(all(p["status"] == "feasible" for p in job["result"]["plans"]))
        export = self.request("/api/export", {"kind": "csv", "plan": job["result"]["plans"][0], "scenario": scenario})
        with urllib.request.urlopen(self.base + export["url"]) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            csv_text = response.read().decode("utf-8-sig")
        self.assertIn("하차완료분", csv_text)
        self.assertIn("G001", csv_text)

    def test_road_file_roundtrip_and_reject_invalid_import(self):
        config = app.defaults()
        config.update(school_count=4, student_count=20)
        scenario = self.request("/api/generate", {"config": config})
        self.request("/api/matrix", {"scenario": scenario, "source": "estimated"})
        matrix = self.request("/api/matrix")
        data = {"format": "shuttle-roads", "version": 1, "matrix": matrix}
        export = self.request("/api/export", {"kind": "roads", "data": data})
        restored = self.request(export["url"])
        self.assertEqual(restored, data)
        app.STATE["matrix"] = None
        self.request("/api/matrix/import", {"scenario": scenario, "matrix": restored["matrix"]})
        self.assertEqual(self.request("/api/matrix"), matrix)
        invalid = json.loads(json.dumps(matrix))
        invalid["schools"][0]["lat"] += 1
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/matrix/import", {"scenario": scenario, "matrix": invalid})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.request("/api/matrix"), matrix)
        invalid = json.loads(json.dumps(matrix))
        next(iter(invalid["pairs"].values()))["path"] = [[91, 129]]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/matrix/import", {"scenario": scenario, "matrix": invalid})
        self.assertEqual(error.exception.code, 400)

    def test_scenario_export_roundtrip(self):
        scenario = {"test": "부산", "config": {"students": 1000}}
        export = self.request("/api/export", {"kind": "scenario", "data": scenario})
        self.assertEqual(self.request(export["url"]), scenario)

    def test_source_mismatch_is_blocked(self):
        c = app.defaults()
        c["student_count"] = 10
        s = self.request("/api/generate", {"config": c})
        self.request("/api/matrix", {"scenario": s, "source": "estimated"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/solve", {"scenario": s, "source": "kakao"})
        self.assertEqual(error.exception.code, 400)

    @patch("app.build_matrix", side_effect=ValueError("network blocked"))
    def test_failed_kakao_does_not_leave_estimated_matrix(self, _):
        saved = app.STATE["rest_key"]
        try:
            app.KEY_FILE.write_text(json.dumps({"rest_key": "test-only"}), encoding="utf-8")
            c = app.defaults()
            c["student_count"] = 10
            s = self.request("/api/generate", {"config": c})
            self.request("/api/matrix", {"scenario": s, "source": "estimated"})
            j = self.request("/api/matrix", {"scenario": s, "source": "kakao"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = self.request("/api/jobs/" + j["job_id"])
                if state["status"] != "running":
                    break
                time.sleep(.02)
            self.assertEqual(state["status"], "error")
            self.assertIsNone(app.STATE["matrix"])
        finally:
            app.STATE["rest_key"] = saved

    def test_file_keys_reload_without_exposing_rest_key(self):
        app.KEY_FILE.write_text(json.dumps({"rest_key": "test-secret", "js_key": "test-js"}), encoding="utf-8")
        response = self.request("/api/bootstrap")
        self.assertTrue(response["rest_key_set"])
        self.assertEqual(response["js_key"], "test-js")
        self.assertNotIn("test-secret", json.dumps(response))
        app.KEY_FILE.write_text('{}', encoding="utf-8")
        response = self.request("/api/bootstrap")
        self.assertFalse(response["rest_key_set"])
        self.assertEqual(response["js_key"], "")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/.local/kakao-keys.json")
        self.assertEqual(error.exception.code, 404)

    def test_web_cannot_save_keys(self):
        before = app.KEY_FILE.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/keys", {"rest_key": "test-secret"})
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(app.KEY_FILE.read_bytes(), before)

    def test_invalid_key_file_returns_visible_error(self):
        app.KEY_FILE.write_text('{', encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/bootstrap")
        self.assertEqual(error.exception.code, 400)

    def test_untrusted_origin_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/keys", {}, Origin="https://untrusted.invalid")
        self.assertEqual(error.exception.code, 403)

    def test_invalid_config_gets_visible_400(self):
        c = app.defaults()
        c["student_count"] = -5
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/generate", {"config": c})
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
