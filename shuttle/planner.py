"""Deterministic insertion heuristic, with independent schedule validation.

This is a feasible-plan search, not a proof of global optimality.
"""
import collections
import math
import time
from .data import validate_scenario


class Network:
    def __init__(self, matrix, config):
        self.pairs = matrix["pairs"]
        self.factor = config["traffic_factor"]

    def edge(self, a, b):
        try:
            p = self.pairs[a + "|" + b]
        except KeyError:
            raise ValueError(f"도로 정보 누락: {a} → {b}") from None
        if p is None:
            raise ValueError(f"도로 경로 조회 불가: {a} → {b}")
        return p["km"], p["minutes"] * self.factor


def validate_matrix(scenario, matrix):
    known = {s["id"]: (s["lat"], s["lng"]) for s in matrix["schools"]}
    for s in scenario["schools"]:
        if known.get(s["id"]) != (s["lat"], s["lng"]):
            raise ValueError("학교 선택/좌표가 변경되었습니다. 도로 정보를 다시 준비하세요.")
        for dest in scenario["schools"]:
            p = matrix["pairs"].get(s["id"] + "|" + dest["id"])
            if p is None:
                raise ValueError(f"도로 정보 누락: {s['name']} → {dest['name']}")
            for key in ("km", "minutes"):
                x = p[key]
                if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
                    raise ValueError("도로 거리, 시간은 유한한 0 이상의 숫자여야 합니다.")


def make_requests(scenario):
    c = scenario["config"]
    size = min(c["batch_size"], min(v["capacity"] for v in c["vehicles"]))
    requests = []
    for group in scenario["cohorts"]:
        origin = group["home"]
        ready = c["day_start"]
        legs = []
        for index, cl in enumerate(group["classes"]):
            legs.append((origin, cl["school_id"], ready,
                         cl["start"] - c["arrival_buffer"],
                         max(ready, cl["start"] - c["early_arrival"]), index > 0))
            origin, ready = cl["school_id"], cl["end"]
        if c["return_home"]:
            legs.append((origin, group["home"], ready, c["day_end"], ready, True))
        for leg, (origin, dest, ready, deadline, earliest_drop, wait_limit) in enumerate(legs):
            if origin == dest:
                continue
            for start in range(0, group["students"], size):
                count = min(size, group["students"] - start)
                requests.append({"id": f"{group['id']}:{leg}:{start}", "group": group["id"],
                                 "leg": leg, "student_start": start + 1, "count": count,
                                 "origin": origin, "destination": dest, "ready": ready,
                                 "pickup_latest": ready + c["max_wait"] if wait_limit else deadline,
                                 "deadline": deadline, "earliest_drop": earliest_drop,
                                 "class_start": deadline + c["arrival_buffer"] if leg < len(group["classes"]) else None,
                                 "wait_limit": wait_limit})
    return requests


def event(req, pickup):
    return {"request": req["id"], "school": req["origin"] if pickup else req["destination"],
            "pickup": pickup, "count": req["count"]}


def services(events, c):
    return [(c["stop_minutes"] if i == 0 or events[i - 1]["school"] != e["school"] else 0)
            + e["count"] * c["boarding_seconds"] / 60 for i, e in enumerate(events)]


def rate(v, c):
    return v["manufacturing"] / v["lifetime_km"] + v["kwh_per_km"] / c["charging_efficiency"] * c["electricity_factor"]


