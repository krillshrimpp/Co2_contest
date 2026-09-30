import copy
import csv
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def defaults():
    return {
        "seed": 42, "student_count": 1000, "school_count": 22,
        "continuation_rate": 0.25, "cohort_size": 24, "cohort_min": 1, "cohort_max": 40,
        "class_starts": [570, 720, 870], "class_minutes": 50,
        "day_start": 450, "day_end": 1110, "return_home": True,
        "arrival_buffer": 5, "early_arrival": 25, "max_ride": 90,
        "max_wait": 35, "stop_minutes": 1, "boarding_seconds": 3,
        "road_factor": 1.35, "speed_kmh": 30, "traffic_factor": 1,
        "electricity_factor": 0.45, "charging_efficiency": 0.9,
        "electricity_price": 180, "max_seconds": 20, "batch_size": 4,
        "max_events": 20, "depot_id": "", "max_vehicles": 150,
        "carbon_time_weight": 0.012, "time_distance_weight": 0.2,
        "cost_runs_per_day": 4,
        "baseline": [
            {"id": "car", "name": "자가용", "share": 0.3,
             "manufacturing": 9000, "lifetime_km": 200000,
             "operation_per_km": 0.22, "occupancy": 1.5,
             "distance_factor": 1, "empty_ratio": 0.5,
             "speed_kmh": 30, "wait_minutes": 0},
            {"id": "bus", "name": "시내버스", "share": 0.5,
             "manufacturing": 90000, "lifetime_km": 800000,
             "operation_per_km": 1.1, "occupancy": 20,
             "distance_factor": 1.3, "empty_ratio": 0,
             "speed_kmh": 18, "wait_minutes": 15},
            {"id": "rail", "name": "도시철도", "share": 0.2,
             "manufacturing": 6000000, "lifetime_km": 4000000,
             "operation_per_km": 5, "occupancy": 300,
             "distance_factor": 1.25, "empty_ratio": 0,
             "speed_kmh": 28, "wait_minutes": 18},
        ],
        "vehicles": [
            {"id": "ev4", "name": "전기 승용차", "capacity": 4,
             "kwh_per_km": 0.18, "usable_kwh": 50, "manufacturing": 12000,
             "lifetime_km": 200000, "daily_cost": 110000, "cost_per_km": 70},
            {"id": "ev9", "name": "전기 승합차", "capacity": 9,
             "kwh_per_km": 0.30, "usable_kwh": 70, "manufacturing": 20000,
             "lifetime_km": 250000, "daily_cost": 140000, "cost_per_km": 100},
            {"id": "ev15", "name": "전기 소형버스", "capacity": 15,
             "kwh_per_km": 0.50, "usable_kwh": 100, "manufacturing": 35000,
             "lifetime_km": 400000, "daily_cost": 180000, "cost_per_km": 150},
            {"id": "ev25", "name": "전기 중형버스", "capacity": 25,
             "kwh_per_km": 0.75, "usable_kwh": 150, "manufacturing": 55000,
             "lifetime_km": 500000, "daily_cost": 220000, "cost_per_km": 220},
            {"id": "ev40", "name": "전기 버스", "capacity": 40,
             "kwh_per_km": 1.05, "usable_kwh": 230, "manufacturing": 80000,
             "lifetime_km": 600000, "daily_cost": 270000, "cost_per_km": 300},
        ],
    }


def load_schools():
    path = ROOT / "한국교육시설안전원_초중등학교위치.csv"
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{"id": r["학교ID"], "name": r["학교명"],
                 "lat": float(r["위도"]), "lng": float(r["경도"]),
                 "address": r["소재지도로명주소"], "source_date": r["데이터기준일자"]}
                for r in csv.DictReader(f)
                if "부산" in r["시도교육청명"] and r["학교급구분"] == "고등학교"
                and r["운영상태"] == "운영"]


def haversine(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    x = math.sin((lat2 - lat1) / 2) ** 2
    x += math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b["lng"] - a["lng"]) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(x)))


def estimated_matrix(schools, config):
    pairs = {}
    for a in schools:
        for b in schools:
            km = haversine(a, b) * config["road_factor"]
            pairs[a["id"] + "|" + b["id"]] = {
                "km": km, "minutes": km / config["speed_kmh"] * 60,
                "path": [[a["lat"], a["lng"]], [b["lat"], b["lng"]]],
            }
    return {"source": "estimated", "label": "직선거리 기반 추정, 실제 도로 미반영",
            "pairs": pairs, "schools": copy.deepcopy(schools)}


