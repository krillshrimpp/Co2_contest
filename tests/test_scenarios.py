import copy
import unittest

from shuttle.data import defaults, make_scenario, estimated_matrix
from shuttle.scenarios import generate_case, test_cases as run_cases


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        c = defaults()
        c.update(school_count=6, student_count=60, max_seconds=.1)
        self.base = make_scenario(c)
        self.matrix = estimated_matrix(self.base['schools'], c)

    def test_reproducible_count_and_fixed_assumptions(self):
        before = copy.deepcopy(self.base)
        for profile in ('favorable', 'mixed', 'adverse'):
            s = generate_case(self.base, self.matrix, profile, 42)
            self.assertEqual(s, generate_case(self.base, self.matrix, profile, 42))
            self.assertEqual(sum(g['students'] for g in s['cohorts']), 60)
            self.assertEqual(s['schools'], self.base['schools'])
            self.assertEqual(s['config']['baseline'], self.base['config']['baseline'])
            self.assertTrue(all(len(g['classes']) == 1 for g in s['cohorts']))
        self.assertEqual(self.base, before)

    def test_extremes_match_feasible_results(self):
        r = run_cases(self.base, self.matrix, 1, 42, True)
        feasible = [i for i, c in enumerate(r['cases']) if c['totals']]
        self.assertTrue(feasible)
        self.assertEqual(r['best'], max(feasible, key=lambda i: r['cases'][i]['totals']['reduction']))
        self.assertEqual(r['worst'], min(feasible, key=lambda i: r['cases'][i]['totals']['reduction']))
        for case in r['cases']:
            self.assertAlmostEqual(sum(m['share'] for m in case['results']['scenario']['config']['baseline']), 1)

    def test_infeasible_demand_is_not_filtered_out(self):
        base = copy.deepcopy(self.base)
        base['config']['max_ride'] = .01
        r = run_cases(base, self.matrix, 1, 42, False)
        self.assertEqual(len(r['cases']), 3)
        self.assertTrue(all(c['totals'] is None for c in r['cases']))
        self.assertIsNone(r['best'])
        self.assertIsNone(r['worst'])