def evaluate(events, reqs, net, c, depot, objective):
    """Propagate lower-bound difference constraints, including max ride back-edges."""
    n = len(events)
    service = services(events, c)
    occupancy = peak = 0
    pickups = {}
    links = []
    times, upper = [], []
    km = 0.0
    ride_edges = []
    for i, e in enumerate(events):
        r = reqs[e["request"]]
        if e["pickup"]:
            if e["request"] in pickups:
                return None
            pickups[e["request"]] = i
            occupancy += e["count"]
            times.append(r["ready"])
            upper.append(r["pickup_latest"])
        else:
            if e["request"] not in pickups:
                return None
            p = pickups[e["request"]]
            occupancy -= e["count"]
            times.append(r["earliest_drop"])
            upper.append(r["deadline"] - service[i])
            ride_edges.append((i, p, -c["max_ride"] - service[p]))
        peak = max(peak, occupancy)
        if occupancy < 0:
            return None
        if i:
            distance, minutes = net.edge(events[i - 1]["school"], e["school"])
            km += distance
            links.append((i - 1, i, service[i - 1] + minutes))
    if occupancy or peak > max(v["capacity"] for v in c["vehicles"]):
        return None
    first_km, first_time = net.edge(depot, events[0]["school"])
    last_km, last_time = net.edge(events[-1]["school"], depot)
    times[0] = max(times[0], c["day_start"] + first_time)
    upper[-1] = min(upper[-1], c["day_end"] - service[-1] - last_time)
    links += ride_edges
    for _ in range(n + 1):
        changed = False
        for a, b, offset in links:
            if times[b] < times[a] + offset - 1e-8:
                times[b] = times[a] + offset
                changed = True
        if any(t > limit + 1e-7 for t, limit in zip(times, upper)):
            return None
        if not changed:
            break
    else:
        return None
    # Keep arrival times, but pick students up as late as the route permits.
    # Earliest feasible propagation alone can leave them waiting on the vehicle.
    for i in range(n - 2, -1, -1):
        if events[i]["pickup"]:
            _, travel = net.edge(events[i]["school"], events[i + 1]["school"])
            times[i] = min(upper[i], times[i + 1] - service[i] - travel)
    total_km = first_km + km + last_km
    types = [v for v in c["vehicles"] if v["capacity"] >= peak
             and total_km * v["kwh_per_km"] <= v["usable_kwh"] + 1e-7]
    if not types:
        return None
    passenger_minutes = 0.0
    for i, e in enumerate(events):
        if not e["pickup"]:
            p = pickups[e["request"]]
            passenger_minutes += (times[i] - times[p] - service[p]) * e["count"]
    def score(v):
        if objective == "carbon":
            return total_km * rate(v, c) + c["carbon_time_weight"] * passenger_minutes
        if objective == "cost":
            # Fixed cost here is a per-run proxy; final cost uses physical vehicles.
            return (v["daily_cost"] / c["cost_runs_per_day"] + total_km * (v["cost_per_km"] + v["kwh_per_km"] / c["charging_efficiency"] * c["electricity_price"])) / 1000
        return passenger_minutes + total_km * c["time_distance_weight"]
    v = min(types, key=lambda v: (score(v), v["capacity"]))
    return {"events": events, "times": times, "service": service, "type": v,
            "peak": peak, "km": km, "passenger_minutes": passenger_minutes,
            "score": score(v), "start": times[0], "end": times[-1] + service[-1]}


def baseline(requests, net, c):
    by_mode = []
    for mode in c["baseline"]:
        manufacturing = operation = passenger_minutes = passenger_km = 0.0
        for r in requests:
            km, _ = net.edge(r["origin"], r["destination"])
            km *= mode["distance_factor"]
            people = r["count"] * mode["share"]
            vehicle_km = km * people / mode["occupancy"] * (1 + mode["empty_ratio"])
            manufacturing += vehicle_km * mode["manufacturing"] / mode["lifetime_km"]
            operation += vehicle_km * mode["operation_per_km"]
            passenger_km += people * km
            passenger_minutes += people * (km / mode["speed_kmh"] * 60 + mode["wait_minutes"])
        by_mode.append({"name": mode["name"], "manufacturing": manufacturing,
                        "operation": operation, "total": manufacturing + operation,
                        "passenger_minutes": passenger_minutes, "passenger_km": passenger_km})
    return {"modes": by_mode, **{k: sum(m[k] for m in by_mode)
            for k in ("manufacturing", "operation", "total", "passenger_minutes", "passenger_km")}}


