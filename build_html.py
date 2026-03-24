#!/usr/bin/env python3
"""
build_html.py  –  Generate index.html from garmin.db.

Usage:
    uv run python build_html.py           # reads garmin.db, writes index.html
    uv run python build_html.py --db PATH --out PATH
"""

import argparse
import json
import sqlite3
from pathlib import Path


def load_data(db_path: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT
            date,
            total_steps,
            ROUND(wellness_distance_m / 1000.0, 2)   AS distance_km,
            ROUND(wellness_distance_m * 0.000621371, 2) AS distance_mi,
            floors_ascended,
            active_kilocalories,
            bmr_kilocalories,
            total_kilocalories,
            avg_heart_rate,
            max_heart_rate,
            resting_heart_rate,
            hrv_last_night_avg,
            hrv_weekly_avg,
            hrv_status,
            avg_stress_level,
            max_stress_level,
            body_battery_highest,
            body_battery_lowest,
            body_battery_most_recent,
            ROUND(sleep_total_seconds  / 3600.0, 2) AS sleep_total_h,
            ROUND(sleep_deep_seconds   / 3600.0, 2) AS sleep_deep_h,
            ROUND(sleep_light_seconds  / 3600.0, 2) AS sleep_light_h,
            ROUND(sleep_rem_seconds    / 3600.0, 2) AS sleep_rem_h,
            sleep_score,
            sleep_avg_spo2,
            sleep_avg_respiration,
            spo2_avg,
            respiration_avg,
            weight_kg,
            ROUND(weight_kg * 2.20462, 2)           AS weight_lbs,
            bmi,
            body_fat_pct,
            moderate_intensity_mins,
            vigorous_intensity_mins,
            hydration_intake_ml
        FROM daily
        ORDER BY date ASC
    """).fetchall()
    return [dict(r) for r in rows]


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Andy's Garmin Dashboard</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2a2d3a;
    --accent:  #00b4d8;   /* Garmin blue */
    --accent2: #0096c7;
    --text:    #e8eaf0;
    --muted:   #8b90a0;
    --good:    #43aa8b;
    --warn:    #f8961e;
    --bad:     #e63946;
    --radius:  8px;
    --font:    'Inter', system-ui, sans-serif;
  }}
  body {{ font-family: var(--font); background: var(--bg); color: var(--text);
          font-size: 14px; line-height: 1.5; }}
  a {{ color: var(--accent); text-decoration: none; }}

  header {{ background: var(--surface); border-bottom: 1px solid var(--border);
            padding: 12px 24px; display: flex; align-items: center; gap: 16px; }}
  header h1 {{ font-size: 1.2rem; font-weight: 700; color: var(--accent); }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px 16px; }}

  /* ── filters ── */
  .filters {{ background: var(--surface); border: 1px solid var(--border);
              border-radius: var(--radius); padding: 16px; margin-bottom: 20px; }}
  .filters h2 {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .08em;
                 color: var(--muted); margin-bottom: 12px; }}
  .filter-row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }}
  .filter-group {{ display: flex; flex-direction: column; gap: 4px; }}
  .filter-group label {{ font-size: 0.75rem; color: var(--muted); }}
  select, input[type=date] {{
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 10px; font-size: 0.85rem; min-width: 130px; cursor: pointer;
  }}
  select:focus, input:focus {{ outline: 2px solid var(--accent); border-color: var(--accent); }}
  .btn {{ background: var(--accent); color: #fff; border: none; border-radius: 6px;
          padding: 7px 16px; cursor: pointer; font-size: 0.85rem; font-weight: 600; }}
  .btn:hover {{ background: var(--accent2); }}
  .btn.secondary {{ background: var(--surface); border: 1px solid var(--border); color: var(--text); }}
  .btn.secondary:hover {{ border-color: var(--accent); color: var(--accent); }}

  /* ── cards ── */
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
            gap: 12px; margin-bottom: 20px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border);
           border-radius: var(--radius); padding: 14px 16px; }}
  .card .label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: .06em;
                  color: var(--muted); margin-bottom: 4px; }}
  .card .value {{ font-size: 1.4rem; font-weight: 700; }}
  .card .sub   {{ font-size: 0.75rem; color: var(--muted); margin-top: 2px; }}

  /* ── tabs ── */
  .tabs {{ display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--border);
           flex-wrap: wrap; }}
  .tab {{ padding: 8px 16px; cursor: pointer; border-radius: 6px 6px 0 0;
          font-size: 0.85rem; color: var(--muted); border: 1px solid transparent;
          border-bottom: none; margin-bottom: -1px; }}
  .tab.active {{ background: var(--surface); border-color: var(--border);
                 color: var(--text); font-weight: 600; }}
  .tab:hover:not(.active) {{ color: var(--text); }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}

  /* ── tables ── */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  thead th {{ background: var(--surface); color: var(--muted); font-weight: 600;
              text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
              white-space: nowrap; cursor: pointer; user-select: none; }}
  thead th:hover {{ color: var(--text); }}
  thead th.sorted-asc::after  {{ content: ' ↑'; color: var(--accent); }}
  thead th.sorted-desc::after {{ content: ' ↓'; color: var(--accent); }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:hover {{ background: var(--surface); }}
  tbody td {{ padding: 7px 10px; white-space: nowrap; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

  /* ── charts ── */
  .chart-wrap {{ background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 16px; margin-bottom: 20px;
                 overflow-x: auto; }}
  .chart-wrap h3 {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: .06em;
                    color: var(--muted); margin-bottom: 12px; }}
  .bar-chart {{ display: flex; align-items: flex-end; gap: 2px;
                height: 140px; padding-bottom: 24px; position: relative; }}
  .bar-col {{ display: flex; flex-direction: column; align-items: center;
              flex: 1; min-width: 18px; max-width: 60px; height: 100%;
              justify-content: flex-end; position: relative; }}
  .bar {{ width: 100%; border-radius: 3px 3px 0 0; background: var(--accent);
          transition: opacity .15s; cursor: default; min-height: 2px; }}
  .bar:hover {{ opacity: 0.75; }}
  .bar-label {{ position: absolute; bottom: -20px; font-size: 0.6rem; color: var(--muted);
                white-space: nowrap; text-align: center; width: 100%; }}
  .bar-val {{ position: absolute; top: -16px; font-size: 0.6rem; color: var(--muted);
              text-align: center; width: 100%; white-space: nowrap; }}

  /* ── sparkline strip ── */
  .strip {{ display: flex; gap: 1px; align-items: flex-end; height: 40px;
            margin-bottom: 4px; }}
  .strip-bar {{ flex: 1; border-radius: 2px 2px 0 0; background: var(--accent);
                min-height: 2px; opacity: 0.7; }}

  /* ── hrv status badge ── */
  .hrv-BALANCED {{ color: var(--good); }}
  .hrv-UNBALANCED {{ color: var(--warn); }}
  .hrv-LOW {{ color: var(--bad); }}
  .hrv-POOR {{ color: var(--bad); }}

  /* ── pagination ── */
  .pagination {{ display: flex; gap: 6px; align-items: center; margin-top: 12px; flex-wrap: wrap; }}
  .pagination button {{ background: var(--surface); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 0.8rem; }}
  .pagination button:hover, .pagination button.active {{ border-color: var(--accent); color: var(--accent); }}
  .pagination .info {{ color: var(--muted); font-size: 0.8rem; }}

  .empty {{ padding: 40px; text-align: center; color: var(--muted); }}
  @media (max-width: 600px) {{
    .filter-row {{ flex-direction: column; }}
    select, input {{ min-width: 100%; }}
  }}
</style>
</head>
<body>

<header>
  <div>
    <h1>⌚ Garmin Dashboard</h1>
    <div class="subtitle" id="header-sub">Loading…</div>
  </div>
</header>

<div class="container">

  <!-- FILTERS -->
  <div class="filters">
    <h2>Date range</h2>
    <div class="filter-row">
      <div class="filter-group">
        <label>From</label>
        <input type="date" id="f-from">
      </div>
      <div class="filter-group">
        <label>To</label>
        <input type="date" id="f-to">
      </div>
      <div class="filter-group">
        <label>Quick range</label>
        <select id="f-quick">
          <option value="">Custom</option>
          <option value="30">Last 30 days</option>
          <option value="90">Last 90 days</option>
          <option value="365">Last 365 days</option>
          <option value="0">All time</option>
        </select>
      </div>
      <div class="filter-group" style="justify-content:flex-end">
        <button class="btn secondary" id="btn-reset">Reset</button>
      </div>
    </div>
  </div>

  <!-- SUMMARY CARDS -->
  <div class="cards" id="cards"></div>

  <!-- TABS -->
  <div class="tabs">
    <div class="tab active" data-tab="steps">Steps</div>
    <div class="tab" data-tab="heart">Heart Rate</div>
    <div class="tab" data-tab="sleep">Sleep</div>
    <div class="tab" data-tab="body">Body</div>
    <div class="tab" data-tab="stress">Stress & Battery</div>
    <div class="tab" data-tab="daily">Daily Log</div>
  </div>

  <!-- STEPS TAB -->
  <div class="panel active" id="panel-steps">
    <div class="chart-wrap">
      <h3>Weekly steps — last 52 weeks</h3>
      <div class="bar-chart" id="steps-chart"></div>
    </div>
    <div class="chart-wrap">
      <h3>Monthly step totals</h3>
      <div class="bar-chart" id="monthly-chart"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Month</th>
          <th class="num">Days w/ data</th>
          <th class="num">Avg steps/day</th>
          <th class="num">Total steps</th>
          <th class="num">Avg distance</th>
          <th class="num">Avg active kcal</th>
        </tr></thead>
        <tbody id="steps-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- HEART RATE TAB -->
  <div class="panel" id="panel-heart">
    <div class="chart-wrap">
      <h3>Resting heart rate — daily</h3>
      <div class="bar-chart" id="rhr-chart"></div>
    </div>
    <div class="chart-wrap">
      <h3>HRV (last night avg, ms)</h3>
      <div class="bar-chart" id="hrv-chart"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Month</th>
          <th class="num">Avg RHR</th>
          <th class="num">Min RHR</th>
          <th class="num">Avg HRV (ms)</th>
          <th class="num">Avg max HR</th>
        </tr></thead>
        <tbody id="hr-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- SLEEP TAB -->
  <div class="panel" id="panel-sleep">
    <div class="chart-wrap">
      <h3>Sleep duration — daily (hours)</h3>
      <div class="bar-chart" id="sleep-chart"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Month</th>
          <th class="num">Nights</th>
          <th class="num">Avg total (h)</th>
          <th class="num">Avg deep (h)</th>
          <th class="num">Avg REM (h)</th>
          <th class="num">Avg score</th>
          <th class="num">Avg SpO2 %</th>
        </tr></thead>
        <tbody id="sleep-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- BODY TAB -->
  <div class="panel" id="panel-body">
    <div class="chart-wrap">
      <h3>Weight (lbs)</h3>
      <div class="bar-chart" id="weight-chart"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Date</th>
          <th class="num">Weight (lbs)</th>
          <th class="num">Weight (kg)</th>
          <th class="num">BMI</th>
          <th class="num">Body fat %</th>
        </tr></thead>
        <tbody id="body-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- STRESS & BATTERY TAB -->
  <div class="panel" id="panel-stress">
    <div class="chart-wrap">
      <h3>Average stress level — daily</h3>
      <div class="bar-chart" id="stress-chart"></div>
    </div>
    <div class="chart-wrap">
      <h3>Body battery — daily high/low</h3>
      <div class="bar-chart" id="battery-chart"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Month</th>
          <th class="num">Avg stress</th>
          <th class="num">Avg battery high</th>
          <th class="num">Avg battery low</th>
          <th class="num">Avg moderate mins</th>
          <th class="num">Avg vigorous mins</th>
        </tr></thead>
        <tbody id="stress-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- DAILY LOG TAB -->
  <div class="panel" id="panel-daily">
    <div class="table-wrap">
      <table id="daily-table">
        <thead><tr>
          <th data-col="date">Date</th>
          <th data-col="total_steps" class="num">Steps</th>
          <th data-col="distance_mi" class="num">Miles</th>
          <th data-col="resting_heart_rate" class="num">RHR</th>
          <th data-col="avg_heart_rate" class="num">Avg HR</th>
          <th data-col="hrv_last_night_avg" class="num">HRV (ms)</th>
          <th data-col="sleep_total_h" class="num">Sleep (h)</th>
          <th data-col="sleep_score" class="num">Sleep score</th>
          <th data-col="weight_lbs" class="num">Weight (lbs)</th>
          <th data-col="avg_stress_level" class="num">Stress</th>
          <th data-col="body_battery_highest" class="num">Battery ↑</th>
          <th data-col="active_kilocalories" class="num">Active kcal</th>
        </tr></thead>
        <tbody id="daily-tbody"></tbody>
      </table>
    </div>
    <div class="pagination" id="daily-pagination"></div>
  </div>

</div>

<script>
// ── DATA ──────────────────────────────────────────────────────────────────
{data_js}

// ── HELPERS ───────────────────────────────────────────────────────────────
const fmt1   = v => v == null ? '—' : (+v).toFixed(1);
const fmt0   = v => v == null ? '—' : Math.round(+v).toLocaleString();
const fmtPct = v => v == null ? '—' : (+v).toFixed(1) + '%';

// ── STATE ─────────────────────────────────────────────────────────────────
let filtered = [...ALL_DAYS];
let sortCol = 'date', sortDir = -1;
let page = 1;
const PAGE_SIZE = 60;

// ── FILTER ────────────────────────────────────────────────────────────────
const fromInput  = document.getElementById('f-from');
const toInput    = document.getElementById('f-to');
const quickSel   = document.getElementById('f-quick');

function applyFilters() {{
  const from = fromInput.value;
  const to   = toInput.value;
  filtered = ALL_DAYS.filter(d => {{
    if (from && d.date < from) return false;
    if (to   && d.date > to)   return false;
    return true;
  }});
  page = 1;
  render();
}}

quickSel.addEventListener('change', () => {{
  const v = quickSel.value;
  if (v === '') return;
  const today = new Date().toISOString().slice(0,10);
  if (v === '0') {{
    fromInput.value = '';
    toInput.value   = '';
  }} else {{
    const d = new Date();
    d.setDate(d.getDate() - parseInt(v));
    fromInput.value = d.toISOString().slice(0,10);
    toInput.value   = today;
  }}
  applyFilters();
}});

[fromInput, toInput].forEach(el => el.addEventListener('change', applyFilters));
document.getElementById('btn-reset').addEventListener('click', () => {{
  fromInput.value = ''; toInput.value = ''; quickSel.value = '';
  filtered = [...ALL_DAYS]; page = 1; render();
}});

// ── RENDER ────────────────────────────────────────────────────────────────
function render() {{
  renderCards();
  renderSteps();
  renderHeart();
  renderSleep();
  renderBody();
  renderStress();
  renderDailyTable();
}}

// ── CARDS ─────────────────────────────────────────────────────────────────
function renderCards() {{
  const withSteps  = filtered.filter(d => d.total_steps != null);
  const withRHR    = filtered.filter(d => d.resting_heart_rate != null);
  const withSleep  = filtered.filter(d => d.sleep_total_h != null);
  const withWeight = filtered.filter(d => d.weight_lbs != null);
  const withHRV    = filtered.filter(d => d.hrv_last_night_avg != null);

  const avg = (arr, key) => arr.length ? arr.reduce((s,d)=>s+(+d[key]||0),0)/arr.length : null;
  const latest = (arr, key) => {{ const r = [...arr].reverse().find(d=>d[key]!=null); return r?.[key]??null; }};

  const avgSteps   = avg(withSteps,  'total_steps');
  const avgRHR     = avg(withRHR,    'resting_heart_rate');
  const avgSleep   = avg(withSleep,  'sleep_total_h');
  const avgHRV     = avg(withHRV,    'hrv_last_night_avg');
  const lastWeight = latest(withWeight, 'weight_lbs');
  const avgStress  = avg(filtered.filter(d=>d.avg_stress_level!=null), 'avg_stress_level');
  const dates      = filtered.map(d=>d.date).filter(Boolean).sort();
  const span       = dates.length ? dates[0] + ' → ' + dates[dates.length-1] : '—';

  document.getElementById('header-sub').textContent =
    filtered.length.toLocaleString() + ' days · ' + span;

  const defs = [
    ['Avg daily steps',  avgSteps  ? fmt0(avgSteps)          : '—', withSteps.length + ' days'],
    ['Avg resting HR',   avgRHR    ? fmt0(avgRHR) + ' bpm'   : '—', withRHR.length + ' days'],
    ['Avg sleep',        avgSleep  ? fmt1(avgSleep) + ' h'   : '—', withSleep.length + ' nights'],
    ['Avg HRV',          avgHRV    ? fmt0(avgHRV) + ' ms'    : '—', withHRV.length + ' nights'],
    ['Latest weight',    lastWeight ? fmt1(lastWeight) + ' lbs' : '—', withWeight.length + ' weigh-ins'],
    ['Avg stress',       avgStress ? fmt0(avgStress)          : '—', '0–100 scale'],
  ];
  document.getElementById('cards').innerHTML = defs.map(([lbl,val,sub]) =>
    `<div class="card"><div class="label">${{lbl}}</div>
     <div class="value">${{val}}</div>
     <div class="sub">${{sub}}</div></div>`
  ).join('');
}}

// ── STEPS ─────────────────────────────────────────────────────────────────
function groupByMonth(arr, keys) {{
  const m = {{}};
  arr.forEach(d => {{
    const mo = d.date.slice(0,7);
    if (!m[mo]) {{ m[mo] = {{_n:0}}; keys.forEach(k => m[mo][k] = []); }}
    m[mo]._n++;
    keys.forEach(k => {{ if (d[k] != null) m[mo][k].push(+d[k]); }});
  }});
  return m;
}}

function barChart(elId, data, color='var(--accent)') {{
  // data = array of {{label, value, title}}
  const max = Math.max(...data.map(d=>d.value||0), 1);
  document.getElementById(elId).innerHTML = data.map(d => {{
    const pct = ((d.value||0) / max * 100).toFixed(1);
    return `<div class="bar-col" title="${{d.title||d.label}}">
      <div class="bar" style="height:${{pct}}%;background:${{color}}"></div>
      <div class="bar-label">${{d.label}}</div>
    </div>`;
  }}).join('');
}}

function renderSteps() {{
  // Weekly chart: last 52 weeks
  const weeks = {{}};
  filtered.forEach(d => {{
    if (d.total_steps == null) return;
    const dt = new Date(d.date + 'T12:00:00');
    const jan4 = new Date(dt.getFullYear(), 0, 4);
    const wn = Math.ceil(((dt - jan4) / 86400000 + jan4.getDay() + 1) / 7);
    const yr = wn === 0 ? dt.getFullYear()-1 : (wn>52&&dt.getMonth()===0?dt.getFullYear()-1:dt.getFullYear());
    const key = yr + '-W' + String(wn).padStart(2,'0');
    weeks[key] = (weeks[key]||0) + (+d.total_steps);
  }});
  const wkeys = Object.keys(weeks).sort().slice(-52);
  barChart('steps-chart', wkeys.map(k => ({{
    label: k.slice(5), value: weeks[k],
    title: k + ': ' + weeks[k].toLocaleString() + ' steps'
  }})));

  // Monthly chart
  const mo = groupByMonth(filtered, ['total_steps','distance_mi','active_kilocalories']);
  const mokeys = Object.keys(mo).sort();
  barChart('monthly-chart', mokeys.map(k => {{
    const tot = mo[k].total_steps.reduce((s,v)=>s+v,0);
    return {{ label: k.slice(5), value: tot, title: k+': '+tot.toLocaleString() }};
  }}));

  // Table
  document.getElementById('steps-tbody').innerHTML = [...mokeys].reverse().map(k => {{
    const s = mo[k].total_steps, dist = mo[k].distance_mi, cal = mo[k].active_kilocalories;
    const avg = s.length ? s.reduce((a,v)=>a+v,0)/s.length : null;
    const tot = s.reduce((a,v)=>a+v,0);
    const avgDist = dist.length ? dist.reduce((a,v)=>a+v,0)/dist.length : null;
    const avgCal  = cal.length  ? cal.reduce((a,v)=>a+v,0)/cal.length   : null;
    return `<tr>
      <td>${{k}}</td>
      <td class="num">${{s.length}}</td>
      <td class="num">${{avg?fmt0(avg):'—'}}</td>
      <td class="num">${{tot?tot.toLocaleString():'—'}}</td>
      <td class="num">${{avgDist?fmt1(avgDist)+' mi':'—'}}</td>
      <td class="num">${{avgCal?fmt0(avgCal):'—'}}</td>
    </tr>`;
  }}).join('');
}}

// ── HEART RATE ────────────────────────────────────────────────────────────
function renderHeart() {{
  // Daily RHR chart (last 180 days)
  const rhrDays = filtered.filter(d=>d.resting_heart_rate!=null).slice(-180);
  barChart('rhr-chart', rhrDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.resting_heart_rate,
    title: d.date + ': ' + d.resting_heart_rate + ' bpm'
  }})), 'var(--bad)');

  // HRV chart (last 180 days)
  const hrvDays = filtered.filter(d=>d.hrv_last_night_avg!=null).slice(-180);
  barChart('hrv-chart', hrvDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.hrv_last_night_avg,
    title: d.date + ': ' + (+d.hrv_last_night_avg).toFixed(0) + ' ms' + (d.hrv_status?' ('+d.hrv_status+')'  :'')
  }})), 'var(--good)');

  // Monthly table
  const mo = groupByMonth(filtered, ['resting_heart_rate','hrv_last_night_avg','max_heart_rate']);
  const mokeys = Object.keys(mo).sort();
  document.getElementById('hr-tbody').innerHTML = [...mokeys].reverse().map(k => {{
    const rhr = mo[k].resting_heart_rate;
    const hrv = mo[k].hrv_last_night_avg;
    const mhr = mo[k].max_heart_rate;
    const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
    const mn = (arr) => arr.length ? Math.min(...arr) : null;
    return `<tr>
      <td>${{k}}</td>
      <td class="num">${{a(rhr)?fmt1(a(rhr)):'—'}}</td>
      <td class="num">${{mn(rhr)?fmt0(mn(rhr)):'—'}}</td>
      <td class="num">${{a(hrv)?fmt1(a(hrv)):'—'}}</td>
      <td class="num">${{a(mhr)?fmt0(a(mhr)):'—'}}</td>
    </tr>`;
  }}).join('');
}}

// ── SLEEP ─────────────────────────────────────────────────────────────────
function renderSleep() {{
  const sleepDays = filtered.filter(d=>d.sleep_total_h!=null).slice(-120);
  barChart('sleep-chart', sleepDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.sleep_total_h,
    title: d.date + ': ' + (+d.sleep_total_h).toFixed(1) + 'h' +
           (d.sleep_score ? ' · score ' + d.sleep_score : '')
  }})), '#7b2d8b');

  const mo = groupByMonth(filtered, ['sleep_total_h','sleep_deep_h','sleep_rem_h','sleep_score','sleep_avg_spo2']);
  const mokeys = Object.keys(mo).sort();
  document.getElementById('sleep-tbody').innerHTML = [...mokeys].reverse().map(k => {{
    const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
    return `<tr>
      <td>${{k}}</td>
      <td class="num">${{mo[k].sleep_total_h.length}}</td>
      <td class="num">${{a(mo[k].sleep_total_h)?fmt1(a(mo[k].sleep_total_h)):'—'}}</td>
      <td class="num">${{a(mo[k].sleep_deep_h)?fmt1(a(mo[k].sleep_deep_h)):'—'}}</td>
      <td class="num">${{a(mo[k].sleep_rem_h)?fmt1(a(mo[k].sleep_rem_h)):'—'}}</td>
      <td class="num">${{a(mo[k].sleep_score)?fmt1(a(mo[k].sleep_score)):'—'}}</td>
      <td class="num">${{a(mo[k].sleep_avg_spo2)?fmtPct(a(mo[k].sleep_avg_spo2)):'—'}}</td>
    </tr>`;
  }}).join('');
}}

// ── BODY ──────────────────────────────────────────────────────────────────
function renderBody() {{
  const weightDays = filtered.filter(d=>d.weight_lbs!=null);
  barChart('weight-chart', weightDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.weight_lbs,
    title: d.date + ': ' + (+d.weight_lbs).toFixed(1) + ' lbs' +
           (d.bmi ? ' · BMI ' + (+d.bmi).toFixed(1) : '')
  }})), 'var(--warn)');

  document.getElementById('body-tbody').innerHTML = [...weightDays].reverse().map(d =>
    `<tr>
      <td>${{d.date}}</td>
      <td class="num">${{fmt1(d.weight_lbs)}}</td>
      <td class="num">${{fmt1(d.weight_kg)}}</td>
      <td class="num">${{d.bmi?fmt1(d.bmi):'—'}}</td>
      <td class="num">${{d.body_fat_pct?fmtPct(d.body_fat_pct):'—'}}</td>
    </tr>`
  ).join('');
}}

// ── STRESS & BATTERY ──────────────────────────────────────────────────────
function renderStress() {{
  const stressDays = filtered.filter(d=>d.avg_stress_level!=null).slice(-120);
  barChart('stress-chart', stressDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.avg_stress_level,
    title: d.date + ': stress ' + d.avg_stress_level
  }})), 'var(--warn)');

  const battDays = filtered.filter(d=>d.body_battery_highest!=null).slice(-120);
  barChart('battery-chart', battDays.map(d => ({{
    label: d.date.slice(5),
    value: +d.body_battery_highest,
    title: d.date + ': high=' + d.body_battery_highest + ' low=' + d.body_battery_lowest
  }})), 'var(--good)');

  const mo = groupByMonth(filtered, ['avg_stress_level','body_battery_highest','body_battery_lowest',
                                      'moderate_intensity_mins','vigorous_intensity_mins']);
  const mokeys = Object.keys(mo).sort();
  const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
  document.getElementById('stress-tbody').innerHTML = [...mokeys].reverse().map(k => `<tr>
    <td>${{k}}</td>
    <td class="num">${{a(mo[k].avg_stress_level)?fmt1(a(mo[k].avg_stress_level)):'—'}}</td>
    <td class="num">${{a(mo[k].body_battery_highest)?fmt0(a(mo[k].body_battery_highest)):'—'}}</td>
    <td class="num">${{a(mo[k].body_battery_lowest)?fmt0(a(mo[k].body_battery_lowest)):'—'}}</td>
    <td class="num">${{a(mo[k].moderate_intensity_mins)?fmt0(a(mo[k].moderate_intensity_mins)):'—'}}</td>
    <td class="num">${{a(mo[k].vigorous_intensity_mins)?fmt0(a(mo[k].vigorous_intensity_mins)):'—'}}</td>
  </tr>`).join('');
}}

// ── DAILY TABLE ───────────────────────────────────────────────────────────
function renderDailyTable() {{
  const mul = sortDir;
  const sorted = [...filtered].sort((a,b) => {{
    const av = a[sortCol], bv = b[sortCol];
    if (av==null && bv==null) return 0;
    if (av==null) return 1; if (bv==null) return -1;
    return av < bv ? -mul : av > bv ? mul : 0;
  }});
  const total = sorted.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  page = Math.min(page, pages);
  const slice = sorted.slice((page-1)*PAGE_SIZE, page*PAGE_SIZE);

  document.querySelectorAll('#daily-table thead th[data-col]').forEach(th => {{
    th.classList.remove('sorted-asc','sorted-desc');
    if (th.dataset.col === sortCol)
      th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  }});

  document.getElementById('daily-tbody').innerHTML = slice.map(d => `<tr>
    <td>${{d.date}}</td>
    <td class="num">${{d.total_steps!=null?fmt0(d.total_steps):'—'}}</td>
    <td class="num">${{d.distance_mi!=null?fmt1(d.distance_mi):'—'}}</td>
    <td class="num">${{d.resting_heart_rate??'—'}}</td>
    <td class="num">${{d.avg_heart_rate??'—'}}</td>
    <td class="num">${{d.hrv_last_night_avg!=null?fmt0(d.hrv_last_night_avg):'—'}}</td>
    <td class="num">${{d.sleep_total_h!=null?fmt1(d.sleep_total_h):'—'}}</td>
    <td class="num">${{d.sleep_score!=null?fmt1(d.sleep_score):'—'}}</td>
    <td class="num">${{d.weight_lbs!=null?fmt1(d.weight_lbs):'—'}}</td>
    <td class="num">${{d.avg_stress_level??'—'}}</td>
    <td class="num">${{d.body_battery_highest??'—'}}</td>
    <td class="num">${{d.active_kilocalories!=null?fmt0(d.active_kilocalories):'—'}}</td>
  </tr>`).join('');

  const pg = document.getElementById('daily-pagination');
  if (pages <= 1) {{ pg.innerHTML=''; return; }}
  const btns = [`<span class="info">Page ${{page}} of ${{pages}} (${{total.toLocaleString()}} days)</span>`];
  if (page > 1) btns.push(`<button onclick="goPage(${{page-1}})">‹ Prev</button>`);
  const lo = Math.max(1,page-3), hi = Math.min(pages,page+3);
  for (let p=lo;p<=hi;p++) btns.push(`<button class="${{p===page?'active':''}}" onclick="goPage(${{p}})">${{p}}</button>`);
  if (page < pages) btns.push(`<button onclick="goPage(${{page+1}})">Next ›</button>`);
  pg.innerHTML = btns.join('');
}}

window.goPage = p => {{ page = p; renderDailyTable(); window.scrollTo(0,0); }};

document.querySelectorAll('#daily-table thead th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    if (sortCol === th.dataset.col) sortDir *= -1;
    else {{ sortCol = th.dataset.col; sortDir = -1; }}
    page = 1; renderDailyTable();
  }});
}});

// ── TABS ──────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-'+tab.dataset.tab).classList.add('active');
  }});
}});

// ── BOOT ──────────────────────────────────────────────────────────────────
render();
</script>
</body>
</html>
"""


def build(db_path: str, out_path: str) -> None:
    print(f"Loading data from {db_path} …")
    days = load_data(db_path)

    data_js = f"const ALL_DAYS = {json.dumps(days, separators=(',', ':'))};\n"

    html = HTML_TEMPLATE.format(data_js=data_js)
    Path(out_path).write_text(html, encoding="utf-8")
    size = Path(out_path).stat().st_size
    print(f"Written {out_path} ({size/1024:.0f} KB, {len(days)} days)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate index.html from garmin.db")
    ap.add_argument("--db",  default="garmin.db",  help="SQLite DB path")
    ap.add_argument("--out", default="index.html", help="Output HTML path")
    args = ap.parse_args()
    build(args.db, args.out)


if __name__ == "__main__":
    main()
