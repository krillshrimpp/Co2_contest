"""Isolated, temporary public demo. Kakao requests and keys are unavailable."""
import argparse
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from app import Handler, start_job, ROOT
from shuttle.data import defaults, make_scenario, validate_scenario
from shuttle.planner import validate_matrix


class PublicHandler(Handler):
    public_mode = True

    @property
    def state(self):
        return self.session_state

    def allowed_origins(self):
        return super().allowed_origins() | {self.server.public_origin}

    def open_session(self):
        if not self.valid_host():
            self.send({"error": "허용되지 않은 호스트입니다."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.allowed_origins():
            self.send({"error": "다른 사이트의 요청은 허용하지 않습니다."}, 403)
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            pass
        token = cookie.get("shuttle_session")
        token = token.value if token else None
        now = time.monotonic()
        with self.server.sessions_lock:
            for key, value in list(self.server.sessions.items()):
                if now - value["seen"] > 3600 and not any(j["status"] == "running" for j in value["state"]["jobs"].values()):
                    del self.server.sessions[key]
            if token not in self.server.sessions:
                if len(self.server.sessions) >= 40:
                    self.send({"error": "접속 인원이 많습니다. 잠시 후 다시 접속하세요."}, 503)
                    return False
                token = secrets.token_urlsafe(32)
                self.server.sessions[token] = {"seen": now, "state": {
                    "matrix": self.server.default_matrix, "jobs": {}, "exports": {}, "rest_key": "", "js_key": ""}}
                secure = "; Secure" if self.headers.get("Host") == urlparse(self.server.public_origin).netloc else ""
                self.session_cookie = f"shuttle_session={token}; Path=/; HttpOnly; SameSite=Lax{secure}"
            session = self.server.sessions[token]
            session["seen"] = now
            self.session_state = session["state"]
        return True

    def do_GET(self):
        if self.open_session():
            super().do_GET()

    def do_POST(self):
        if self.open_session():
            super().do_POST()

    def launch_job(self, kind, task):
        if not self.server.compute_slots.acquire(blocking=False):
            raise ValueError("다른 계산이 진행 중입니다. 잠시 후 다시 실행하세요.")

        def limited_task(progress):
            try:
                return task(progress)
            finally:
                self.server.compute_slots.release()
        try:
            return start_job(kind, limited_task, self.state)
        except Exception:
            self.server.compute_slots.release()
            raise


def make_server(port, origin, roads_path=ROOT / 'roads_test.json'):
    parsed = urlparse(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("공개 주소는 HTTPS origin이어야 합니다.")
    matrix = scenario = None
    if roads_path is not None:
        bundle = json.loads(roads_path.read_text(encoding='utf-8-sig'))
        if bundle.get('format') != 'shuttle-roads' or bundle.get('version') != 1:
            raise ValueError('roads_test.json 도로 파일 형식이 올바르지 않습니다.')
        matrix = bundle['matrix']
        if matrix.get('source') not in ('kakao', 'estimated'):
            raise ValueError('기본 도로 데이터 출처가 올바르지 않습니다.')
        config = defaults()
        config['school_count'] = len(matrix['schools'])
        scenario = make_scenario(config, [s['id'] for s in matrix['schools']])
        if {s['id'] for s in scenario['schools']} != {s['id'] for s in matrix['schools']}:
            raise ValueError('기본 도로 파일에 학교 목록에 없는 학교가 있습니다.')
        scenario['schools'] = matrix['schools']
        validate_scenario(scenario)
        validate_matrix(scenario, matrix)
    server = ThreadingHTTPServer(("127.0.0.1", port), PublicHandler)
    server.default_matrix = matrix
    server.default_scenario = scenario
    server.public_origin = origin.rstrip("/")
    server.sessions = {}
    server.sessions_lock = threading.Lock()
    server.compute_slots = threading.BoundedSemaphore(2)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    server = make_server(args.port, args.origin)
    print(f"Public demo origin: {server.public_origin}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