def assemble_fleet(routes, net, c, depot):
    fleet = []
    for route in sorted(routes, key=lambda r: (r["start"], r["end"])):
        v = route["type"]
        origin = route["events"][0]["school"]
        dest = route["events"][-1]["school"]
        home_km, home_time = net.edge(dest, depot)
        possible = []
        for vehicle in fleet:
            if vehicle["type"]["id"] != v["id"]:
                continue
            reposition_km, reposition_time = net.edge(vehicle["location"], origin)
            if (vehicle["available"] + reposition_time <= route["start"] + 1e-7
                    and (vehicle["km"] + reposition_km + route["km"] + home_km) * v["kwh_per_km"] <= v["usable_kwh"] + 1e-7):
                possible.append((reposition_km, vehicle["id"], vehicle))
        if possible:
            vehicle = min(possible, key=lambda x: x[:2])[2]
        else:
            if len(fleet) >= c["max_vehicles"]:
                raise ValueError("차량 상한 안에서 실행 가능한 계획을 찾지 못했습니다. 상한 또는 시간 조건을 조정하세요.")
            vehicle = {"id": f"V{len(fleet) + 1:03}", "type": v, "runs": [],
                       "available": c["day_start"], "location": depot, "km": 0.0, "legs": []}
            fleet.append(vehicle)
        previous_location = vehicle["location"]
        km, minutes = net.edge(previous_location, origin)
        vehicle["legs"].append({"from": previous_location, "to": origin, "km": km,
                                "start": route["start"] - minutes, "end": route["start"], "load": 0})
        onboard = 0
        for i, e in enumerate(route["events"]):
            onboard += e["count"] if e["pickup"] else -e["count"]
            if i < len(route["events"]) - 1:
                next_school = route["events"][i + 1]["school"]
                d, t = net.edge(e["school"], next_school)
                vehicle["legs"].append({"from": e["school"], "to": next_school,
                                        "km": d, "start": route["times"][i] + route["service"][i],
                                        "end": route["times"][i] + route["service"][i] + t, "load": onboard})
        route["vehicle_id"] = vehicle["id"]
        vehicle["runs"].append(route)
        vehicle["km"] += km + route["km"]
        vehicle["available"], vehicle["location"] = route["end"], dest
    for vehicle in fleet:
        km, minutes = net.edge(vehicle["location"], depot)
        vehicle["legs"].append({"from": vehicle["location"], "to": depot, "km": km,
                                "start": vehicle["available"], "end": vehicle["available"] + minutes, "load": 0})
        vehicle["km"] += km
        v = vehicle["type"]
        vehicle["empty_km"] = sum(leg["km"] for leg in vehicle["legs"] if leg["load"] == 0)
        vehicle["passenger_km"] = sum(leg["km"] * leg["load"] for leg in vehicle["legs"])
        vehicle["manufacturing"] = vehicle["km"] * v["manufacturing"] / v["lifetime_km"]
        vehicle["battery_kwh"] = vehicle["km"] * v["kwh_per_km"]
        vehicle["grid_kwh"] = vehicle["battery_kwh"] / c["charging_efficiency"]
        vehicle["operation"] = vehicle["grid_kwh"] * c["electricity_factor"]
        vehicle["total"] = vehicle["manufacturing"] + vehicle["operation"]
        vehicle["cost"] = v["daily_cost"] + vehicle["km"] * v["cost_per_km"] + vehicle["grid_kwh"] * c["electricity_price"]
    return fleet


