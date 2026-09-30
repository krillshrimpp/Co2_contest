"""Kakao Directions adapter. Matrices can be exported explicitly by the user."""
import copy
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request


def parse_route(data):
    route = data["routes"][0]
    if route["result_code"] != 0:
        raise ValueError("카카오 경로 조회 실패: " + route.get("result_msg", "결과 없음"))
    path = []
    for section in route.get("sections", []):
        for road in section.get("roads", []):
            vertices = road.get("vertexes", [])
            path.extend([[vertices[i + 1], vertices[i]] for i in range(0, len(vertices) - 1, 2)])
    return {"km": route["summary"]["distance"] / 1000,
            "minutes": route["summary"]["duration"] / 60, "path": path}


def directions(a, b, key):
    params = urllib.parse.urlencode({"origin": f"{a['lng']},{a['lat']}",
                                    "destination": f"{b['lng']},{b['lat']}",
                                    "priority": "RECOMMEND", "summary": "false", "avoid": "ferries"})
    request = urllib.request.Request("https://apis-navi.kakaomobility.com/v1/directions?" + params,
                                     headers={"Authorization": "KakaoAK " + key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return parse_route(json.load(response))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"카카오 HTTP {exc.code}: REST API 키, 사용 권한, 쿼터를 확인하세요.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, PermissionError):
            raise ValueError("카카오 접속이 실행 환경의 네트워크 권한에 의해 차단되었습니다. 외부 네트워크 접근이 허용된 터미널에서 run.ps1 또는 python app.py를 실행하세요.") from None
        raise ValueError("카카오 서버에 연결하지 못했습니다. 네트워크, 프록시, 인증서 설정을 확인하세요.") from None
    except TimeoutError:
        raise ValueError("카카오 연결 실패 또는 응답 시간 초과. 네트워크를 확인하세요.") from None


def build_matrix(schools, key, progress=lambda *_: None):
    if not key:
        raise ValueError("카카오 REST API 키를 먼저 연결하세요.")
    pairs = {}
    total = len(schools) * (len(schools) - 1)
    done = 0
    for a in schools:
        for b in schools:
            if a["id"] == b["id"] or (a["lat"], a["lng"]) == (b["lat"], b["lng"]):
                result = {"km": 0, "minutes": 0, "path": [[a["lat"], a["lng"]]]}
            else:
                result = directions(a, b, key)
            pairs[a["id"] + "|" + b["id"]] = result
            if a["id"] != b["id"]:
                done += 1
                progress(done / max(1, total), f"카카오 도로 조회 {done}/{total}")
    return {"source": "kakao", "label": "카카오 자동차 경로, 조회 시점 교통, 버스 진입 미검증",
            "fetched_at": datetime.datetime.now().astimezone().isoformat(),
            "pairs": pairs, "schools": copy.deepcopy(schools)}
