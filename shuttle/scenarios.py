"""Bounded scenario sensitivity tests, not mathematical best/worst guarantees."""
import copy
import random
import math

from .data import validate_scenario, group_sizes
from .planner import compare

PROFILES = {'favorable': '유리', 'mixed': '중간', 'adverse': '불리'}


def generate_case(base, matrix, profile, seed, vary_modes=False, vary_students=False):
    if profile not in PROFILES or isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError('시나리오 유형 또는 난수 값이 올바르지 않습니다.')
    scenario = copy.deepcopy(base)
    c, schools = scenario['config'], scenario['schools']
    c['seed'] = seed
    c['student_count'] = sum(g['students'] for g in base['cohorts'])
    c['school_count'] = len(schools)
    if vary_students:
        c['student_count'] = max(1, round(c['student_count'] * {'favorable': 1, 'mixed': .5, 'adverse': .1}[profile]))
    rng = random.Random(seed)
    hubs = rng.sample(schools, min(3, len(schools)))
    depot = c['depot_id'] or min(schools, key=lambda s: sum(matrix['pairs'][s['id']+'|'+d['id']]['km'] for d in schools))['id']
    def travel(a, b):
        return matrix['pairs'][a+'|'+b]['minutes'] * c['traffic_factor']
    groups = []
    sizes = group_sizes(c['student_count'], c.get('cohort_min', 1), c.get('cohort_max', c['cohort_size']), rng, profile)
    for count in sizes:
        home = schools[len(groups) % len(schools)]
        candidates = [s for s in schools if s['id'] != home['id']]
        candidates.sort(key=lambda s: matrix['pairs'][home['id'] + '|' + s['id']]['km'])
        reachable = [s for s in candidates if matrix['pairs'][home['id'] + '|' + s['id']]['minutes']
                     * c['traffic_factor'] <= c['max_ride'] - c['stop_minutes']]
        # Prefer directly reachable pairs; full fleet feasibility is still tested, not assumed.
        candidates = reachable or candidates
        if profile == 'favorable':
            near_hubs = [s for s in candidates if s in hubs]
            destination = (near_hubs or candidates)[0]
            start = c['class_starts'][len(c['class_starts']) // 2]
        elif profile == 'adverse':
            destination = rng.choice(candidates[-max(1, len(candidates) // 3):])
            start = rng.choice(c['class_starts']) + rng.randrange(-35, 36, 5)
        else:
            destination = rng.choice(candidates)
            start = rng.choice(c['class_starts']) + rng.randrange(-10, 11, 5)
        earliest = c['day_start'] + travel(depot, home['id']) + travel(home['id'], destination['id']) + c['arrival_buffer'] + 5
        latest = c['day_end'] - c['class_minutes'] - travel(destination['id'], depot) - 5
        if c['return_home']:
            latest = c['day_end'] - c['class_minutes'] - travel(destination['id'], home['id']) - travel(home['id'], depot) - 5
        if earliest <= latest:
            start = max(math.ceil(earliest / 5) * 5, min(start, math.floor(latest / 5) * 5))
        start = max(c['day_start'], min(start, c['day_end'] - c['class_minutes']))
        groups.append({'id': f'G{len(groups)+1:03}', 'home': home['id'], 'students': count,
                       'classes': [{'school_id': destination['id'], 'start': start, 'end': start + c['class_minutes']}]})
    scenario['cohorts'] = groups
    if vary_modes:
        shares = {'favorable': [.7, .2, .1], 'mixed': [.3, .5, .2], 'adverse': [.05, .25, .7]}[profile]
        if {m['id'] for m in c['baseline']} != {'car', 'bus', 'rail'}:
            raise ValueError('이동수단 비율 변경은 car, bus, rail 세 수단 구성에서 지원합니다.')
        for m in c['baseline']:
            m['share'] = dict(zip(('car', 'bus', 'rail'), shares))[m['id']]
    scenario['stress_case'] = {'profile': profile, 'seed': seed, 'vary_modes': vary_modes,
                             'classes_per_student': 1, 'vary_students': vary_students}
    validate_scenario(scenario)
    return scenario


def test_cases(base, matrix, repeats, seed, vary_modes, progress=lambda *_: None, vary_students=False):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 3:
        raise ValueError('유형별 시험 횟수는 1~3회입니다.')
    cases = []
    for profile in PROFILES:
        for repeat in range(repeats):
            case_seed = (seed + repeat) % 2**32
            scenario = generate_case(base, matrix, profile, case_seed, vary_modes, vary_students)
            # Equal search budgets keep the batch bounded; saved inputs record the budget.
            scenario['config']['max_seconds'] = min(base['config']['max_seconds'], 1)
            label = f'{PROFILES[profile]} seed {case_seed}'
            offset = len(cases)
            result = compare(scenario, matrix, lambda p, message: progress(
                (offset + p) / (len(PROFILES) * repeats), label + ': ' + message))
            feasible = [(i, p) for i, p in enumerate(result['plans']) if p['status'] == 'feasible']
            chosen = min(feasible, key=lambda item: item[1]['totals']['total']) if feasible else None
            cases.append({'profile': profile, 'label': label, 'seed': case_seed, 'results': result,
                          'plan_index': chosen[0] if chosen else None,
                          'totals': chosen[1]['totals'] if chosen else None,
                          'baseline': chosen[1]['baseline']['total'] if chosen else None,
                          'error': None if chosen else '; '.join(p.get('error', '') for p in result['plans'])})
    feasible_indices = [i for i, case in enumerate(cases) if case['totals'] is not None]
    return {'cases': cases, 'matrix': matrix, 'vary_modes': vary_modes,
            'vary_students': vary_students,
            'best': max(feasible_indices, key=lambda i: cases[i]['totals']['reduction'] / cases[i]['totals']['students']) if feasible_indices else None,
            'worst': min(feasible_indices, key=lambda i: cases[i]['totals']['reduction'] / cases[i]['totals']['students']) if feasible_indices else None}
