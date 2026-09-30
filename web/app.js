'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (x, digits=0) => x == null ? '—' : Number(x).toLocaleString('ko-KR', {maximumFractionDigits:digits});
const hhmm = minutes => `${String(Math.floor(minutes/60)).padStart(2,'0')}:${String(Math.floor(minutes%60)).padStart(2,'0')}`;
const minutes = value => { const [h,m] = value.split(':').map(Number); return h*60+m; };
const labels = {carbon:'저탄소 우선', cost:'운영비 우선', time:'탑승시간 우선'};
let config, schools = [], selected = new Set(), scenario, matrix, results;
let currentPlan = 0, currentVehicle = null, busy = false, dirty = false, jsKey = '';
let kakaoMap, mapObjects = [];
let matrixState='empty', resultMatrix=null, publicMode=false, scenarioTests=null;

async function api(path, body) {
  const response = await fetch('/api/'+path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok || data.error && !data.status) throw new Error(data.error || '요청 실패');
  return data;
}
function report(error) { $('alert').textContent = error.message || error; $('alert').hidden = false; }
function clearError() { $('alert').hidden = true; }
function saveSession() { if(scenario)sessionStorage.setItem('ieo-scenario',JSON.stringify(scenario)); }
function edited() { dirty = true; $('stale').hidden = !results; syncCount(); saveSession(); }
function syncCount() {
  if (!scenario) return;
  const total = scenario.cohorts.reduce((sum,g)=>sum+Number(g.students),0);
  $('simple-count').textContent=`현재 수요: ${scenario.schools.length}개교, ${fmt(total)}명`;
  $('side-count').textContent = `${scenario.schools.length}개교, ${fmt(total)}명`;
  $('api-count').textContent = `${scenario.schools.length}개교 기준 최대 ${fmt(scenario.schools.length*(scenario.schools.length-1))}회 경로 조회`;
}
function setBusy(value) {
  busy = value;
  document.querySelectorAll('button,input,select,textarea').forEach(e => { e.disabled=value; });
  if (!value) {
    $('export-roads').disabled = matrixState!=='ready'||!matrix;
    $('export-result').disabled = !results;
    $('export-csv').disabled = !getPlan();
    $('export-report').disabled = !getPlan();
    $('test-export').disabled = !scenarioTests;
    if(publicMode){
      $('matrix-source').querySelector('[value="kakao"]').disabled=true;
      $('prepare-matrix').disabled=$('matrix-source').value==='kakao';
    }
  }
}
async function job(start) {
  $('progress-box').hidden = false;
  $('progress').value = 0;
  $('progress-message').textContent = '준비 중';
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 350));
    const j = await api('jobs/'+start.job_id);
    $('progress').value = j.progress;
    $('progress-message').textContent = j.message;
    $('progress-percent').textContent = Math.round(j.progress*100)+'%';
    if (j.status === 'error') throw new Error(j.error);
    if (j.status === 'done') return j.result;
  }
}
async function action(fn) {
  if (busy) return;
  clearError(); setBusy(true);
  try { await fn(); } catch (error) { report(error); }
  finally { setBusy(false); $('progress-box').hidden=true; }
}
function switchTab(tab) {
  document.querySelectorAll('.page').forEach(e=>e.classList.toggle('active', e.id===tab));
  document.querySelectorAll('.nav').forEach(e=>e.classList.toggle('active', e.dataset.tab===tab));
  if (tab==='overview') { if(kakaoMap) kakaoMap.relayout(); drawMap(); }
  window.scrollTo({top:0, behavior:'smooth'});
}
document.querySelectorAll('.nav').forEach(e=>e.onclick=()=>switchTab(e.dataset.tab));

