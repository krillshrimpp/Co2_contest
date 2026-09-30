import copy
import json
import unittest
from unittest.mock import patch

from shuttle.data import defaults, load_schools, make_scenario, estimated_matrix, validate_scenario
from shuttle.kakao import parse_route, build_matrix
from shuttle.planner import Network, baseline, compare, make_requests, solve, verify_plan


def small_case():
    c = defaults()
    c.update(student_count=13, school_count=3, batch_size=4, depot_id="A", max_seconds=10)
    schools = [{"id": x, "name": x, "lat": 35.1, "lng": 129 + i * .01, "address": "test"}
               for i, x in enumerate("ABC")]
    s = {"version": 1, "config": c, "schools": schools,
         "cohorts": [{"id": "G1", "home": "A", "students": 13,
                      "classes": [{"school_id": "B", "start": 570, "end": 620},
                                  {"school_id": "C", "start": 720, "end": 770}]}]}
    pairs = {a + "|" + b: {"km": 0 if a == b else 10, "minutes": 0 if a == b else 20, "path": []}
             for a in "ABC" for b in "ABC"}
    return s, {"source": "test", "label": "test", "schools": schools, "pairs": pairs}


class PlanningTests(unittest.TestCase):
    def test_source_csv(self):
        schools = load_schools()
        self.assertEqual(len(schools), 142)
        self.assertEqual(len({s["id"] for s in schools}), 142)
        self.assertTrue(all(34 < s["lat"] < 36 and 128 < s["lng"] < 130 for s in schools))

    def test_generation_is_repeatable_and_preserves_population(self):
        c = defaults()
        a, b = make_scenario(c), make_scenario(c)
        self.assertEqual(a, b)
        self.assertEqual(len(a["schools"]), 22)
        self.assertEqual(sum(g["students"] for g in a["cohorts"]), 1000)

    def test_chain_split_and_return(self):
        s, _ = small_case()
        r = make_requests(s)
        for leg, pair in enumerate((("A", "B"), ("B", "C"), ("C", "A"))):
            subset = [x for x in r if x["leg"] == leg]
            self.assertEqual(sum(x["count"] for x in subset), 13)
            self.assertEqual({(x["origin"], x["destination"]) for x in subset}, {pair})
            self.assertEqual([x["count"] for x in subset], [4, 4, 4, 1])
        self.assertEqual(r[4]["ready"], 620)

    def test_return_can_be_disabled(self):
        s, m = small_case()
        s["config"]["return_home"] = False
        self.assertEqual(sum(r["count"] for r in make_requests(s)), 26)
        p = solve(s, m)
        self.assertTrue(all(v["legs"][-1]["to"] == "A" for v in p["fleet"]))

    def test_alighting_finishes_before_class_even_without_buffer(self):
        s, m = small_case()
        s["config"].update(arrival_buffer=0, early_arrival=5, boarding_seconds=30)
        p = solve(s, m)
        self.assertTrue(all(r["arrival_complete"] <= r["deadline"] + 1e-7 for r in p["rides"]))
        requests = {r["id"]: r for r in make_requests(s)}
        first_run = p["fleet"][0]["runs"][0]
        for i, e in enumerate(first_run["events"]):
            if not e["pickup"]:
                first_run["times"][i] = requests[e["request"]]["deadline"]
                break
        self.assertFalse(verify_plan(p["fleet"], list(requests.values()), s, Network(m, s["config"]), p["depot"])["valid"])

    def test_all_three_objectives_are_feasible(self):
        s, m = small_case()
        result = compare(s, m)
        for p in result["plans"]:
            self.assertEqual(p["status"], "feasible", p)
            self.assertTrue(p["validation"]["valid"])
            self.assertEqual(p["totals"]["passenger_trips"], 39)
            self.assertEqual(p["totals"]["students"], 13)

    def test_forward_asymmetric_matrix_is_respected(self):
        s, m = small_case()
        m["pairs"]["B|A"].update(km=35, minutes=45)
        n = Network(m, s["config"])
        self.assertEqual(n.edge("A", "B"), (10, 20))
        self.assertEqual(n.edge("B", "A"), (35, 45))
        self.assertTrue(solve(s, m)["validation"]["valid"])

    def test_tampered_capacity_is_rejected(self):
        s, m = small_case()
        p = solve(s, m)
        fleet = copy.deepcopy(p["fleet"])
        for v in fleet:
            v["type"]["capacity"] = 1
        check = verify_plan(fleet, make_requests(s), s, Network(m, s["config"]), p["depot"])
        self.assertFalse(check["valid"])
        self.assertTrue(any("정원" in x for x in check["violations"]))

    def test_missing_vehicle_does_not_silently_drop_students(self):
        s, m = small_case()
        p = solve(s, m)
        check = verify_plan(p["fleet"][1:], make_requests(s), s, Network(m, s["config"]), p["depot"])
        self.assertFalse(check["valid"])

    def test_tampered_time_is_rejected(self):
        s, m = small_case()
        p = solve(s, m)
        p["fleet"][0]["runs"][0]["times"][0] = 0
        self.assertFalse(verify_plan(p["fleet"], make_requests(s), s, Network(m, s["config"]), p["depot"])["valid"])

    def test_impossible_trip_is_reported(self):
        s, m = small_case()
        s["config"]["max_ride"] = 5
        with self.assertRaisesRegex(ValueError, "배정"):
            solve(s, m)

    def test_insufficient_battery_is_reported(self):
        s, m = small_case()
        for v in s["config"]["vehicles"]:
            v["usable_kwh"] = .01
        with self.assertRaisesRegex(ValueError, "배정"):
            solve(s, m)

    def test_emission_formula_includes_empty_distance(self):
        s, m = small_case()
        p = solve(s, m)
        for v in p["fleet"]:
            spec = v["type"]
            all_km = sum(leg["km"] for leg in v["legs"])
            self.assertAlmostEqual(v["km"], all_km)
            self.assertAlmostEqual(v["manufacturing"], all_km * spec["manufacturing"] / spec["lifetime_km"])
            self.assertAlmostEqual(v["operation"], all_km * spec["kwh_per_km"] / .9 * .45)
        self.assertGreater(p["totals"]["empty_km"], 0)

    def test_baseline_uses_occupancy_and_no_double_count(self):
        s, m = small_case()
        c = s["config"]
        c["baseline"] = [{"name": "test", "share": 1, "manufacturing": 12000,
                          "lifetime_km": 200000, "operation_per_km": .2, "occupancy": 2,
                          "distance_factor": 1, "empty_ratio": .5, "speed_kmh": 30, "wait_minutes": 0}]
        r = [{"origin": "A", "destination": "B", "count": 4}]
        b = baseline(r, Network(m, c), c)
        self.assertAlmostEqual(b["manufacturing"], 1.8)
        self.assertAlmostEqual(b["operation"], 6)

    def test_zero_baseline_and_negative_savings(self):
        s, m = small_case()
        for mode in s["config"]["baseline"]:
            mode["manufacturing"] = mode["operation_per_km"] = 0
        p = solve(s, m)
        self.assertLess(p["totals"]["reduction"], 0)
        self.assertIsNone(p["totals"]["reduction_pct"])
        json.dumps(p, allow_nan=False)

    def test_invalid_inputs_are_rejected(self):
        for mutate in (
            lambda s: s["config"].update(charging_efficiency=0),
            lambda s: s["config"].update(early_arrival=1),
            lambda s: s["cohorts"][0].update(students=1.5),
            lambda s: s["cohorts"][0]["classes"][1].update(start=600),
            lambda s: s["config"]["baseline"][0].update(share=.9),
        ):
            s, _ = small_case()
            mutate(s)
            with self.assertRaises(ValueError):
                validate_scenario(s)

    def test_changed_coordinates_require_new_matrix(self):
        s, m = small_case()
        m = copy.deepcopy(m)
        s["schools"][0]["lat"] += .01
        with self.assertRaisesRegex(ValueError, "좌표"):
            solve(s, m)

    def test_missing_matrix_not_replaced_by_straight_line(self):
        s, m = small_case()
        del m["pairs"]["A|B"]
        with self.assertRaisesRegex(ValueError, "누락"):
            solve(s, m)

    def test_kakao_units_and_polyline(self):
        response = {"routes": [{"result_code": 0, "summary": {"distance": 1500, "duration": 120},
                                "sections": [{"roads": [{"vertexes": [129, 35, 129.1, 35.1]}]}]}]}
        parsed = parse_route(response)
        self.assertEqual(parsed, {"km": 1.5, "minutes": 2, "path": [[35, 129], [35.1, 129.1]]})
        response["routes"][0]["result_code"] = 104
        with self.assertRaises(ValueError):
            parse_route(response)

    @patch("shuttle.kakao.directions")
    def test_kakao_matrix_direction_and_failure(self, mock):
        s, _ = small_case()
        mock.return_value = {"km": 3, "minutes": 7, "path": []}
        m = build_matrix(s["schools"], "test-key")
        self.assertEqual(mock.call_count, 6)
        self.assertEqual(m["pairs"]["A|A"]["km"], 0)
        self.assertEqual(len(m["pairs"]), 9)
        mock.side_effect = ValueError("no route")
        with self.assertRaisesRegex(ValueError, "no route"):
            build_matrix(s["schools"], "test-key")

    def test_full_demo_population(self):
        c = defaults()
        s = make_scenario(c)
        p = solve(s, estimated_matrix(s["schools"], c))
        self.assertTrue(p["validation"]["valid"])
        self.assertEqual(p["totals"]["students"], 1000)
        self.assertEqual({r["group"] for r in p["rides"]}, {g["id"] for g in s["cohorts"]})


if __name__ == "__main__":
    unittest.main()