def verify_plan(fleet, requests, scenario, net, depot):
    """Check returned artifacts directly; do not invoke the optimization evaluator."""
    c = scenario["config"]
    expected = {r["id"]: r for r in requests}
    found = collections.Counter()
    violations = []
    rides = []
    if len(fleet) > c["max_vehicles"]:
        violations.append("차량 대수 상한 초과")
    for vehicle in fleet:
        v = vehicle["type"]
        last_time, last_school = c["day_start"], depot
        total_distance = 0.0
        for run in vehicle["runs"]:
            aboard = {}
            load = 0
            dwell = services(run["events"], c)
            for i, e in enumerate(run["events"]):
                t = run["times"][i]
                distance, duration = net.edge(last_school, e["school"])
                total_distance += distance
                if t + 1e-6 < last_time + duration:
                    violations.append(f"{vehicle['id']}: 차량 시간 충돌")
                last_time, last_school = t + dwell[i], e["school"]
                r = expected.get(e["request"])
                if r is None or e["count"] != r["count"]:
                    violations.append("수요 ID/인원 불일치")
                    continue
                if e["pickup"]:
                    if e["school"] != r["origin"] or not r["ready"] - 1e-6 <= t <= r["pickup_latest"] + 1e-6:
                        violations.append(f"{r['id']}: 승차 장소/시간 위반")
                    if r["id"] in aboard:
                        violations.append("중복 승차")
                    aboard[r["id"]] = (t, t + dwell[i])
                    found[(r["id"], "pickup")] += 1
                    load += e["count"]
                else:
                    if e["school"] != r["destination"] or t < r["earliest_drop"] - 1e-6 or t + dwell[i] > r["deadline"] + 1e-6:
                        violations.append(f"{r['id']}: 하차 장소/시간 위반")
                    picked = aboard.pop(r["id"], None)
                    if picked is None:
                        violations.append("동일 차량 승차 없는 하차/환승")
                    else:
                        ride = t - picked[1]
                        if not -1e-6 <= ride <= c["max_ride"] + 1e-6:
                            violations.append(f"{r['id']}: 최대 탑승시간 위반")
                        rides.append({**r, "vehicle_id": vehicle["id"], "pickup": picked[0],
                                      "dropoff": t, "arrival_complete": t + dwell[i], "ride_minutes": ride,
                                      "wait_minutes": picked[0] - r["ready"] if r["wait_limit"] else 0})
                    found[(r["id"], "dropoff")] += 1
                    load -= e["count"]
                if load < 0 or load > v["capacity"]:
                    violations.append(f"{vehicle['id']}: 정원 위반")
            if aboard or load:
                violations.append("운행 종료 후 미하차 학생")
        km, minutes = net.edge(last_school, depot)
        total_distance += km
        if last_time + minutes > c["day_end"] + 1e-6:
            violations.append("차량 거점 복귀 마감 위반")
        if abs(total_distance - vehicle["km"]) > 1e-5:
            violations.append("공차 포함 총거리 불일치")
        if total_distance * v["kwh_per_km"] > v["usable_kwh"] + 1e-6:
            violations.append("일일 배터리 초과")
    for request_id in expected:
        if found[(request_id, "pickup")] != 1 or found[(request_id, "dropoff")] != 1:
            violations.append(f"{request_id}: 수요 누락/중복")
    return {"valid": not violations, "violations": violations[:30], "rides": rides,
            "checks": ["승하차 인원 보존", "정시 도착", "정원", "환승 금지", "하루 차량 연결", "공차 복귀", "탑승, 대기시간", "배터리"]}