const fields = {
  'generator-fields': [ ['student_count','이동 학생 수','명',1], ['school_count','추첨 학교 수','개교',1], ['seed','난수 seed','같은 값 = 같은 시나리오',1], ['cohort_min','그룹 최소 인원','명',1], ['cohort_max','그룹 최대 인원','명',1], ['continuation_rate','다음 수업 추가 확률','0~1',0.05], ['class_minutes','수업 길이','분',5] ],
  'time-fields': [['day_start','공통 운행 시작','시각','time'],['day_end','공통 운행 종료','시각','time'],['arrival_buffer','수업 전 도착 여유','분',1],['early_arrival','수업 전 최대 조기 도착','분',1],['max_ride','최대 탑승시간','분',5],['max_wait','수업 후 최대 승차 대기','분',5],['stop_minutes','정차 기본 시간','분',0.5],['boarding_seconds','학생 1명 승하차 시간','초',1]],
  'energy-fields': [['electricity_factor','전력 배출계수','kgCO₂e/kWh, 가정',0.01],['charging_efficiency','충전효율','0 초과~1',0.01],['electricity_price','전력 단가','원/kWh',10],['road_factor','추정 도로 거리 배율','직선거리 대비',0.05],['speed_kmh','추정 평균속도','km/h',1],['traffic_factor','교통 소요시간 배율','기준 행렬 대비',0.1]],
  'fleet-fields': [['max_vehicles','전체 차량 대수 상한','대',1],['batch_size','계산용 분할 인원','명',1],['max_events','운행당 최대 승하차 사건','개',2],['max_seconds','대안당 경로 개선 예산','초, 이후 직접 배정',1],['carbon_time_weight','탄소안 탑승시간 가중치','kgCO₂e/(명×분)',0.001],['time_distance_weight','시간안 거리 가중치','명×분/km',0.1],['cost_runs_per_day','비용안 일일 운행횟수 가정','회, 탐색용 고정비 배분',1]],
};
function renderFields() {
  config.cohort_min ??= 1;
  config.cohort_max ??= config.cohort_size ?? 40;
  Object.entries(fields).forEach(([id, specs])=>{
    $(id).innerHTML=specs.map(([key,label,unit,step])=>`<label>${label}<span class="unit">${unit}</span><input data-config="${key}" type="${step==='time'?'time':'number'}" ${step==='time'?'':`step="${step}"`} value="${step==='time'?hhmm(config[key]):config[key]}"></label>`).join('');
    $(id).querySelectorAll('input').forEach(input=>input.onchange=()=>{config[input.dataset.config]=input.type==='time'?minutes(input.value):Number(input.value); edited();});
  });
  $('generator-fields').insertAdjacentHTML('beforeend', `<label>수업 시작 시각들<span class="unit">쉼표로 구분</span><input id="class-starts" value="${config.class_starts.map(hhmm).join(', ')}"></label>`);
  $('class-starts').onchange=()=>{ config.class_starts=$('class-starts').value.split(',').map(x=>minutes(x.trim())); edited(); };
  $('return-home').checked=config.return_home;
  $('return-home').onchange=()=>{config.return_home=$('return-home').checked; edited();};
  $('depot-select').innerHTML='<option value="">자동, 학교 간 거리 합이 작은 학교</option>'+schoolOptions(config.depot_id);
  $('depot-select').value=config.depot_id;
  $('depot-select').onchange=()=>{config.depot_id=$('depot-select').value;edited();};
}
function schoolOptions(value) {
  return (scenario?.schools || []).map(s=>`<option value="${esc(s.id)}" ${s.id===value?'selected':''}>${esc(s.name)}</option>`).join('');
}
function name(id) { return (scenario?.schools.find(s=>s.id===id)?.name) || id; }
function renderSchools() {
  const query=$('school-search').value.trim();
  $('school-picker').innerHTML=schools.filter(s=>s.name.includes(query)||s.address.includes(query)).map(s=>`<label class="school-choice"><input type="checkbox" data-school="${s.id}" ${selected.has(s.id)?'checked':''}><span>${esc(s.name)}</span></label>`).join('');
  $('school-picker').querySelectorAll('input').forEach(e=>e.onchange=()=>{e.checked?selected.add(e.dataset.school):selected.delete(e.dataset.school);$('school-count-label').textContent=`선택 ${selected.size}개교, 적용하려면 수요 재생성`;});
  $('school-count-label').textContent=`현재 시나리오 ${scenario.schools.length}개교 / 선택 ${selected.size}개교`;
  $('school-edit').innerHTML=`<table><thead><tr><th>선택 학교</th><th>위도</th><th>경도</th></tr></thead><tbody>${scenario.schools.map((s,i)=>`<tr><td>${esc(s.name)}</td><td><input type="number" step="0.000001" value="${s.lat}" data-school-index="${i}" data-coordinate="lat" aria-label="${esc(s.name)} 위도"></td><td><input type="number" step="0.000001" value="${s.lng}" data-school-index="${i}" data-coordinate="lng" aria-label="${esc(s.name)} 경도"></td></tr>`).join('')}</tbody></table>`;
  $('school-edit').querySelectorAll('input').forEach(e=>e.onchange=()=>{scenario.schools[Number(e.dataset.schoolIndex)][e.dataset.coordinate]=Number(e.value);edited();drawMap();});
}
function renderDemand() {
  const sizes=scenario.cohorts.map(g=>g.students);
  $('group-size-summary').textContent=`${sizes.length}개 그룹 / 1명 그룹 ${sizes.filter(n=>n===1).length}개 / 최소 ${sizes.length?Math.min(...sizes):0}명 / 최대 ${sizes.length?Math.max(...sizes):0}명`;
  $('demand-table').innerHTML=`<table><thead><tr><th>그룹</th><th>학생 수</th><th>소속 학교</th><th>수업 학교, 시작, 종료</th><th></th></tr></thead><tbody>${scenario.cohorts.map((g,i)=>`<tr><td>${esc(g.id)}</td><td><input aria-label="${esc(g.id)} 학생 수" type="number" min="1" value="${g.students}" data-group="${i}" data-field="students"></td><td><select aria-label="${esc(g.id)} 소속 학교" class="group-home" data-group="${i}" data-field="home">${schoolOptions(g.home)}</select></td><td>${g.classes.map((cl,j)=>`<div class="class-inputs"><select aria-label="${esc(g.id)} 수업 ${j+1} 학교" data-group="${i}" data-class="${j}" data-field="school_id">${schoolOptions(cl.school_id)}</select><input type="time" aria-label="${esc(g.id)} 수업 ${j+1} 시작" value="${hhmm(cl.start)}" data-group="${i}" data-class="${j}" data-field="start"><span>~</span><input type="time" aria-label="${esc(g.id)} 수업 ${j+1} 종료" value="${hhmm(cl.end)}" data-group="${i}" data-class="${j}" data-field="end"><button class="delete" data-remove-class="${i},${j}" aria-label="수업 삭제">×</button></div>`).join('')}<button class="quiet" data-add-class="${i}">다음 수업 +</button></td><td><button class="delete" data-remove-group="${i}" aria-label="${esc(g.id)} 그룹 삭제">×</button></td></tr>`).join('')}</tbody></table>`;
  $('demand-table').querySelectorAll('input,select').forEach(e=>e.onchange=()=>{
    const g=scenario.cohorts[Number(e.dataset.group)];
    const target=e.dataset.class===undefined?g:g.classes[Number(e.dataset.class)];
    target[e.dataset.field]=e.type==='time'?minutes(e.value):e.type==='number'?Number(e.value):e.value; edited();
  });
  $('demand-table').querySelectorAll('[data-remove-class]').forEach(e=>e.onclick=()=>{const [i,j]=e.dataset.removeClass.split(',').map(Number);scenario.cohorts[i].classes.splice(j,1);edited();renderDemand();});
  $('demand-table').querySelectorAll('[data-add-class]').forEach(e=>e.onclick=()=>{const g=scenario.cohorts[Number(e.dataset.addClass)];const start=(g.classes.at(-1)?.end||570)+90;g.classes.push({school_id:scenario.schools[0].id,start,end:start+50});edited();renderDemand();});
  $('demand-table').querySelectorAll('[data-remove-group]').forEach(e=>e.onclick=()=>{scenario.cohorts.splice(Number(e.dataset.removeGroup),1);edited();renderDemand();});
}
const vehicleColumns=[['name','차종','text'],['capacity','학생 정원'],['kwh_per_km','전비<br>kWh/km'],['usable_kwh','가용 배터리<br>kWh'],['manufacturing','제조 배출<br>kgCO₂e'],['lifetime_km','생애 주행<br>km'],['daily_cost','운전자 포함<br>원/(대×일)'],['cost_per_km','전력 제외 변동비<br>원/km']];
const baselineColumns=[['name','이동수단','text'],['share','비율<br>합계 1'],['manufacturing','제조 배출<br>kgCO₂e'],['lifetime_km','생애 주행<br>km'],['operation_per_km','운행 배출<br>kgCO₂e/km'],['occupancy','평균 동승<br>명'],['distance_factor','거리 배율'],['empty_ratio','추가 공차 비율'],['speed_kmh','속도<br>km/h'],['wait_minutes','접근, 대기<br>분']];
function editableTable(id,key,columns,remove=false) {
  $(id).innerHTML=`<table><thead><tr>${columns.map(col=>`<th>${col[1]}</th>`).join('')}${remove?'<th></th>':''}</tr></thead><tbody>${config[key].map((row,i)=>`<tr>${columns.map(([k,label,type])=>`<td><input aria-label="${esc(row.name)} ${label.replace(/<br>/g,' ')}" type="${type||'number'}" step="any" value="${esc(row[k])}" data-index="${i}" data-key="${k}"></td>`).join('')}${remove?`<td><button class="delete" data-index="${i}" aria-label="${esc(row.name)} 삭제">×</button></td>`:''}</tr>`).join('')}</tbody></table>`;
  $(id).querySelectorAll('input').forEach(e=>e.onchange=()=>{config[key][Number(e.dataset.index)][e.dataset.key]=e.type==='number'?Number(e.value):e.value;edited();});
  $(id).querySelectorAll('button').forEach(e=>e.onclick=()=>{config[key].splice(Number(e.dataset.index),1);edited();editableTable(id,key,columns,remove);});
}
function renderEditors() {
  $('simple-schools').value=config.school_count;
  $('simple-students').value=config.student_count;
  renderFields(); renderSchools(); renderDemand();
  editableTable('vehicle-table','vehicles',vehicleColumns,true);
  editableTable('baseline-table','baseline',baselineColumns);
  syncCount();
}
async function generate(useSelection=false) {
  const body={config};if(useSelection)body.school_ids=[...selected];
  scenario=await api('generate',body);config=scenario.config;
  selected=new Set(scenario.schools.map(s=>s.id));
  results=null;currentVehicle=null;dirty=false;$('stale').hidden=true;
  const preferred=$('matrix-source').value;
  if(preferred==='kakao') {
    const same=matrix?.source==='kakao'&&scenario.schools.every(s=>matrix.schools.some(m=>m.id===s.id&&m.lat===s.lat&&m.lng===s.lng));
    if(!same){matrixState='empty';matrix=null;showMatrixStatus('학교가 변경되었습니다. 카카오 도로 정보를 준비하세요.','카카오 조회 필요');}
  }else await prepareMatrix('estimated');
  saveSession();
  renderEditors();renderResults();
}
function showMatrixStatus(message, badge) {
  $('matrix-status').textContent=message;$('matrix-status').hidden=false;
  $('source-pill').textContent=badge;
  // Keep the status next to the action, including failure details.
  $('prepare-matrix').insertAdjacentElement('afterend',$('matrix-status'));
}
async function prepareMatrix(source) {
  $('matrix-source').value=source;
  matrixState='loading';
  showMatrixStatus(source==='kakao'?'카카오 도로 정보를 조회하고 있습니다. 완료 전에는 실도로 계산을 실행하지 않습니다.':'추정 도로를 준비하고 있습니다.',source==='kakao'?'카카오 조회 중':'추정 정보 준비 중');
  try {
    const response=await api('matrix',{scenario,source});
    if(response.job_id)await job(response);
    const next=await api('matrix');
    if(next.source!==source)throw new Error('요청한 도로 모드와 서버 데이터가 일치하지 않습니다. 다시 준비하세요.');
    matrix=next;matrixState='ready';
    showMatrixStatus(source==='kakao'?`카카오 도로 조회 완료, ${matrix.schools.length}개교, ${matrix.fetched_at}`:'추정 도로 준비 완료',source==='kakao'?'카카오 실도로 연결':'추정 도로 모드');
    drawMap();
  }catch(error){
    matrixState='error';
    showMatrixStatus('도로 조회 실패: '+error.message+' 이전 데이터로 자동 계산하지 않습니다.',source==='kakao'?'카카오 조회 실패':'도로 준비 실패');
    throw error;
  }
}
function getPlan() {const p=results?.plans[currentPlan];return p?.status==='feasible'?p:null;}
function getVehicle() {const p=getPlan();return p?.fleet.find(v=>v.id===currentVehicle)||p?.fleet[0];}
function renderBaselineMethod(plan) {
  const target=$('baseline-method-content');
  if(!plan){target.textContent='계산 전';return;}
  const modes=plan.baseline.modes, assumptions=results.scenario.config.baseline;
  if(!Array.isArray(modes)||modes.length!==assumptions.length){target.textContent='수단별 계산 내역 없음';return;}
  const source=resultMatrix?.source==='kakao'?'카카오 도로 거리':'추정 도로 거리';
  const baseDistance=plan.rides.reduce((sum,r)=>sum+resultMatrix.pairs[r.origin+'|'+r.destination].km*r.count,0);
  target.innerHTML=`<div class="baseline-formulas">
    <div>거리 기준: ${source} / 계산 당시 입력 기준</div>
    <div>수단별 학생 이동거리(명×km) = Σ(구간 거리 × 이동 인원) × 수단 비율 × 거리 배율</div>
    <div>배분 차량거리(km) = 수단별 학생 이동거리 ÷ 평균 동승 인원 × (1 + 공차 비율)</div>
    <div>제조 배분량(kgCO₂e) = 배분 차량거리 × 차량 제조 배출량 ÷ 생애 주행거리</div>
    <div>운행 배출량(kgCO₂e) = 배분 차량거리 × 운행 배출계수</div>
  </div><div class="table-scroll"><table><thead><tr><th>이동수단</th><th>수단 비율</th><th>학생 이동거리<br>명×km</th><th>배분 차량거리<br>km</th><th>제조 배분량<br>kgCO₂e</th><th>운행 배출량<br>kgCO₂e</th><th>합계<br>kgCO₂e</th></tr></thead><tbody>${modes.map((m,i)=>{
    const a=assumptions[i],distance=m.passenger_km/a.occupancy*(1+a.empty_ratio);
    return `<tr><td>${esc(m.name)}</td><td>${fmt(a.share*100,2)}%</td><td>${fmt(m.passenger_km,2)}</td><td>${fmt(distance,2)}</td><td>${fmt(m.manufacturing,2)}</td><td>${fmt(m.operation,2)}</td><td>${fmt(m.total,2)}</td></tr>`;
  }).join('')}</tbody></table></div>${modes.map((m,i)=>{
    const a=assumptions[i],distance=m.passenger_km/a.occupancy*(1+a.empty_ratio);
    return `<details class="baseline-mode"><summary>${esc(m.name)} 계산식</summary><p>수단 비율 ${fmt(a.share*100,2)}% / 거리 배율 ${fmt(a.distance_factor,4)}<br>
    학생 이동거리: ${fmt(baseDistance,2)} × ${fmt(a.share,4)} × ${fmt(a.distance_factor,4)} = ${fmt(m.passenger_km,2)}명×km<br>
    배분 차량거리: ${fmt(m.passenger_km,2)} ÷ ${fmt(a.occupancy,4)} × (1 + ${fmt(a.empty_ratio,4)}) = ${fmt(distance,2)}km<br>
    제조 배분량: ${fmt(distance,2)} × ${fmt(a.manufacturing,2)} ÷ ${fmt(a.lifetime_km,2)} = ${fmt(m.manufacturing,2)}kgCO₂e<br>
    운행 배출량: ${fmt(distance,2)} × ${fmt(a.operation_per_km,6)} = ${fmt(m.operation,2)}kgCO₂e<br>
    합계: ${fmt(m.manufacturing,2)} + ${fmt(m.operation,2)} = ${fmt(m.total,2)}kgCO₂e</p></details>`;
  }).join('')}<div class="baseline-total">전체 합계: 제조 ${fmt(plan.baseline.manufacturing,2)} + 운행 ${fmt(plan.baseline.operation,2)} = ${fmt(plan.baseline.total,2)}kgCO₂e</div>`;
}
function renderResults() {
  const p=getPlan();renderBaselineMethod(p);const totalStudents=scenario?.cohorts.reduce((s,g)=>s+g.students,0)||0;
  const metricData=p?[
    ['추정 탄소 '+(p.totals.reduction>=0?'감축':'증가'),fmt(Math.abs(p.totals.reduction),1),'kgCO₂e'],
    ['이동 참여 학생',fmt(p.totals.students),'명'],
    ['제안 차량',fmt(p.totals.vehicles),'대'],
    ['평균 탑승시간',fmt(p.totals.ride_minutes,1),'분'],
  ]:[['추정 탄소 감축','—','kgCO₂e'],['이동 참여 학생',fmt(totalStudents),'명'],['제안 차량','—','대'],['평균 탑승시간','—','분']];
  $('metrics').innerHTML=metricData.map(([title,value,unit])=>`<div class="metric"><div class="metric-label">${title}</div><div class="metric-value">${value}<small>${unit}</small></div></div>`).join('');
  $('alternatives').innerHTML=['carbon','cost','time'].map((objective,i)=>{
    const r=results?.plans[i];
    return `<button class="alternative ${p&&i===currentPlan?'selected':''}" data-plan="${i}"><div class="alternative-head"><strong>${labels[objective]}</strong><span class="tag ${r?.dominated?'neutral':''}">${r?.status==='feasible'?(r.dominated?'다른 안이 모든 지표에서 우세':'비교 후보'):'계산 대기'}</span></div>${r?.status==='feasible'?`<div class="alternative-values"><div>${fmt(r.totals.total,1)}<small>kgCO₂e / 하루</small></div><div>${fmt(r.totals.cost/10000,1)}만<small>원 / 하루</small></div><div>${fmt(r.totals.ride_minutes,1)}분<small>평균 탑승시간</small></div></div>`:r?.error?`<div class="error-text">${esc(r.error)}</div>`:''}</button>`;
  }).join('');
  $('alternatives').querySelectorAll('button').forEach(e=>e.onclick=()=>{const i=Number(e.dataset.plan);if(results?.plans[i]?.status==='feasible'){currentPlan=i;currentVehicle=null;renderResults();}});
  $('fleet-count').textContent=p?fmt(p.fleet.length):'—';
  $('fleet-summary').textContent=p?`${p.fleet.length}대, ${fmt(p.totals.empty_km)}km 공차 포함`:'계산 전';
  $('fleet-list').innerHTML=p?p.fleet.map(v=>`<button class="vehicle-row ${v.id===getVehicle()?.id?'selected':''}" data-vehicle="${v.id}"><div class="row"><strong>${v.id}, ${esc(v.type.name)}</strong><span>${v.type.capacity}석</span></div><small>${v.runs.length}회 운행, ${fmt(v.km,1)}km, ${fmt(v.total,1)}kgCO₂e</small></button>`).join(''):'<div class="empty-state">계산 전</div>';
  $('fleet-list').querySelectorAll('button').forEach(e=>e.onclick=()=>{currentVehicle=e.dataset.vehicle;renderVehicle();$('fleet-list').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b.dataset.vehicle===currentVehicle));});
  $('export-result').disabled=!results;$('export-csv').disabled=!p;$('export-report').disabled=!p;
  if(p){
    const max=Math.max(p.baseline.total,p.totals.total,0.001);
    $('carbon-chart').innerHTML=[['기존 이동',p.baseline],['선택한 대안',p.totals]].map(([label,x])=>`<div class="bar-row"><div class="bar-label"><span>${label}</span><b>${fmt(x.total,1)} kgCO₂e</b></div><div class="bar-track"><div class="bar-manufacturing" style="width:${x.manufacturing/max*100}%" title="제조 ${fmt(x.manufacturing,2)}kgCO₂e"></div><div class="bar-operation" style="width:${x.operation/max*100}%" title="운행 ${fmt(x.operation,2)}kgCO₂e"></div></div></div>`).join('')+'<div class="carbon-legend"><span>제조 배분량</span><span>운행 배출량</span></div>'+`<p class="hint">셔틀 제조 ${fmt(p.totals.manufacturing,1)} + 운행 ${fmt(p.totals.operation,1)} kgCO₂e<br>기존 이동시간 추정 ${fmt(p.totals.baseline_minutes,1)}분/회<br>셔틀 탑승, 수업 후 대기, 조기 도착 ${fmt(p.totals.journey_minutes,1)}분/회</p>`;
  }else $('carbon-chart').innerHTML='<div class="empty-state">계산 전</div>';
  renderVehicle();
}
function renderVehicle() {
  const v=getVehicle();
  $('timeline-title').textContent=v?`${v.id}, ${v.type.name} 시간표`:'차량 운행 시간표';
  if(v){
    const rows=[];
    for(const [ri,run] of v.runs.entries()){
      let load=0;
      run.events.forEach((e,i)=>{load+=e.pickup?e.count:-e.count;rows.push(`<tr><td>${ri+1}</td><td>${hhmm(run.times[i])}</td><td><span class="route-dot"></span>${esc(name(e.school))}</td><td>${e.pickup?'승차':'하차'} ${e.count}명</td><td>${load} / ${v.type.capacity}</td><td>${esc(e.request.split(':')[0])}</td></tr>`);});
    }
    $('timeline').innerHTML=`<table><thead><tr><th>운행</th><th>시각</th><th>학교</th><th>승하차</th><th>차내 인원</th><th>학생 그룹</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
  }else $('timeline').innerHTML='<div class="empty-state">계산 전</div>';
  drawMap();
}

function drawMap() {
  if(!scenario)return;
  const p=getPlan(), v=getVehicle();
  $('depot-info').textContent='거점: '+(p?name(p.depot):config.depot_id?name(config.depot_id):'자동 선정');
  $('map-caption').textContent=kakaoMap?(matrix?.source==='kakao'?'카카오 도로':'추정 경로'):'학교 좌표';
  $('map-info').textContent=v?`${v.id}, ${fmt(v.km,1)}km, ${fmt(v.battery_kwh,1)}/${v.type.usable_kwh}kWh`:'';
  const byId=Object.fromEntries(scenario.schools.map(s=>[s.id,s]));
  if(kakaoMap){
    $('schematic').hidden=true;$('kakao-map').hidden=false;
    mapObjects.forEach(o=>o.setMap(null));mapObjects=[];
    const bounds=new kakao.maps.LatLngBounds();
    scenario.schools.forEach(s=>{
      const position=new kakao.maps.LatLng(s.lat,s.lng);bounds.extend(position);
      const marker=new kakao.maps.Marker({position,map:kakaoMap,title:s.name});mapObjects.push(marker);
      kakao.maps.event.addListener(marker,'click',()=>{$('map-info').textContent=s.name+', '+s.address;});
    });
    for(const leg of v?.legs||[]){
      if(!leg.km)continue;
      const points=matrix?.pairs[leg.from+'|'+leg.to]?.path;
      const path=(points?.length?points:[[byId[leg.from].lat,byId[leg.from].lng],[byId[leg.to].lat,byId[leg.to].lng]]).map(([lat,lng])=>new kakao.maps.LatLng(lat,lng));
      const line=new kakao.maps.Polyline({map:kakaoMap,path,strokeWeight:4,strokeColor:leg.load?'#16765c':'#bc854f',strokeOpacity:.75,strokeStyle:leg.load?'solid':'shortdash'});mapObjects.push(line);
    }
    kakaoMap.relayout();kakaoMap.setBounds(bounds);return;
  }
  const width=760,height=390,pad=55;
  const lats=scenario.schools.map(s=>s.lat),lngs=scenario.schools.map(s=>s.lng);
  const minLat=Math.min(...lats),maxLat=Math.max(...lats),minLng=Math.min(...lngs),maxLng=Math.max(...lngs);
  const scale=Math.min((width-2*pad)/Math.max(.001,(maxLng-minLng)*Math.cos(minLat*Math.PI/180)),(height-2*pad)/Math.max(.001,maxLat-minLat));
  const project=(lat,lng)=>[width/2+(lng-(minLng+maxLng)/2)*Math.cos(minLat*Math.PI/180)*scale,height/2-(lat-(minLat+maxLat)/2)*scale];
  let lines='';
  for(const leg of v?.legs||[]){
    if(!leg.km)continue;
    const points=matrix?.pairs[leg.from+'|'+leg.to]?.path;
    const path=(points?.length?points:[[byId[leg.from].lat,byId[leg.from].lng],[byId[leg.to].lat,byId[leg.to].lng]]).map(([a,b])=>project(a,b).join(',')).join(' ');
    lines+=`<polyline points="${path}" fill="none" stroke="${leg.load?'#16765c':'#bc854f'}" stroke-width="2.2" opacity=".6" ${leg.load?'':'stroke-dasharray="5 4"'}/>`;
  }
  const pins=scenario.schools.map((s,i)=>{const [x,y]=project(s.lat,s.lng);const depot=p?.depot===s.id;return `<g class="school-pin" data-pin="${s.id}" tabindex="0" role="button" aria-label="${esc(s.name)}" style="cursor:pointer"><title>${esc(s.name)}</title><circle cx="${x}" cy="${y}" r="${depot?9:5}" fill="${depot?'#d4b46e':'#338468'}" stroke="white" stroke-width="2"/><text x="${x+8}" y="${y+(i%2?12:-8)}" font-size="8" fill="#385e4b" paint-order="stroke" stroke="#f1f5ed" stroke-width="3">${esc(s.name)}</text></g>`;}).join('');
  $('schematic').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="부산 학교 위치와 선택 차량의 개략 경로"><defs><pattern id="grid" width="35" height="35" patternUnits="userSpaceOnUse"><path d="M 35 0 L 0 0 0 35" fill="none" stroke="#dce4d5" stroke-width=".65"/></pattern></defs><rect width="100%" height="100%" fill="#edf2e7"/><rect width="100%" height="100%" fill="url(#grid)"/><text x="25" y="32" font-size="10" fill="#8da284" letter-spacing="3">부산 학교 위치</text><text x="710" y="30" font-size="13" fill="#75946d">N ↑</text>${lines}${pins}<text x="520" y="369" font-size="9" fill="#8b9b80">${matrix?.source==='kakao'?'카카오 도로':'학교 좌표'}</text></svg>`;
  $('schematic').querySelectorAll('[data-pin]').forEach(e=>{const show=()=>{$('map-info').textContent=name(e.dataset.pin)+', '+byId[e.dataset.pin].address;};e.onclick=show;e.onkeydown=ev=>{if(ev.key==='Enter')show();};});
}
async function loadKakao() {
  if(!jsKey||kakaoMap)return;
  if(!window.kakao?.maps){
    await new Promise((resolve,reject)=>{
      const script=document.createElement('script');
      script.src='https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey='+encodeURIComponent(jsKey);
      script.onload=resolve;script.onerror=()=>reject(new Error('지도 SDK를 불러오지 못했습니다. JavaScript 키와 도메인을 확인하세요.'));
      document.head.appendChild(script);
    });
  }
  if(!window.kakao?.maps)throw new Error('카카오 지도 초기화 실패. 키와 등록 도메인을 확인하세요.');
  await new Promise(resolve=>kakao.maps.load(resolve));
  $('kakao-map').hidden=false;
  kakaoMap=new kakao.maps.Map($('kakao-map'),{center:new kakao.maps.LatLng(35.17,129.06),level:9});drawMap();
}

async function download(body) {
  const file=await api('export',body);
  const a=document.createElement('a');a.href=file.url;a.download=file.filename;
  document.body.appendChild(a);a.click();a.remove();
}
async function applyScenario(value) {
  await api('validate',{scenario:value});
  scenario=value;config=value.config;selected=new Set(value.schools.map(s=>s.id));
  results=null;dirty=false;$('stale').hidden=true;currentVehicle=null;
  if($('matrix-source').value==='kakao'){
    matrixState='empty';matrix=null;showMatrixStatus('입력이 변경되었습니다. 카카오 도로 정보를 준비하세요.','카카오 조회 필요');
  }else await prepareMatrix('estimated');
  saveSession();renderEditors();renderResults();
}
$('solve').onclick=()=>action(async()=>{
  if(matrixState!=='ready'||matrix?.source!==$('matrix-source').value)throw new Error('선택한 모드의 도로 정보를 먼저 준비하세요. 조회 실패 시 기존 추정 데이터로 계산하지 않습니다.');
  scenario.config=config;
  results=await job(await api('solve',{scenario,source:$('matrix-source').value}));
  matrix=await api('matrix');resultMatrix=matrix;currentPlan=Math.max(0,results.plans.findIndex(p=>p.status==='feasible'&&!p.dominated));
  currentVehicle=null;dirty=false;$('stale').hidden=true;renderResults();switchTab('overview');
});
const sharedControls = ['road-controls','source-pill','solve'].map(id=>{
  const element=$(id), anchor=document.createComment(id);
  element.before(anchor);
  return {element,anchor};
});
function setMode(simple) {
  if(config){$('simple-schools').value=config.school_count;$('simple-students').value=config.student_count;}
  document.body.classList.toggle('simple-mode',simple);
  $('simple-mode').setAttribute('aria-pressed',String(simple));
  $('detail-mode').setAttribute('aria-pressed',String(!simple));
  sharedControls.forEach(({element,anchor})=>{
    if(simple)$(element.id==='road-controls'?'simple-road-slot':'simple-solve-slot').append(element);
    else anchor.after(element);
  });
  localStorage.setItem('shuttle-ui-mode',simple?'simple':'detail');
  switchTab('overview');
}
$('simple-mode').onclick=()=>setMode(true);
$('detail-mode').onclick=()=>setMode(false);
$('simple-generate').onclick=()=>action(async()=>{
  const schoolCount=Number($('simple-schools').value), studentCount=Number($('simple-students').value);
  if(!Number.isInteger(schoolCount)||schoolCount<2||schoolCount>schools.length||!Number.isInteger(studentCount)||studentCount<1)
    throw new Error(`학교 수는 2~${schools.length}, 학생 수는 1 이상의 정수여야 합니다.`);
  const previous=config;
  config={...config,school_count:schoolCount,student_count:studentCount};
  try { await generate(false); } catch(error) { config=scenario?.config||previous; throw error; }
});
setMode(localStorage.getItem('shuttle-ui-mode')!=='detail');
function scenarioTestInput() {
  if(matrixState!=='ready'||matrix?.source!==$('matrix-source').value)throw new Error('도로 정보를 먼저 준비하세요.');
  return {scenario,source:matrix.source,seed:Number($('test-seed').value),profile:$('test-profile').value,
    repeats:Number($('test-repeats').value),vary_students:$('test-vary-students').checked,vary_modes:$('test-vary-modes').checked};
}
function showScenarioTests() {
  const data=scenarioTests, names={favorable:'유리',mixed:'중간',adverse:'불리'};
  $('test-summary').textContent=`시험 ${data.cases.length}건 / 계산 불가 ${data.cases.filter(c=>!c.totals).length}건 / ${data.vary_modes?'이동수단 비율 변경':'이동수단 비율 고정'}`;
  $('test-results').innerHTML='<table><thead><tr><th>유형 / seed</th><th>추정 범위</th><th>학생 수</th><th>1명당 감축 kgCO₂e</th><th>기존 배출 kgCO₂e</th><th>셔틀 배출 kgCO₂e</th><th>감축 kgCO₂e</th><th>차량 수</th><th>결과</th></tr></thead><tbody>'+data.cases.map((c,i)=>`<tr><td>${names[c.profile]} / ${c.seed}</td><td>${[i===data.best?'최선 추정':'',i===data.worst?'최악 추정':''].filter(Boolean).join(' / ')}</td>${c.totals?`<td>${fmt(c.totals.students)}</td><td>${fmt(c.totals.reduction/c.totals.students,3)}</td><td>${fmt(c.baseline,2)}</td><td>${fmt(c.totals.total,2)}</td><td>${fmt(c.totals.reduction,2)}${c.totals.reduction<0?' (배출 증가)':''}</td><td>${fmt(c.totals.vehicles)}</td><td><button class="quiet" data-test-result="${i}">결과 적용</button></td>`:`<td colspan="7">계산 불가: ${esc(c.error)}</td>`}</tr>`).join('')+'</tbody></table>';
  $('test-results').querySelectorAll('[data-test-result]').forEach(button=>button.onclick=()=>action(async()=>{
    const c=data.cases[Number(button.dataset.testResult)];
    await api('matrix/import',{scenario:c.results.scenario,matrix:data.matrix});
    results=structuredClone(c.results);scenario=structuredClone(results.scenario);config=scenario.config;
    resultMatrix=data.matrix;restoreMatrix(data.matrix);currentPlan=c.plan_index;currentVehicle=null;
    selected=new Set(scenario.schools.map(s=>s.id));dirty=false;$('stale').hidden=true;
    saveSession();renderEditors();renderResults();switchTab('overview');
  }));
}
$('test-random-seed').onclick=()=>{$('test-seed').value=crypto.getRandomValues(new Uint32Array(1))[0];};
$('test-generate').onclick=()=>action(async()=>{
  const value=await api('scenario-case',scenarioTestInput());
  scenario=value;config=value.config;results=null;currentVehicle=null;dirty=false;$('stale').hidden=true;
  saveSession();renderEditors();renderResults();
});
$('test-run').onclick=()=>action(async()=>{
  scenarioTests=await job(await api('scenario-test',scenarioTestInput()));showScenarioTests();
});
$('test-export').onclick=()=>action(()=>download({kind:'results',data:{format:'shuttle-scenario-tests',version:1,...scenarioTests}}));
$('generate').onclick=()=>action(()=>generate(true));
$('resample').onclick=()=>action(()=>generate(false));
$('apply-schools').onclick=()=>action(()=>generate(true));
$('school-search').oninput=renderSchools;
$('prepare-matrix').onclick=()=>action(async()=>{await prepareMatrix($('matrix-source').value);edited();});
$('matrix-source').onchange=()=>{if(publicMode)$('prepare-matrix').disabled=$('matrix-source').value==='kakao';matrixState='empty';showMatrixStatus('선택한 모드의 도로 정보를 준비한 뒤 계산하세요.',$('matrix-source').value==='kakao'?'카카오 조회 필요':'추정 정보 준비 필요');edited();};
$('add-group').onclick=()=>{const id='G'+Date.now().toString().slice(-7);scenario.cohorts.push({id,home:scenario.schools[0].id,students:4,classes:[{school_id:scenario.schools[1].id,start:570,end:620}]});edited();renderDemand();};
$('add-vehicle').onclick=()=>{config.vehicles.push({...config.vehicles[0],id:'ev'+Date.now(),name:'새 전기차'});edited();editableTable('vehicle-table','vehicles',vehicleColumns,true);};
$('refresh-json').onclick=()=>{$('json-editor').value=JSON.stringify(scenario,null,2);};
$('apply-json').onclick=()=>action(()=>applyScenario(JSON.parse($('json-editor').value)));
$('import-input').onchange=e=>action(async()=>{const file=e.target.files[0];if(file)await applyScenario(JSON.parse(await file.text()));e.target.value='';});
$('export-input').onclick=()=>action(()=>download({kind:'scenario',data:scenario}));
$('export-report').onclick=()=>action(async()=>{
  await download({kind:'report',results,matrix:resultMatrix,plan_index:currentPlan});
  $('file-status').textContent='운행 보고서 다운로드 요청 완료';
});
$('export-result').onclick=()=>action(async()=>{
  await download({kind:'results',data:{format:'shuttle-results',version:1,saved_at:new Date().toISOString(),results,matrix:resultMatrix}});
  $('file-status').textContent='결과 다운로드 요청 완료';
});
$('export-roads').onclick=()=>action(async()=>{
  await api('validate',{scenario});
  await download({kind:'roads',data:{format:'shuttle-roads',version:1,saved_at:new Date().toISOString(),matrix}});
  $('file-status').textContent='도로 다운로드 요청 완료';
});
function validateSavedResults(value) {
  const fail=()=>{throw new Error('계산 결과 파일이 올바르지 않습니다.');};
  if(!value?.scenario||!Array.isArray(value.plans)||value.plans.length!==3)fail();
  const ids=new Set(value.scenario.schools?.map(s=>s.id));
  const finite=x=>typeof x==='number'&&Number.isFinite(x);
  const safeText=x=>typeof x==='string'&&!/[<>&"']/.test(x);
  for(const [i,p] of value.plans.entries()) {
    if(p.objective!==['carbon','cost','time'][i]||!['feasible','not_found'].includes(p.status))fail();
    if(p.status!=='feasible'){if(typeof p.error!=='string')fail();continue;}
    if(!ids.has(p.depot)||!Array.isArray(p.fleet)||!p.fleet.length||!Array.isArray(p.rides)||!Array.isArray(p.validation?.checks)||!p.validation.checks.every(x=>typeof x==='string')||!finite(p.seconds))fail();
    for(const key of ['reduction','students','vehicles','ride_minutes','empty_km','total','cost','journey_minutes','manufacturing','operation','baseline_minutes'])if(!finite(p.totals?.[key]))fail();
    for(const key of ['total','manufacturing','operation'])if(!finite(p.baseline?.[key]))fail();
    for(const v of p.fleet) {
      if(!safeText(v.id)||typeof v.type?.name!=='string'||!finite(v.type.capacity)||!finite(v.type.usable_kwh)||!['km','battery_kwh','total'].every(k=>finite(v[k]))||!Array.isArray(v.runs)||!Array.isArray(v.legs))fail();
      for(const leg of v.legs)if(!ids.has(leg.from)||!ids.has(leg.to)||!finite(leg.km)||!finite(leg.load))fail();
      for(const run of v.runs){
        if(!Array.isArray(run.events)||!Array.isArray(run.times)||run.events.length!==run.times.length||!run.times.every(finite))fail();
        for(const e of run.events)if(!ids.has(e.school)||!finite(e.count)||typeof e.request!=='string'||typeof e.pickup!=='boolean')fail();
      }
    }
  }
}
async function readDataFile(input,format) {
  const file=input.files[0];input.value='';
  if(!file)return null;
  if(file.size>100_000_000)throw new Error('파일 크기 상한: 100MB');
  const data=JSON.parse(await file.text());
  if(data.format!==format||data.version!==1||!data.matrix)throw new Error('지원하지 않는 파일 형식입니다.');
  return data;
}
function restoreMatrix(value) {
  matrix=value;matrixState='ready';$('matrix-source').value=value.source;
  showMatrixStatus('도로 파일 로드 완료',value.source==='kakao'?'카카오 도로':'추정 도로');
}
$('import-roads').onchange=()=>action(async()=>{
  const data=await readDataFile($('import-roads'),'shuttle-roads');if(!data)return;
  await api('matrix/import',{scenario,matrix:data.matrix});
  restoreMatrix(data.matrix);edited();drawMap();$('file-status').textContent='도로 로드 완료';
});
$('import-results').onchange=()=>action(async()=>{
  const data=await readDataFile($('import-results'),'shuttle-results');if(!data)return;
  validateSavedResults(data.results);
  await api('matrix/import',{scenario:data.results.scenario,matrix:data.matrix});
  results=data.results;scenario=structuredClone(results.scenario);config=scenario.config;resultMatrix=data.matrix;
  selected=new Set(scenario.schools.map(s=>s.id));
  restoreMatrix(data.matrix);currentPlan=Math.max(0,results.plans.findIndex(p=>p.status==='feasible'));currentVehicle=null;
  dirty=false;$('stale').hidden=true;saveSession();renderEditors();renderResults();switchTab('overview');
  $('file-status').textContent='결과 로드 완료';
});
$('export-csv').onclick=()=>action(async()=>{
  const p=getPlan();if(!p)return;
  await download({kind:'csv',plan:p,scenario:results.scenario});
});
$('map-reset').onclick=drawMap;
$('local-origin').textContent=location.origin;
action(async()=>{
  const boot=await api('bootstrap');config=boot.config;schools=boot.schools;jsKey=boot.js_key;publicMode=!!boot.public_mode;
  if(publicMode){document.body.classList.add('public-mode');$('local-origin').closest('article').hidden=true;}
  $('key-status').textContent=`파일 설정, REST 키 ${boot.rest_key_set?'설정됨':'비어 있음'}, 지도 키 ${jsKey?'설정됨':'비어 있음'}`;
  const saved=sessionStorage.getItem('ieo-scenario');
  const existing=await api('matrix').catch(()=>null);
  if(saved&&existing?.source==='kakao'){
    const value=JSON.parse(saved);await api('validate',{scenario:value});
    const same=value.schools.every(s=>existing.schools.some(m=>m.id===s.id&&m.lat===s.lat&&m.lng===s.lng));
    if(same){scenario=value;config=value.config;selected=new Set(value.schools.map(s=>s.id));matrix=existing;matrixState='ready';$('matrix-source').value='kakao';showMatrixStatus('기존 카카오 실도로 정보를 유지했습니다.','카카오 실도로 연결');renderEditors();renderResults();}
    else await applyScenario(value);
  }
  else if(saved){try{await applyScenario(JSON.parse(saved));}catch(error){sessionStorage.removeItem('ieo-scenario');throw error;}}
  else if(boot.default_scenario&&existing?.source){
    scenario=boot.default_scenario;config=scenario.config;selected=new Set(scenario.schools.map(s=>s.id));
    restoreMatrix(existing);showMatrixStatus('기본 도로 파일 로드 완료',existing.source==='kakao'?'카카오 도로':'추정 도로');
    saveSession();renderEditors();renderResults();
  }
  else await generate();
  if(boot.rest_key_set&&matrix?.source!=='kakao'){$('matrix-source').value='kakao';matrixState='empty';showMatrixStatus('키 로드 완료. 도로 조회 필요.','카카오 조회 필요');}
  await loadKakao();
});

