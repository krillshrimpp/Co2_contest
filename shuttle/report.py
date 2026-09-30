"""Standalone, printable report for one calculated alternative."""
import math
from collections import Counter
from html import escape


def text(value):
    return escape(str(value), quote=True)


def clock(value):
    minute = math.floor(value)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def table(headers, rows):
    return ('<div class="table"><table><thead><tr>'
            + ''.join(f'<th>{text(h)}</th>' for h in headers)
            + '</tr></thead><tbody>'
            + ''.join('<tr>' + ''.join(f'<td>{text(c)}</td>' for c in row) + '</tr>' for row in rows)
            + '</tbody></table></div>')


def route_map(vehicle, schools, matrix):
    by_id = {s['id']: s for s in schools}
    used = {leg[k] for leg in vehicle['legs'] for k in ('from', 'to')}
    paths = []
    for leg in vehicle['legs']:
        if not leg['km']:
            continue
        points = matrix['pairs'][leg['from'] + '|' + leg['to']].get('path')
        if not points:
            points = [[by_id[k]['lat'], by_id[k]['lng']] for k in (leg['from'], leg['to'])]
        paths.append((points, leg['load']))
    coords = [p for points, _ in paths for p in points] + [[by_id[k]['lat'], by_id[k]['lng']] for k in used]
    if not coords:
        return ''
    low_lat, high_lat = min(p[0] for p in coords), max(p[0] for p in coords)
    low_lng, high_lng = min(p[1] for p in coords), max(p[1] for p in coords)
    cosine = math.cos(math.radians((low_lat + high_lat) / 2))
    scale = min(650 / max((high_lng - low_lng) * cosine, .001), 250 / max(high_lat - low_lat, .001))

    def project(lat, lng):
        return (400 + (lng - (low_lng + high_lng) / 2) * cosine * scale,
                160 - (lat - (low_lat + high_lat) / 2) * scale)

    shapes = []
    for points, load in paths:
        line = ' '.join(f'{x:.1f},{y:.1f}' for x, y in (project(*p) for p in points))
        style = 'stroke="#345d83"' if load else 'stroke="#999" stroke-dasharray="5 4"'
        shapes.append(f'<polyline points="{line}" fill="none" stroke-width="2" {style}/>')
    legend = []
    for i, s in enumerate((s for s in schools if s['id'] in used), 1):
        x, y = project(s['lat'], s['lng'])
        shapes.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="white" stroke="#345d83"/>'
                      f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-size="11">{i}</text>')
        legend.append(f'{i}. {text(s["name"])}')
    source = '카카오 도로 형상' if matrix['source'] == 'kakao' else '추정 연결선'
    return ('<figure><svg viewBox="0 0 800 320" role="img" aria-label="차량 운행 경로">'
            + ''.join(shapes) + '</svg><figcaption>' + source + ' / 실선: 탑승 / 점선: 공차<br>'
            + ' &nbsp; '.join(legend) + '</figcaption></figure>')