def solve(scenario, matrix, objective="carbon", progress=lambda *_: None):
    validate_scenario(scenario)
    validate_matrix(scenario, matrix)
    c = scenario["config"]
    net = Network(matrix, c)
    depot = c["depot_id"] or min(scenario["schools"], key=lambda a: sum(net.edge(a["id"], b["id"])[0] for b in scenario["schools"]))["id"]
    requests = make_requests(scenario)
    if not requests:
        raise ValueError("학교 사이 이동 요청이 없습니다.")
    lookup = {r["id"]: r for r in requests}
    started = time.perf_counter()
    routes = []
    budget_hit = False
    for index, request in enumerate(sorted(requests, key=lambda r: (r["deadline"], r["pickup_latest"], r["origin"], r["id"]))):
        pair = [event(request, True), event(request, False)]
        direct = evaluate(pair, lookup, net, c, depot, objective)
        if direct is None:
            raise ValueError(f"{request['group']}의 {request['leg'] + 1}번째 이동에 가능한 배정을 찾지 못했습니다. 수업 간격, 최대 탑승/대기시간, 거점, 배터리를 확인하세요.")
        best, best_index, increase = direct, None, direct["score"]
        if time.perf_counter() - started < c["max_seconds"]:
            candidates = [(i, route) for i, route in enumerate(routes)
                          if len(route["events"]) + 2 <= c["max_events"]
                          and route["start"] <= request["deadline"]
                          and route["end"] + c["max_ride"] >= request["ready"]]
            candidates.sort(key=lambda ir: abs(ir[1]["start"] - direct["start"]) + net.edge(ir[1]["events"][0]["school"], request["origin"])[1])
            for i, route in candidates[:5]:
                old = route["events"]
                for p in range(len(old) + 1):
                    for d in range(p + 1, len(old) + 2):
                        events = old[:p] + [pair[0]] + old[p:]
                        events = events[:d] + [pair[1]] + events[d:]
                        attempt = evaluate(events, lookup, net, c, depot, objective)
                        if attempt is not None and attempt["score"] - route["score"] < increase - 1e-7:
                            best, best_index, increase = attempt, i, attempt["score"] - route["score"]
        else:
            budget_hit = True
        if best_index is None:
            routes.append(best)
        else:
            routes[best_index] = best
        if index % 20 == 0:
            progress(index / len(requests), f"{index + 1}/{len(requests)} 이동 묶음 배정")
    fleet = assemble_fleet(routes, net, c, depot)
    validation = verify_plan(fleet, requests, scenario, net, depot)
    if not validation["valid"]:
        raise ValueError("결과 검증 실패: " + "; ".join(validation["violations"]))
    base = baseline(requests, net, c)
    totals = {key: sum(v[key] for v in fleet) for key in
              ("km", "empty_km", "passenger_km", "manufacturing", "operation", "total", "cost", "grid_kwh")}
    rides = validation.pop("rides")
    passenger_trips = sum(r["count"] for r in requests)
    # Door-to-class interval: boarding, ride, post-class waiting, and early arrival.
    journey_minutes = 0.0
    for r in rides:
        arrival_wait = max(0, r["class_start"] - r["arrival_complete"]) if r["class_start"] is not None else 0
        journey_minutes += r["count"] * (r["arrival_complete"] - r["pickup"] + r["wait_minutes"] + arrival_wait)
    totals.update({"vehicles": len(fleet), "runs": len(routes), "passenger_trips": passenger_trips,
                   "students": sum(g["students"] for g in scenario["cohorts"]),
                   "ride_minutes": sum(r["ride_minutes"] * r["count"] for r in rides) / passenger_trips,
                   "journey_minutes": journey_minutes / passenger_trips,
                   "baseline_minutes": base["passenger_minutes"] / passenger_trips,
                   "max_ride_minutes": max(r["ride_minutes"] for r in rides),
                   "reduction": base["total"] - totals["total"],
                   "reduction_pct": (base["total"] - totals["total"]) / base["total"] * 100 if base["total"] else None})
    return {"objective": objective, "status": "feasible", "depot": depot,
            "matrix_source": matrix["source"], "matrix_label": matrix["label"],
            "seconds": time.perf_counter() - started, "budget_hit": budget_hit,
            "fleet": fleet, "rides": rides, "totals": totals, "baseline": base,
            "validation": validation, "fleet_mix": dict(collections.Counter(v["type"]["name"] for v in fleet))}


def compare(scenario, matrix, progress=lambda *_: None):
    results = []
    for i, objective in enumerate(("carbon", "cost", "time")):
        try:
            result = solve(scenario, matrix, objective,
                           lambda p, message: progress((i + p) / 3, f"{objective}: {message}"))
            results.append(result)
        except ValueError as exc:
            results.append({"objective": objective, "status": "not_found", "error": str(exc)})
    good = [r for r in results if r["status"] == "feasible"]
    for r in good:
        r["dominated"] = any(all(o["totals"][k] <= r["totals"][k] + 1e-7 for k in ("total", "cost", "journey_minutes"))
                             and any(o["totals"][k] < r["totals"][k] - 1e-7 for k in ("total", "cost", "journey_minutes"))
                             for o in good if o is not r)
    return {"plans": results, "scenario": scenario,
            "method": "시간 제약 삽입 휴리스틱, 목적별 근사 대안, 전역 최적성 미보장"}
