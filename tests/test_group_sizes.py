import random
import unittest

from shuttle.data import defaults, group_sizes, make_scenario, validate_config


class GroupSizeTests(unittest.TestCase):
    def test_random_groups_preserve_total_and_allow_single_students(self):
        c = defaults()
        scenario = make_scenario(c)
        sizes = [g['students'] for g in scenario['cohorts']]
        self.assertEqual(sum(sizes), 1000)
        self.assertTrue(all(1 <= n <= 40 for n in sizes))
        self.assertIn(1, sizes)
        self.assertGreater(len(set(sizes)), 1)
        self.assertEqual(scenario, make_scenario(c))

    def test_bounds_and_exact_partition(self):
        for seed in range(20):
            sizes = group_sizes(103, 7, 20, random.Random(seed))
            self.assertEqual(sum(sizes), 103)
            self.assertTrue(all(7 <= n <= 20 for n in sizes))
        self.assertEqual(group_sizes(5, 1, 1, random.Random(1)), [1] * 5)
        with self.assertRaises(ValueError):
            group_sizes(5, 3, 3, random.Random(1))

    def test_reversed_bounds_and_legacy_config(self):
        c = defaults()
        c.update(cohort_min=20, cohort_max=1)
        with self.assertRaises(ValueError):
            validate_config(c)
        c = defaults()
        del c['cohort_min'], c['cohort_max']
        sizes = [g['students'] for g in make_scenario(c)['cohorts']]
        self.assertTrue(all(n == 24 for n in sizes[:-1]))
        self.assertEqual(sum(sizes), 1000)