def render_report(results, matrix, plan_index):
    scenario = results['scenario']
    plan = results['plans'][plan_index]
    if plan['status'] != 'feasible':
        raise ValueError('계산이 완료된 대안을 선택하세요.')
    schools, groups = scenario['schools'], scenario['cohorts']
    names = {s['id']: s['name'] for s in schools}
    objective = {'carbon': '저탄소 우선', 'cost': '운영비 우선', 'time': '탑승시간 우선'}[plan['objective']]
    total = plan['totals']
    home_counts = Counter()
    for g in groups:
        home_counts[g['home']] += g['students']
    sections = [f'<h1>학교 및 차량 운행 계획</h1><p>{objective}</p>',
                '<div class="summary">' + ''.join(f'<div><span>{label}</span><strong>{text(value)}</strong></div>' for label, value in [
                    ('학교', f'{len(schools)}개교'), ('학생', f'{total["students"]:,}명'),
                    ('차량', f'{total["vehicles"]}대'), ('운행거리', f'{total["km"]:,.1f}km'),
                    ('탄소배출량', f'{total["total"]:,.1f}kgCO₂e'), ('기존 대비 감축량', f'{total["reduction"]:,.1f}kgCO₂e')]) + '</div>',
                '<nav><a href="#schools">학교</a><a href="#students">학생 일정</a><a href="#rides">학생 배차</a><a href="#routes">차량 경로</a></nav>',
                '<h2 id="schools">학교 목록</h2>',
                table(['학교', '주소', '소속 이동 학생 수'], [(s['name'], s.get('address', ''), home_counts[s['id']]) for s in schools]),
                '<h2 id="students">학생 수업 일정</h2><p>학생 번호는 그룹 내 번호입니다.</p>',
                table(['그룹', '학생 번호', '인원', '소속 학교', '수업 순서', '수업 학교', '시작', '종료'],
                      [(g['id'], f'1~{g["students"]}', g['students'], names[g['home']], i + 1,
                        names[c['school_id']], clock(c['start']), clock(c['end']))
                       for g in groups for i, c in enumerate(g['classes'])]),
                '<h2 id="rides">학생 배차 목록</h2>',
                table(['그룹', '학생 번호', '이동 순서', '차량', '출발 학교', '도착 학교', '승차', '하차', '탑승시간'],
                      [(r['group'], f'{r["student_start"]}~{r["student_start"] + r["count"] - 1}', r['leg'] + 1,
                        r['vehicle_id'], names[r['origin']], names[r['destination']], clock(r['pickup']),
                        clock(r['dropoff']), f'{r["ride_minutes"]:.1f}분')
                       for r in sorted(plan['rides'], key=lambda r: (r['group'], r['student_start'], r['leg']))]),
                '<h2 id="routes">차량별 운행 경로</h2>']
    for v in plan['fleet']:
        sections.append(f'<section class="vehicle"><h3>{text(v["id"])} {text(v["type"]["name"])}</h3>'
                        f'<p>정원 {v["type"]["capacity"]}명 / {len(v["runs"])}회 운행 / {v["km"]:.1f}km</p>')
        sections.append(route_map(v, schools, matrix))
        sections.append(table(['출발', '도착', '출발 학교', '도착 학교', '거리', '탑승 인원'],
                              [(clock(l['start']), clock(l['end']), names[l['from']], names[l['to']],
                                f'{l["km"]:.1f}km', f'{l["load"]}명' if l['load'] else '공차')
                               for l in v['legs'] if l['from'] != l['to']]))
        rows = []
        for i, run in enumerate(v['runs'], 1):
            load = 0
            for e, time in zip(run['events'], run['times']):
                load += e['count'] if e['pickup'] else -e['count']
                rows.append((i, clock(time), names[e['school']], '승차' if e['pickup'] else '하차',
                             e['count'], e['request'].split(':')[0], load))
        sections.append(table(['운행', '시각', '학교', '구분', '인원', '학생 그룹', '차내 인원'], rows) + '</section>')
    css = '''*{box-sizing:border-box}body{font:14px "Malgun Gothic",sans-serif;color:#263238;margin:32px auto;max-width:1100px;padding:0 24px}h1{font-size:26px}h2{font-size:21px;margin-top:42px;border-bottom:2px solid #354e60;padding-bottom:10px}h3{font-size:18px}p,figcaption{font-size:12px;color:#58636b;line-height:1.8}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.summary div{border:1px solid #dce0e3;padding:16px}.summary span{display:block;font-size:12px}.summary strong{display:block;font-size:22px;margin-top:8px}nav{display:flex;gap:20px;margin:24px 0}a{color:#345d83}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #dde2e6;padding:9px;text-align:left}th{background:#eef1f4;white-space:nowrap}tr:nth-child(even){background:#f8f9fa}.table{overflow:auto;margin:14px 0}figure{margin:18px 0}svg{width:100%;max-height:360px;background:#f7f9fb;border:1px solid #dce0e3}.vehicle{margin-bottom:40px}button{border:1px solid #ccd3d9;background:white;padding:10px 16px;cursor:pointer;float:right}@media(max-width:650px){.summary{grid-template-columns:repeat(2,1fr)}body{padding:0 12px}.table{font-size:11px}}@media print{@page{size:A4 landscape;margin:12mm}body{margin:0;padding:0;max-width:none;font-size:11px}button,nav{display:none}h2,h3{break-after:avoid}.vehicle{break-before:page}thead{display:table-header-group}tr,figure,.summary{break-inside:avoid}.table{overflow:visible}th,td{padding:6px;font-size:10px}a{color:inherit}}'''
    return ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>학교 및 차량 운행 계획</title><style>' + css + '</style><body>'
            '<button onclick="window.print()">인쇄 / PDF 저장</button>' + ''.join(sections) + '</body></html>')