def group_sizes(total, low, high, rng, bias="mixed"):
    """Random partition with exact total and hard per-group bounds."""
    total, low, high = int(total), int(low), int(high)
    sizes = []
    while total:
        options = [n for n in range(low, min(high, total) + 1)
                   if total == n or math.ceil((total - n) / high) <= (total - n) // low]
        if not options:
            raise ValueError("총인원을 지정한 그룹 최소/최대 인원으로 나눌 수 없습니다.")
        weights = [n if bias == "favorable" else 1 / n if bias == "adverse" else 1 for n in options]
        size = rng.choices(options, weights=weights)[0]
        sizes.append(size)
        total -= size
    rng.shuffle(sizes)
    return sizes


def make_scenario(config, school_ids=None):
    validate_config(config)
    rng = random.Random(config["seed"])
    available = load_schools()
    if school_ids is None:
        schools = rng.sample(available, min(config["school_count"], len(available)))
    else:
        chosen = set(school_ids)
        schools = [s for s in available if s["id"] in chosen]
    if len(schools) < 2:
        raise ValueError("최소 2개 학교를 선택하세요.")
    matrix = estimated_matrix(schools, config)
    cohorts = []
    if "cohort_min" in config or "cohort_max" in config:
        sizes = group_sizes(config["student_count"], config.get("cohort_min", 1),
                            config.get("cohort_max", config["cohort_size"]), rng)
    else:
        sizes = [min(config["cohort_size"], config["student_count"] - i)
                 for i in range(0, config["student_count"], config["cohort_size"])]
    for number in sizes:
        home = schools[len(cohorts) % len(schools)]
        start_index = rng.randrange(len(config["class_starts"]))
        schedule = []
        current = home
        for i in range(start_index, len(config["class_starts"])):
            if schedule and rng.random() >= config["continuation_rate"]:
                break
            start = config["class_starts"][i]
            ready = schedule[-1]["end"] if schedule else config["day_start"]
            choices = [s for s in schools if s["id"] != current["id"]
                       and matrix["pairs"][current["id"] + "|" + s["id"]]["minutes"] * config["traffic_factor"]
                       <= min(config["max_ride"], start - ready - config["arrival_buffer"] - 5)]
            if not choices:
                raise ValueError(f"{current['name']}: 현재 수업 시간/탑승 제한으로 생성할 수 있는 목적지가 없습니다.")
            dest = rng.choices(choices, weights=[1 / (2 + haversine(current, s)) for s in choices])[0]
            schedule.append({"school_id": dest["id"], "start": start,
                             "end": start + config["class_minutes"]})
            current = dest
        cohorts.append({"id": f"G{len(cohorts) + 1:03}", "home": home["id"],
                        "students": number, "classes": schedule})
    return {"version": 1, "config": copy.deepcopy(config), "schools": schools,
            "cohorts": cohorts, "note": "학교 위치는 제공 CSV, 수요, 차량, 탄소 상수는 시연용 가정"}


def number(value, name, low=0, high=1e12, integer=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name}: 유한한 숫자가 필요합니다.")
    if not low <= value <= high or (integer and value != int(value)):
        raise ValueError(f"{name}: {low}~{high} 범위{'의 정수' if integer else ''}가 필요합니다.")


def validate_config(c):
    for name in ("student_count", "school_count", "cohort_size", "batch_size", "max_events", "max_vehicles"):
        number(c[name], name, 1, 10000, True)
    number(c["school_count"], "학교 수", 2, 142, True)
    low, high = c.get("cohort_min", 1), c.get("cohort_max", c["cohort_size"])
    number(low, "그룹 최소 인원", 1, 10000, True)
    number(high, "그룹 최대 인원", low, 10000, True)
    number(c["max_events"], "운행당 사건 수", 2, 40, True)
    number(c["max_seconds"], "대안당 탐색 시간", 0.01, 120)
    number(c["carbon_time_weight"], "탄소안 탑승시간 가중치", 0, 1000)
    number(c["time_distance_weight"], "시간안 주행거리 가중치", 0, 1000)
    number(c["cost_runs_per_day"], "비용안 일일 운행횟수 가정", 1, 100)
    number(c["seed"], "난수 seed", 0, 2**32, True)
    for name in ("speed_kmh", "road_factor", "traffic_factor", "max_ride", "early_arrival", "class_minutes"):
        number(c[name], name, 0.01, 1000)
    number(c["continuation_rate"], "연속 수업 비율", 0, 1)
    number(c["charging_efficiency"], "충전효율", 0.01, 1)
    for name in ("day_start", "day_end", "arrival_buffer", "max_wait", "stop_minutes", "boarding_seconds", "electricity_factor", "electricity_price"):
        number(c[name], name, 0, 100000)
    if not 0 <= c["day_start"] < c["day_end"] <= 1440:
        raise ValueError("운행 시작/종료는 같은 날 00:00~24:00 안에서 순서대로 설정하세요.")
    if not isinstance(c["return_home"], bool):
        raise ValueError("소속 학교 복귀 설정은 true/false여야 합니다.")
    if c["early_arrival"] < c["arrival_buffer"]:
        raise ValueError("최대 조기 도착 시간은 도착 여유시간 이상이어야 합니다.")
    if not c["class_starts"] or c["class_starts"] != sorted(set(c["class_starts"])):
        raise ValueError("수업 시작 시각은 중복 없이 오름차순이어야 합니다.")
    for start in c["class_starts"]:
        number(start, "수업 시작", c["day_start"], c["day_end"] - c["class_minutes"])
    if not c["vehicles"] or len(c["vehicles"]) > 12:
        raise ValueError("차종은 1~12개를 지정하세요.")
    if len({v["id"] for v in c["vehicles"]}) != len(c["vehicles"]):
        raise ValueError("차종 ID는 고유해야 합니다.")
    for v in c["vehicles"]:
        number(v["capacity"], "학생용 좌석", 1, 100, True)
        for name in ("kwh_per_km", "usable_kwh", "lifetime_km"):
            number(v[name], f"차량 {name}", 0.001)
        for name in ("manufacturing", "daily_cost", "cost_per_km"):
            number(v[name], f"차량 {name}")
    if not c["baseline"] or abs(sum(m["share"] for m in c["baseline"]) - 1) > 1e-6:
        raise ValueError("기존 이동수단 비율의 합은 1(100%)이어야 합니다.")
    for m in c["baseline"]:
        number(m["share"], "이동수단 비율", 0, 1)
        for name in ("lifetime_km", "occupancy", "distance_factor", "speed_kmh"):
            number(m[name], f"기존 이동 {name}", 0.001)
        for name in ("manufacturing", "operation_per_km", "empty_ratio", "wait_minutes"):
            number(m[name], f"기존 이동 {name}")


def validate_scenario(s):
    c = s["config"]
    validate_config(c)
    ids = [school["id"] for school in s["schools"]]
    if not 2 <= len(ids) <= 142 or len(set(ids)) != len(ids):
        raise ValueError("학교는 중복 ID 없이 2~142개여야 합니다.")
    for school in s["schools"]:
        number(school["lat"], "위도", -90, 90)
        number(school["lng"], "경도", -180, 180)
    if c["depot_id"] and c["depot_id"] not in ids:
        raise ValueError("차량 거점이 선택한 학교에 없습니다.")
    seen = set()
    for g in s["cohorts"]:
        if g["id"] in seen:
            raise ValueError("학생 그룹 ID가 중복됩니다.")
        seen.add(g["id"])
        number(g["students"], "그룹 인원", 1, 10000, True)
        if g["home"] not in ids or not g["classes"]:
            raise ValueError(f"{g['id']}: 소속 학교와 하나 이상의 수업이 필요합니다.")
        last_end = c["day_start"]
        for cl in g["classes"]:
            if cl["school_id"] not in ids:
                raise ValueError(f"{g['id']}: 수업 학교가 선택 목록에 없습니다.")
            number(cl["start"], "수업 시작", last_end, c["day_end"])
            number(cl["end"], "수업 종료", cl["start"] + 0.01, c["day_end"])
            last_end = cl["end"]
    if not s["cohorts"] or sum(g["students"] for g in s["cohorts"]) > 10000:
        raise ValueError("총 학생 수는 1~10,000명이어야 합니다.")
