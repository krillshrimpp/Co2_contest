import copy
import unittest

from shuttle.data import defaults, make_scenario, estimated_matrix
from shuttle.planner import compare
from shuttle.report import render_report


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = defaults()
        config.update(school_count=4, student_count=20)
        scenario = make_scenario(config)
        cls.matrix = estimated_matrix(scenario['schools'], config)
        cls.results = compare(scenario, cls.matrix)

    def test_report_contains_all_groups_schools_vehicles_and_routes(self):
        report = render_report(self.results, self.matrix, 0)
        for school in self.results['scenario']['schools']:
            self.assertIn(school['name'], report)
        for group in self.results['scenario']['cohorts']:
            self.assertIn(group['id'], report)
        for vehicle in self.results['plans'][0]['fleet']:
            self.assertIn(vehicle['id'], report)
        self.assertIn('학생 배차 목록', report)
        self.assertIn('<polyline', report)
        self.assertIn('@media print', report)
        self.assertIn('공차', report)

    def test_user_supplied_names_are_escaped(self):
        results = copy.deepcopy(self.results)
        results['scenario']['schools'][0]['name'] = '<img src=x onerror=alert(1)>'
        report = render_report(results, self.matrix, 0)
        self.assertNotIn('<img src=x', report)
        self.assertIn('&lt;img', report)

    def test_unavailable_plan_is_rejected(self):
        results = copy.deepcopy(self.results)
        results['plans'][0] = {'status': 'not_found'}
        with self.assertRaises(ValueError):
            render_report(results, self.matrix, 0)
