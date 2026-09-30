"""Run with Python 3.10+: python app.py. No third-party runtime dependencies."""
import argparse
import copy
import csv
import io
import json
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from shuttle.data import defaults, estimated_matrix, load_schools, make_scenario, validate_scenario
from shuttle.kakao import build_matrix
from shuttle.planner import compare, validate_matrix
from shuttle.report import render_report
from shuttle.scenarios import generate_case, test_cases

ROOT = Path(__file__).resolve().parent
KEY_FILE = ROOT / ".local" / "kakao-keys.json"


def load_keys():
    stored = {}
    if KEY_FILE.exists():
        try:
            stored = json.loads(KEY_FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(stored, dict) or any(not isinstance(stored.get(k, ""), str) for k in ("rest_key", "js_key")):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError(".local/kakao-keys.json 파일을 읽을 수 없습니다. JSON 형식과 파일 권한을 확인하세요.") from None
    return {k: stored.get(k, "").strip() for k in ("rest_key", "js_key")}


STATE = {**load_keys(), "matrix": None, "jobs": {}, "exports": {}}
LOCK = threading.Lock()


def start_job(kind, task, state=None):
    state = STATE if state is None else state
    with LOCK:
        if any(j["status"] == "running" for j in state["jobs"].values()):
            raise ValueError("현재 계산/조회가 진행 중입니다. 완료 후 다시 실행하세요.")
        job_id = secrets.token_hex(8)
        state["jobs"].clear()
        state["jobs"][job_id] = {"status": "running", "kind": kind, "progress": 0, "message": "준비 중"}
    def progress(value, message):
        with LOCK:
            state["jobs"][job_id].update(progress=value, message=message)
    def run():
        try:
            result = task(progress)
            with LOCK:
                state["jobs"][job_id].update(status="done", progress=1, result=result)
        except Exception as exc:
            traceback.print_exc()
            with LOCK:
                state["jobs"][job_id].update(status="error", error=str(exc))
    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


class Handler(BaseHTTPRequestHandler):
    public_mode = False

    @property
    def state(self):
        return STATE

    def launch_job(self, kind, task):
        return start_job(kind, task, self.state)

    def allowed_origins(self):
        return {f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}

    def log_message(self, *_):
        pass

    def send(self, value, status=200, content_type="application/json; charset=utf-8", filename=None):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "session_cookie", None):
            self.send_header("Set-Cookie", self.session_cookie)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get("Host", "") in {urlparse(origin).netloc for origin in self.allowed_origins()}

    def do_GET(self):
        if not self.valid_host():
            return self.send({"error": "로컬 호스트만 허용합니다."}, 403)
        path = urlparse(self.path).path
        if path.startswith("/api/export/"):
            export = self.state["exports"].get(path.split("/")[-1])
            if not export:
                return self.send({"error": "내보내기 파일이 만료되었습니다. 다시 저장하세요."}, 404)
            return self.send(export["data"], content_type=export["type"], filename=export["name"])
        if path == "/api/bootstrap":
            try:
                if not self.public_mode:
                    self.state.update(load_keys())
            except ValueError as exc:
                return self.send({"error": str(exc)}, 400)
            return self.send({"config": defaults(), "schools": load_schools(),
                              "rest_key_set": bool(self.state["rest_key"]), "js_key": self.state["js_key"], "public_mode": self.public_mode,
                              "default_scenario": getattr(self.server, "default_scenario", None)})
        if path.startswith("/api/jobs/"):
            with LOCK:
                data = copy.deepcopy(self.state["jobs"].get(path.split("/")[-1]))
            return self.send(data or {"error": "작업을 찾을 수 없습니다."}, 200 if data else 404)
        if path == "/api/matrix":
            return self.send(self.state["matrix"] or {"error": "도로 정보가 없습니다."})
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in files:
            filename, content_type = files[path]
            return self.send((ROOT / "web" / filename).read_bytes(), content_type=content_type)
        self.send({"error": "없는 경로입니다."}, 404)

    def do_POST(self):
        if not self.valid_host():
            return self.send({"error": "로컬 호스트만 허용합니다."}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in self.allowed_origins():
            return self.send({"error": "다른 사이트의 요청은 허용하지 않습니다."}, 403)
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send({"error": "JSON 요청만 허용합니다."}, 415)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 100_000_000:
                raise ValueError("입력 파일은 100MB 이하 JSON이어야 합니다.")
            body = json.loads(self.rfile.read(size))
            path = urlparse(self.path).path
            if path == "/api/export":
                kind = body["kind"]
                if kind == "csv":
                    plan, scenario = body["plan"], body["scenario"]
                    names = {s["id"]: s["name"] for s in scenario["schools"]}
                    rows = [["그룹", "이동순서", "시작학생번호", "인원", "차량", "출발학교", "도착학교",
                             "승차분", "하차분", "하차완료분", "탑승분", "대기분"]]
                    rows += [[r["group"], r["leg"] + 1, r["student_start"], r["count"], r["vehicle_id"],
                              names[r["origin"]], names[r["destination"]], r["pickup"], r["dropoff"],
                              r["arrival_complete"], r["ride_minutes"], r["wait_minutes"]] for r in plan["rides"]]
                    text = io.StringIO(newline="")
                    writer = csv.writer(text)
                    for row in rows:
                        writer.writerow(["'" + x if isinstance(x, str) and x.startswith(("=", "+", "-", "@")) else x for x in row])
                    data, content_type, name = text.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", "student-trips.csv"
                elif kind == "report":
                    results, matrix = body["results"], body["matrix"]
                    validate_scenario(results["scenario"])
                    validate_matrix(results["scenario"], matrix)
                    index = body["plan_index"]
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(results["plans"]):
                        raise ValueError("대안 선택이 올바르지 않습니다.")
                    data = render_report(results, matrix, index).encode("utf-8")
                    content_type, name = "text/html; charset=utf-8", "transport-report.html"
                elif kind in ("scenario", "results", "roads"):
                    data = json.dumps(body["data"], ensure_ascii=False, allow_nan=False, indent=2).encode()
                    content_type, name = "application/json; charset=utf-8", f"{kind}.json"
                else:
                    raise ValueError("지원하지 않는 내보내기 형식입니다.")
                export_id = secrets.token_hex(8)
                if len(self.state["exports"]) >= 5:
                    self.state["exports"].pop(next(iter(self.state["exports"])))
                self.state["exports"][export_id] = {"data": data, "type": content_type, "name": name}
                return self.send({"url": "/api/export/" + export_id, "filename": name})
            if path == "/api/matrix/import":
                scenario, matrix = body["scenario"], body["matrix"]
                validate_scenario(scenario)
                if not isinstance(matrix, dict) or matrix.get("source") not in ("kakao", "estimated"):
                    raise ValueError("지원하지 않는 도로 데이터입니다.")
                if not isinstance(matrix.get("schools"), list) or not isinstance(matrix.get("pairs"), dict):
                    raise ValueError("도로 데이터 구조가 올바르지 않습니다.")
                if any(not isinstance(p, dict) for p in matrix["pairs"].values()):
                    raise ValueError("도로 구간이 올바르지 않습니다.")
                validate_matrix(scenario, matrix)
                for pair in matrix["pairs"].values():
                    for point in pair.get("path", []):
                        if (not isinstance(point, list) or len(point) != 2
                                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in point)
                                or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180):
                            raise ValueError("도로 경로 좌표가 올바르지 않습니다.")
                with LOCK:
                    if any(j["status"] == "running" for j in self.state["jobs"].values()):
                        raise ValueError("현재 작업 완료 후 불러오세요.")
                    self.state["matrix"] = matrix
                return self.send({"source": matrix["source"]})
            if path == "/api/generate":
                scenario = make_scenario(body["config"], body.get("school_ids"))
                validate_scenario(scenario)
                return self.send(scenario)
            if path == "/api/validate":
                validate_scenario(body["scenario"])
                return self.send({"valid": True})
            if path in ("/api/scenario-case", "/api/scenario-test"):
                scenario = body["scenario"]
                validate_scenario(scenario)
                matrix = self.state["matrix"]
                if matrix is None or matrix["source"] != body.get("source"):
                    raise ValueError("선택한 도로 정보를 먼저 준비하세요.")
                if matrix["source"] == "estimated":
                    matrix = estimated_matrix(scenario["schools"], scenario["config"])
                validate_matrix(scenario, matrix)
                seed = body["seed"]
                if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
                    raise ValueError("난수는 0~4294967295 정수여야 합니다.")
                vary_modes = body.get("vary_modes", False)
                if not isinstance(vary_modes, bool):
                    raise ValueError("이동수단 변경 설정은 true/false여야 합니다.")
                vary_students = body.get("vary_students", False)
                if not isinstance(vary_students, bool):
                    raise ValueError("인원 변경 설정은 true/false여야 합니다.")
                if path == "/api/scenario-case":
                    return self.send(generate_case(scenario, matrix, body["profile"], seed, vary_modes, vary_students))
                repeats = body["repeats"]
                if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 3:
                    raise ValueError("유형별 시험 횟수는 1~3회입니다.")
                return self.send(self.launch_job("scenario-test", lambda progress:
                    test_cases(scenario, matrix, repeats, seed, vary_modes, progress, vary_students)))
            if path == "/api/matrix":
                scenario = body["scenario"]
                validate_scenario(scenario)
                if body.get("source") == "kakao":
                    if self.public_mode:
                        return self.send({"error": "공개 시연에서는 카카오 신규 조회를 사용할 수 없습니다. 도로 파일을 불러오세요."}, 403)
                    self.state.update(load_keys())
                    key = self.state["rest_key"]
                    if not key:
                        raise ValueError(".local/kakao-keys.json 파일의 rest_key를 입력하세요.")
                    def fetch(progress):
                        self.state["matrix"] = None
                        matrix = build_matrix(scenario["schools"], key, progress)
                        self.state["matrix"] = matrix
                        return {"source": matrix["source"], "label": matrix["label"]}
                    return self.send(self.launch_job("matrix", fetch))
                matrix = estimated_matrix(scenario["schools"], scenario["config"])
                self.state["matrix"] = matrix
                return self.send({"source": matrix["source"], "label": matrix["label"]})
            if path == "/api/solve":
                scenario = body["scenario"]
                validate_scenario(scenario)
                matrix = self.state["matrix"]
                if matrix is None:
                    raise ValueError("도로 정보를 먼저 준비하세요.")
                if body.get("source", matrix["source"]) != matrix["source"]:
                    raise ValueError("선택한 도로 모드와 준비된 데이터가 다릅니다. 도로 정보를 다시 준비하세요.")
                # Estimates always reflect edited speed/road factor. Kakao data stays a snapshot.
                if matrix["source"] == "estimated":
                    matrix = estimated_matrix(scenario["schools"], scenario["config"])
                    self.state["matrix"] = matrix
                validate_matrix(scenario, matrix)
                return self.send(self.launch_job("solve", lambda progress: compare(scenario, matrix, progress)))
            self.send({"error": "없는 경로입니다."}, 404)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            self.send({"error": str(exc)}, 400)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"School shuttle demo: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
