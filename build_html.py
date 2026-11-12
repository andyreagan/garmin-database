#!/usr/bin/env python3
"""
build_html.py  –  Generate index.html from garmin.db.

Usage:
    uv run python build_html.py           # reads garmin.db, writes index.html
    uv run python build_html.py --db PATH --out PATH
"""

import argparse
import calendar
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
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


# ---------------------------------------------------------------------------
# Long-term trend computation (all run at build time, in Python)
# ---------------------------------------------------------------------------

RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
             "indoor_running", "street_running")
BIKE_TYPES = ("cycling", "road_biking", "mountain_biking", "gravel_cycling",
              "cyclocross", "indoor_cycling", "virtual_ride", "track_cycling",
              "bmx", "recumbent_cycling", "e_bike_fitness", "e_bike_mountain")
# Whoop-style consistency: agreement vs each of the previous 4 days, recent
# days weighted more.
CONS_WEIGHTS = [0.4, 0.3, 0.2, 0.1]


def _month_range(m_lo, m_hi):
    y, m = map(int, m_lo.split("-"))
    ey, em = map(int, m_hi.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _month_avg(days, key, min_n=5):
    """{month: (mean, n)} over days where key is present; months with < min_n kept but flagged via n."""
    buckets = defaultdict(list)
    for d in days:
        v = d.get(key)
        if v is not None:
            buckets[d["date"][:7]].append(float(v))
    return {m: (sum(v) / len(v), len(v)) for m, v in buckets.items()}


def _rolling(months, vc, weighted=True, min_pts=3):
    """Trailing 12-month rolling mean of {month:(val,n)}; weighted by n when asked."""
    out = []
    for i in range(len(months)):
        num = den = 0.0
        pts = 0
        for j in range(max(0, i - 11), i + 1):
            hit = vc.get(months[j])
            if hit and hit[0] is not None:
                w = hit[1] if weighted else 1
                num += hit[0] * w
                den += w
                pts += 1
        out.append(round(num / den, 3) if den and pts >= min_pts else None)
    return out


def _series(months, vc, min_n=1):
    """Return the monthly point list (None where n < min_n), aligned to months."""
    return [vc[m][0] if (m in vc and vc[m][1] >= min_n) else None for m in months]


def load_activities_monthly(con):
    run = defaultdict(float)
    bike = defaultdict(float)
    try:
        rows = con.execute(
            "SELECT substr(start_time_local,1,7) m, activity_type t, "
            "COALESCE(distance_m,0) d FROM activities").fetchall()
    except sqlite3.OperationalError:
        return {}, {}                       # activities table not present
    for m, t, d in rows:
        if not m:
            continue
        mi = d / 1609.344
        if t in RUN_TYPES:
            run[m] += mi
        elif t in BIKE_TYPES:
            bike[m] += mi
    return run, bike


def _weekly_rate(total_by_month):
    """Convert {month: total_miles} to {month: (miles_per_week, 1)}."""
    out = {}
    for m, tot in total_by_month.items():
        y, mo = map(int, m.split("-"))
        weeks = calendar.monthrange(y, mo)[1] / 7.0
        out[m] = (tot / weeks, 1)
    return out


def _parse_dt(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "").replace(".0", ""))
    except ValueError:
        return None


def compute_regularity(con):
    """Monthly SRI (consecutive-day) and Whoop-style 4-day consistency.

    Uses sleep-stage segments where available (awake episodes count as awake);
    older nights fall back to the solid main sleep window. Returns two
    {month: (value, n_pairs)} dicts.
    """
    minutes = defaultdict(lambda: bytearray(1440))   # 1 = asleep, per clock minute
    tracked = set()
    seg_dates = set()

    def mark(start, end):
        t = start.replace(second=0, microsecond=0)
        while t < end:
            minutes[t.date().isoformat()][t.hour * 60 + t.minute] = 1
            t += timedelta(minutes=1)

    for ds, s, e, stage in con.execute(
            "SELECT date,start_time,end_time,stage FROM sleep_segments "
            "WHERE start_time IS NOT NULL AND end_time IS NOT NULL"):
        seg_dates.add(ds)
        if stage == "awake":
            continue
        st, en = _parse_dt(s), _parse_dt(e)
        if not st or not en or en <= st:
            continue
        tracked.add(ds)
        mark(st, en)

    for ds, s, e in con.execute(
            "SELECT date,sleep_start,sleep_end FROM daily "
            "WHERE sleep_start IS NOT NULL AND sleep_end IS NOT NULL "
            "AND sleep_total_seconds > 0"):
        if ds in seg_dates:
            continue
        st, en = _parse_dt(s), _parse_dt(e)
        if not st or not en or en <= st or (en - st) > timedelta(hours=20):
            continue
        tracked.add(ds)
        mark(st, en)

    def agreement(a, b):
        return sum(1 for i in range(1440) if a[i] == b[i]) / 1440

    sri = defaultdict(list)
    cons = defaultdict(list)
    for ds in sorted(tracked):
        d = datetime.fromisoformat(ds).date()
        lags = []
        for k in range(1, 5):
            prev = (d - timedelta(days=k)).isoformat()
            if prev in tracked:
                lags.append((k, -100 + 200 * agreement(minutes[ds], minutes[prev])))
        if lags and lags[0][0] == 1:
            sri[ds[:7]].append(lags[0][1])
        if len(lags) >= 3:
            wsum = sum(CONS_WEIGHTS[k - 1] for k, _ in lags)
            cons[ds[:7]].append(
                sum(CONS_WEIGHTS[k - 1] * v for k, v in lags) / wsum)

    sri_m = {m: (sum(v) / len(v), len(v)) for m, v in sri.items()}
    cons_m = {m: (sum(v) / len(v), len(v)) for m, v in cons.items()}
    return sri_m, cons_m


def build_trends(con, days):
    steps_m = _month_avg(days, "total_steps")
    sleep_m = _month_avg(days, "sleep_total_h")
    hrv_m = _month_avg(days, "hrv_last_night_avg")
    run_tot, bike_tot = load_activities_monthly(con)
    runwk_m = _weekly_rate(run_tot)
    bikewk_m = _weekly_rate(bike_tot)
    sri_m, cons_m = compute_regularity(con)

    allm = (set(steps_m) | set(sleep_m) | set(hrv_m) | set(runwk_m)
            | set(bikewk_m) | set(sri_m) | set(cons_m))
    if not allm:
        return {"months": []}
    months = _month_range(min(allm), max(allm))

    sleep_roll = _rolling(months, sleep_m)
    cons_roll = _rolling(months, cons_m, weighted=True)
    phase = [{"m": months[i], "x": sleep_roll[i], "y": cons_roll[i]}
             for i in range(len(months))
             if sleep_roll[i] is not None and cons_roll[i] is not None]

    return {
        "months": months,
        "steps": _series(months, steps_m),
        "steps_roll": _rolling(months, steps_m),
        "run_roll": _rolling(months, runwk_m, weighted=False),
        "bike_roll": _rolling(months, bikewk_m, weighted=False),
        "sleep": _series(months, sleep_m),
        "sleep_roll": sleep_roll,
        "hrv": _series(months, hrv_m),
        "hrv_roll": _rolling(months, hrv_m),
        "sri": _series(months, sri_m, min_n=5),
        "sri_roll": _rolling(months, sri_m),
        "cons_roll": cons_roll,
        "phase": phase,
    }


# Placeholders ({generated}, {data_js}, {trends_js}, {trends_css}, {trends_panel},
# {trends_script}) are substituted with str.replace in build(), so JS/CSS braces
# are written normally (no doubling).
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-site="garmin">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="ar-repo" content="garmin-database">
<meta name="ar-generated" content="{generated}">
<title>Andy's Garmin Dashboard</title>
<link rel="stylesheet" href="ar.css">
<style>
.top-controls { display: flex; gap: 8px 14px; align-items: flex-end; flex-wrap: wrap; }
.card-vals { display: flex; gap: 4px 18px; flex-wrap: wrap; align-items: baseline; }
.card-vals .lbl { font-size: 0.75rem; font-weight: 600; color: var(--muted); margin-left: 4px; }
.metric-card .legend { margin-bottom: 6px; }
{trends_css}
</style>
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
<script src="ar.js"></script>
</head>
<body>

<!-- SIDEBAR -->
<nav class="sidebar">
  <div class="sidebar-logo">Andy's <span>Garmin</span></div>

  <div class="nav-section">
    <div class="nav-section-title">Long-Term</div>
    <div class="nav-item active" data-section="trends">
      <span class="icon">📈</span> Long-term Trends
    </div>
  </div>

  <div class="nav-section">
    <div class="nav-section-title">Activity</div>
    <div class="nav-item" data-section="steps">
      <span class="icon">👟</span> Steps
    </div>
  </div>

  <div class="nav-section">
    <div class="nav-section-title">Health Markers</div>
    <div class="nav-item" data-section="heart">
      <span class="icon">❤️</span> Heart Rate
    </div>
    <div class="nav-item" data-section="sleep">
      <span class="icon">🌙</span> Sleep
    </div>
    <div class="nav-item" data-section="body">
      <span class="icon">⚖️</span> Body
    </div>
    <div class="nav-item" data-section="stress">
      <span class="icon">🔋</span> Stress &amp; Battery
    </div>
  </div>

  <div class="nav-section">
    <div class="nav-section-title">Log</div>
    <div class="nav-item" data-section="daily">
      <span class="icon">📋</span> Daily Log
    </div>
  </div>
</nav>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <div>
      <h1 id="section-title">Long-term Trends</h1>
      <div class="subtitle" id="header-sub">Loading…</div>
    </div>
    <div class="top-controls">
      <div class="field">
        <label for="f-from">From</label>
        <input type="date" id="f-from">
      </div>
      <div class="field">
        <label for="f-to">To</label>
        <input type="date" id="f-to">
      </div>
      <div class="range-btns" id="range-btns">
        <button class="range-btn" data-days="30">30d</button>
        <button class="range-btn" data-days="90">90d</button>
        <button class="range-btn" data-days="365">1yr</button>
        <button class="range-btn active" data-days="0">All</button>
      </div>
    </div>
  </div>

  <div class="content">

    <!-- SUMMARY CARDS (follow the date range, shown above every section) -->
    <div class="kpi-strip" id="cards"></div>

{trends_panel}

    <!-- ── STEPS ── -->
    <div class="section" id="section-steps">
      <div class="metric-grid wide">
        <div class="metric-card">
          <div class="metric-card-header">
            <div class="metric-card-title">Weekly steps — last 52 weeks</div>
          </div>
          <div class="chart-area" id="steps-chart"></div>
        </div>
        <div class="metric-card">
          <div class="metric-card-header">
            <div class="metric-card-title">Monthly step totals</div>
          </div>
          <div class="chart-area" id="monthly-chart"></div>
        </div>
      </div>
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">By month</div></div>
        <div class="table-wrap" style="margin-top:12px">
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
    </div>

    <!-- ── HEART RATE ── -->
    <div class="section" id="section-heart">
      <div class="metric-grid" id="heart-charts"></div>
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">By month</div></div>
        <div class="table-wrap" style="margin-top:12px">
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
    </div>

    <!-- ── SLEEP ── -->
    <div class="section" id="section-sleep">
      <div class="metric-grid wide" id="sleep-charts"></div>
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">By month</div></div>
        <div class="table-wrap" style="margin-top:12px">
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
    </div>

    <!-- ── BODY ── -->
    <div class="section" id="section-body">
      <div class="metric-grid wide" id="body-charts"></div>
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">Weigh-ins</div></div>
        <div class="table-wrap" style="margin-top:12px">
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
    </div>

    <!-- ── STRESS & BATTERY ── -->
    <div class="section" id="section-stress">
      <div class="metric-grid" id="stress-charts"></div>
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">By month</div></div>
        <div class="table-wrap" style="margin-top:12px">
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
    </div>

    <!-- ── DAILY LOG ── -->
    <div class="section" id="section-daily">
      <div class="metric-card">
        <div class="metric-card-header"><div class="metric-card-title">Daily Log</div></div>
        <div class="table-wrap" style="margin-top:12px">
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

  </div><!-- /content -->
</div><!-- /main -->

<script>
// ── DATA ──────────────────────────────────────────────────────────────────
{data_js}
{trends_js}

// ── HELPERS ───────────────────────────────────────────────────────────────
const fmt1   = v => v == null ? '—' : (+v).toFixed(1);
const fmt0   = v => v == null ? '—' : Math.round(+v).toLocaleString();
const fmtPct = v => v == null ? '—' : (+v).toFixed(1) + '%';
const mean   = a => a.length ? a.reduce((s, v) => s + v, 0) / a.length : null;

// trailing n-point mean, skipping nulls
function rolling(vals, n) {
  let sum = 0, cnt = 0;
  return vals.map((v, i) => {
    if (v != null) { sum += v; cnt++; }
    const old = vals[i - n];
    if (i >= n && old != null) { sum -= old; cnt--; }
    return cnt ? sum / cnt : null;
  });
}

// Garmin reports -1 / -2 for "not enough data" on stress; those are not readings.
ALL_DAYS.forEach(d => { if (d.avg_stress_level != null && d.avg_stress_level < 0) d.avg_stress_level = null; });

// ── STATE ─────────────────────────────────────────────────────────────────
let filtered = [...ALL_DAYS];
let sortCol = 'date', sortDir = -1;
let page = 1;
const PAGE_SIZE = 60;
const rollState = {};   // card id → rolling window (days)

// ── FILTER ────────────────────────────────────────────────────────────────
const fromInput = document.getElementById('f-from');
const toInput   = document.getElementById('f-to');
const rangeBtns = [...document.querySelectorAll('.range-btn')];
const markRange = days => rangeBtns.forEach(b =>
  b.classList.toggle('active', days != null && parseInt(b.dataset.days) === days));

function applyFilters() {
  const from = fromInput.value;
  const to   = toInput.value;
  filtered = ALL_DAYS.filter(d => {
    if (from && d.date < from) return false;
    if (to   && d.date > to)   return false;
    return true;
  });
  page = 1;
  render();
}

// quick ranges; "All" clears the dates (the old Reset button)
rangeBtns.forEach(b => b.addEventListener('click', () => {
  const days = parseInt(b.dataset.days);
  if (days === 0) {
    fromInput.value = '';
    toInput.value   = '';
  } else {
    const d = new Date();
    d.setDate(d.getDate() - days);
    fromInput.value = d.toISOString().slice(0, 10);
    toInput.value   = new Date().toISOString().slice(0, 10);
  }
  markRange(days);
  applyFilters();
}));

// typing a date = custom range: no quick button highlighted
[fromInput, toInput].forEach(el => el.addEventListener('change', () => {
  markRange(!fromInput.value && !toInput.value ? 0 : null);
  applyFilters();
}));

// ── NAV ───────────────────────────────────────────────────────────────────
const SECTION_TITLES = {
  trends: 'Long-term Trends', steps: 'Steps', heart: 'Heart Rate', sleep: 'Sleep',
  body: 'Body', stress: 'Stress & Battery', daily: 'Daily Log',
};
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const s = item.dataset.section;
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    item.classList.add('active');
    document.querySelectorAll('.section').forEach(p => p.classList.remove('active'));
    document.getElementById('section-' + s).classList.add('active');
    document.getElementById('section-title').textContent = SECTION_TITLES[s] || s;
  });
});

// ── RENDER ────────────────────────────────────────────────────────────────
function render() {
  renderCards();
  renderSteps();
  renderHeart();
  renderSleep();
  renderBody();
  renderStress();
  renderDailyTable();
}

// ── KPI STRIP ─────────────────────────────────────────────────────────────
function renderCards() {
  const withSteps  = filtered.filter(d => d.total_steps != null);
  const withRHR    = filtered.filter(d => d.resting_heart_rate != null);
  const withSleep  = filtered.filter(d => d.sleep_total_h != null);
  const withWeight = filtered.filter(d => d.weight_lbs != null);
  const withHRV    = filtered.filter(d => d.hrv_last_night_avg != null);

  const avg = (arr, key) => arr.length ? arr.reduce((s,d)=>s+(+d[key]||0),0)/arr.length : null;
  const latest = (arr, key) => { const r = [...arr].reverse().find(d=>d[key]!=null); return r?.[key]??null; };

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
    ['Avg daily steps',  avgSteps  ? fmt0(avgSteps)          : '—', withSteps.length + ' days',        'accent'],
    ['Avg resting HR',   avgRHR    ? fmt0(avgRHR) + ' bpm'   : '—', withRHR.length + ' days',          'red'],
    ['Avg sleep',        avgSleep  ? fmt1(avgSleep) + ' h'   : '—', withSleep.length + ' nights',      'purple'],
    ['Avg HRV',          avgHRV    ? fmt0(avgHRV) + ' ms'    : '—', withHRV.length + ' nights',        'teal'],
    ['Latest weight',    lastWeight ? fmt1(lastWeight) + ' lbs' : '—', withWeight.length + ' weigh-ins', 'orange'],
    ['Avg stress',       avgStress ? fmt0(avgStress)          : '—', '0–100 scale',                     'yellow'],
  ];
  document.getElementById('cards').innerHTML = defs.map(([lbl,val,sub,color]) =>
    `<div class="kpi-card" style="--kpi-color:var(--${color})"><div class="kpi-label">${lbl}</div>
     <div class="kpi-value">${val}</div>
     <div class="kpi-sub">${sub}</div></div>`
  ).join('');
}

// ── METRIC CARDS (daily time series → AR.lineChart) ───────────────────────
// series: [{field, name, color}]; sparse: plot only days that have a value
// (weigh-ins), connected directly; extra: (day) => extra tooltip text.
const CARDS = {
  rhr:     { title: 'Resting heart rate — daily', unit: ' bpm', decimals: 0,
             series: [{ field: 'resting_heart_rate', color: 'var(--red)' }] },
  hrv:     { title: 'HRV (last night avg, ms)', unit: ' ms', decimals: 0,
             series: [{ field: 'hrv_last_night_avg', color: 'var(--teal)' }],
             extra: d => d.hrv_status || '' },
  sleep:   { title: 'Sleep duration — daily (hours)', unit: ' h', decimals: 1,
             series: [{ field: 'sleep_total_h', color: 'var(--purple)' }],
             extra: d => d.sleep_score ? 'score ' + d.sleep_score : '' },
  weight:  { title: 'Weight (lbs)', unit: ' lbs', decimals: 1, sparse: true, rollWindows: null,
             series: [{ field: 'weight_lbs', color: 'var(--orange)' }],
             extra: d => d.bmi ? 'BMI ' + (+d.bmi).toFixed(1) : '' },
  stress:  { title: 'Average stress level — daily', unit: '', decimals: 0,
             series: [{ field: 'avg_stress_level', color: 'var(--yellow)' }] },
  battery: { title: 'Body battery — daily high/low', unit: '', decimals: 0,
             series: [{ field: 'body_battery_highest', name: 'High', color: 'var(--teal)' },
                      { field: 'body_battery_lowest',  name: 'Low',  color: 'var(--blue)' }] },
};
const ROLL_WINDOWS = [1, 7, 30, 90];
const num = (d, f) => d[f] == null ? null : +d[f];

function cardRows(o) {
  const has = d => o.series.some(s => d[s.field] != null);
  if (o.sparse) return filtered.filter(has);
  // trim to the span that has data, so a late-starting metric fills its chart
  let a = 0, b = filtered.length - 1;
  while (a <= b && !has(filtered[a])) a++;
  while (b >= a && !has(filtered[b])) b--;
  return filtered.slice(a, b + 1);
}

function drawCard(el) {
  const id = el.dataset.card, o = CARDS[id];
  const n = o.rollWindows === null ? 1 : rollState[id];
  const rows = cardRows(o);
  const multi = o.series.length > 1;
  const fmt = v => (+v).toFixed(n === 1 ? o.decimals : Math.max(1, o.decimals)) + o.unit;
  const vals = o.series.map(s => {
    const raw = rows.map(d => num(d, s.field));
    return n === 1 ? raw : rolling(raw, n);
  });
  // Under ~a week AR.lineChart's time axis repeats the same day label on
  // sub-day ticks, so hand it plain per-day labels (index axis) instead.
  const spanDays = rows.length > 1
    ? (AR.parseDate(rows[rows.length - 1].date) - AR.parseDate(rows[0].date)) / 864e5 : 0;
  const dayLbl = d3.timeFormat('%b %-d');
  AR.lineChart(el, {
    x: spanDays <= 8 ? rows.map(d => dayLbl(AR.parseDate(d.date))) : rows.map(d => d.date),
    series: o.series.map((s, k) => ({ name: s.name || '', values: vals[k], color: s.color, area: true,
                                      dots: o.sparse && rows.length <= 120 })),
    height: 170,
    yFmt: v => String(+(+v).toFixed(1)),
    tip: i => {
      const d = rows[i], x = o.extra ? o.extra(d) : '';
      return `<b>${AR.fmtDate(d.date)}</b>` + (n > 1 ? ` <span class="dim">${n}d avg</span>` : '') + '<br>' +
        o.series.map((s, k) => vals[k][i] == null ? null :
          (multi ? AR.swatch(s.color) + `<span class="dim">${s.name}</span> ` : '') + fmt(vals[k][i]))
          .filter(Boolean).join('<br>') +
        (x ? `<br><span class="dim">${x}</span>` : '');
    },
  });
}

function buildCard(id) {
  const o = CARDS[id];
  const windows = o.rollWindows === null ? [] : ROLL_WINDOWS;
  if (windows.length && !rollState[id]) rollState[id] = 7;
  const f = v => (+v).toFixed(o.decimals) + o.unit;
  const stats = o.series.map(s => {
    const v = filtered.map(d => num(d, s.field)).filter(x => x != null);
    return { s, v, cur: v.length ? v[v.length - 1] : null };
  });
  const multi = o.series.length > 1;
  const valHtml = stats.map(({ s, cur }) =>
    `<span style="color:${s.color}">${cur != null ? f(cur) : '—'}` +
    (multi ? `<span class="lbl">${s.name.toLowerCase()}</span>` : '') + `</span>`).join('');
  const subHtml = multi
    ? stats.map(({ s, v }) => v.length ? `avg ${s.name.toLowerCase()} ${(mean(v)).toFixed(o.decimals)}` : '').filter(Boolean).join(' · ') || 'No data'
    : (stats[0].v.length
        ? `avg ${f(mean(stats[0].v))} · min ${f(Math.min(...stats[0].v))} · max ${f(Math.max(...stats[0].v))}`
        : 'No data');
  const rollBtns = windows.map(w =>
    `<button class="roll-btn${rollState[id] === w ? ' active' : ''}" data-card="${id}" data-win="${w}">${w}d</button>`).join('');
  const legend = multi
    ? `<div class="legend">${o.series.map(s =>
        `<span class="legend-item">${AR.swatch(s.color)}${s.name}</span>`).join('')}</div>` : '';
  return `<div class="metric-card">
    <div class="metric-card-header">
      <div>
        <div class="metric-card-title">${o.title}</div>
        <div class="metric-card-val card-vals">${valHtml}</div>
        <div class="metric-card-sub">${subHtml}</div>
      </div>
      <div class="roll-btns">${rollBtns}</div>
    </div>
    ${legend}
    <div class="chart-area" data-card="${id}"></div>
  </div>`;
}

function renderCardGrid(gridId, ids) {
  const grid = document.getElementById(gridId);
  grid.innerHTML = ids.map(buildCard).join('');
  grid.querySelectorAll('.chart-area[data-card]').forEach(drawCard);
}

// rolling-window buttons: redraw just that card's chart
document.addEventListener('click', e => {
  const b = e.target.closest('.roll-btn[data-win]');
  if (!b) return;
  const win = parseInt(b.dataset.win), card = b.closest('.metric-card');
  rollState[b.dataset.card] = win;
  card.querySelectorAll('.roll-btn').forEach(rb =>
    rb.classList.toggle('active', parseInt(rb.dataset.win) === win));
  drawCard(card.querySelector('.chart-area'));
});

// ── STEPS ─────────────────────────────────────────────────────────────────
function groupByMonth(arr, keys) {
  const m = {};
  arr.forEach(d => {
    const mo = d.date.slice(0,7);
    if (!m[mo]) { m[mo] = {_n:0}; keys.forEach(k => m[mo][k] = []); }
    m[mo]._n++;
    keys.forEach(k => { if (d[k] != null) m[mo][k].push(+d[k]); });
  });
  return m;
}

function renderSteps() {
  // Weekly chart: last 52 weeks
  const weeks = {};
  filtered.forEach(d => {
    if (d.total_steps == null) return;
    const dt = new Date(d.date + 'T12:00:00');
    const jan4 = new Date(dt.getFullYear(), 0, 4);
    const wn = Math.ceil(((dt - jan4) / 86400000 + jan4.getDay() + 1) / 7);
    const yr = wn === 0 ? dt.getFullYear()-1 : (wn>52&&dt.getMonth()===0?dt.getFullYear()-1:dt.getFullYear());
    const key = yr + '-W' + String(wn).padStart(2,'0');
    weeks[key] = (weeks[key]||0) + (+d.total_steps);
  });
  const wkeys = Object.keys(weeks).sort().slice(-52);
  AR.barChart('#steps-chart', {
    labels: wkeys, values: wkeys.map(k => weeks[k]), color: 'var(--accent)', height: 180,
    xFmt: k => k.slice(5),
    tip: i => `<b>${wkeys[i]}</b><br>${weeks[wkeys[i]].toLocaleString()} steps`,
  });

  // Monthly chart
  const mo = groupByMonth(filtered, ['total_steps','distance_mi','active_kilocalories']);
  const mokeys = Object.keys(mo).sort();
  const motot = mokeys.map(k => mo[k].total_steps.reduce((s,v)=>s+v,0));
  AR.barChart('#monthly-chart', {
    labels: mokeys, values: motot, color: 'var(--accent)', height: 180,
    xFmt: k => AR.fmtDate(k),
    tip: i => `<b>${AR.fmtDate(mokeys[i])}</b><br>${motot[i].toLocaleString()} steps`,
  });

  // Table
  document.getElementById('steps-tbody').innerHTML = [...mokeys].reverse().map(k => {
    const s = mo[k].total_steps, dist = mo[k].distance_mi, cal = mo[k].active_kilocalories;
    const avg = s.length ? s.reduce((a,v)=>a+v,0)/s.length : null;
    const tot = s.reduce((a,v)=>a+v,0);
    const avgDist = dist.length ? dist.reduce((a,v)=>a+v,0)/dist.length : null;
    const avgCal  = cal.length  ? cal.reduce((a,v)=>a+v,0)/cal.length   : null;
    return `<tr>
      <td>${k}</td>
      <td class="num">${s.length}</td>
      <td class="num">${avg?fmt0(avg):'—'}</td>
      <td class="num">${tot?tot.toLocaleString():'—'}</td>
      <td class="num">${avgDist?fmt1(avgDist)+' mi':'—'}</td>
      <td class="num">${avgCal?fmt0(avgCal):'—'}</td>
    </tr>`;
  }).join('');
}

// ── HEART RATE ────────────────────────────────────────────────────────────
function renderHeart() {
  renderCardGrid('heart-charts', ['rhr', 'hrv']);

  // Monthly table
  const mo = groupByMonth(filtered, ['resting_heart_rate','hrv_last_night_avg','max_heart_rate']);
  const mokeys = Object.keys(mo).sort();
  document.getElementById('hr-tbody').innerHTML = [...mokeys].reverse().map(k => {
    const rhr = mo[k].resting_heart_rate;
    const hrv = mo[k].hrv_last_night_avg;
    const mhr = mo[k].max_heart_rate;
    const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
    const mn = (arr) => arr.length ? Math.min(...arr) : null;
    return `<tr>
      <td>${k}</td>
      <td class="num">${a(rhr)?fmt1(a(rhr)):'—'}</td>
      <td class="num">${mn(rhr)?fmt0(mn(rhr)):'—'}</td>
      <td class="num">${a(hrv)?fmt1(a(hrv)):'—'}</td>
      <td class="num">${a(mhr)?fmt0(a(mhr)):'—'}</td>
    </tr>`;
  }).join('');
}

// ── SLEEP ─────────────────────────────────────────────────────────────────
function renderSleep() {
  renderCardGrid('sleep-charts', ['sleep']);

  const mo = groupByMonth(filtered, ['sleep_total_h','sleep_deep_h','sleep_rem_h','sleep_score','sleep_avg_spo2']);
  const mokeys = Object.keys(mo).sort();
  document.getElementById('sleep-tbody').innerHTML = [...mokeys].reverse().map(k => {
    const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
    return `<tr>
      <td>${k}</td>
      <td class="num">${mo[k].sleep_total_h.length}</td>
      <td class="num">${a(mo[k].sleep_total_h)?fmt1(a(mo[k].sleep_total_h)):'—'}</td>
      <td class="num">${a(mo[k].sleep_deep_h)?fmt1(a(mo[k].sleep_deep_h)):'—'}</td>
      <td class="num">${a(mo[k].sleep_rem_h)?fmt1(a(mo[k].sleep_rem_h)):'—'}</td>
      <td class="num">${a(mo[k].sleep_score)?fmt1(a(mo[k].sleep_score)):'—'}</td>
      <td class="num">${a(mo[k].sleep_avg_spo2)?fmtPct(a(mo[k].sleep_avg_spo2)):'—'}</td>
    </tr>`;
  }).join('');
}

// ── BODY ──────────────────────────────────────────────────────────────────
function renderBody() {
  renderCardGrid('body-charts', ['weight']);

  const weightDays = filtered.filter(d=>d.weight_lbs!=null);
  document.getElementById('body-tbody').innerHTML = [...weightDays].reverse().map(d =>
    `<tr>
      <td>${d.date}</td>
      <td class="num">${fmt1(d.weight_lbs)}</td>
      <td class="num">${fmt1(d.weight_kg)}</td>
      <td class="num">${d.bmi?fmt1(d.bmi):'—'}</td>
      <td class="num">${d.body_fat_pct?fmtPct(d.body_fat_pct):'—'}</td>
    </tr>`
  ).join('');
}

// ── STRESS & BATTERY ──────────────────────────────────────────────────────
function renderStress() {
  renderCardGrid('stress-charts', ['stress', 'battery']);

  const mo = groupByMonth(filtered, ['avg_stress_level','body_battery_highest','body_battery_lowest',
                                      'moderate_intensity_mins','vigorous_intensity_mins']);
  const mokeys = Object.keys(mo).sort();
  const a = (arr) => arr.length ? arr.reduce((s,v)=>s+v,0)/arr.length : null;
  document.getElementById('stress-tbody').innerHTML = [...mokeys].reverse().map(k => `<tr>
    <td>${k}</td>
    <td class="num">${a(mo[k].avg_stress_level)?fmt1(a(mo[k].avg_stress_level)):'—'}</td>
    <td class="num">${a(mo[k].body_battery_highest)?fmt0(a(mo[k].body_battery_highest)):'—'}</td>
    <td class="num">${a(mo[k].body_battery_lowest)?fmt0(a(mo[k].body_battery_lowest)):'—'}</td>
    <td class="num">${a(mo[k].moderate_intensity_mins)?fmt0(a(mo[k].moderate_intensity_mins)):'—'}</td>
    <td class="num">${a(mo[k].vigorous_intensity_mins)?fmt0(a(mo[k].vigorous_intensity_mins)):'—'}</td>
  </tr>`).join('');
}

// ── DAILY TABLE ───────────────────────────────────────────────────────────
function renderDailyTable() {
  const mul = sortDir;
  const sorted = [...filtered].sort((a,b) => {
    const av = a[sortCol], bv = b[sortCol];
    if (av==null && bv==null) return 0;
    if (av==null) return 1; if (bv==null) return -1;
    return av < bv ? -mul : av > bv ? mul : 0;
  });
  const total = sorted.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  page = Math.min(page, pages);
  const slice = sorted.slice((page-1)*PAGE_SIZE, page*PAGE_SIZE);

  document.querySelectorAll('#daily-table thead th[data-col]').forEach(th => {
    th.classList.remove('sorted-asc','sorted-desc');
    if (th.dataset.col === sortCol)
      th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  });

  document.getElementById('daily-tbody').innerHTML = slice.map(d => `<tr>
    <td>${d.date}</td>
    <td class="num">${d.total_steps!=null?fmt0(d.total_steps):'—'}</td>
    <td class="num">${d.distance_mi!=null?fmt1(d.distance_mi):'—'}</td>
    <td class="num">${d.resting_heart_rate??'—'}</td>
    <td class="num">${d.avg_heart_rate??'—'}</td>
    <td class="num">${d.hrv_last_night_avg!=null?fmt0(d.hrv_last_night_avg):'—'}</td>
    <td class="num">${d.sleep_total_h!=null?fmt1(d.sleep_total_h):'—'}</td>
    <td class="num">${d.sleep_score!=null?fmt1(d.sleep_score):'—'}</td>
    <td class="num">${d.weight_lbs!=null?fmt1(d.weight_lbs):'—'}</td>
    <td class="num">${d.avg_stress_level??'—'}</td>
    <td class="num">${d.body_battery_highest??'—'}</td>
    <td class="num">${d.active_kilocalories!=null?fmt0(d.active_kilocalories):'—'}</td>
  </tr>`).join('');

  const pg = document.getElementById('daily-pagination');
  if (pages <= 1) { pg.innerHTML=''; return; }
  const btns = [`<span class="info">Page ${page} of ${pages} (${total.toLocaleString()} days)</span>`];
  if (page > 1) btns.push(`<button onclick="goPage(${page-1})">‹ Prev</button>`);
  const lo = Math.max(1,page-3), hi = Math.min(pages,page+3);
  for (let p=lo;p<=hi;p++) btns.push(`<button class="${p===page?'active':''}" onclick="goPage(${p})">${p}</button>`);
  if (page < pages) btns.push(`<button onclick="goPage(${page+1})">Next ›</button>`);
  pg.innerHTML = btns.join('');
}

window.goPage = p => { page = p; renderDailyTable(); window.scrollTo(0,0); };

document.querySelectorAll('#daily-table thead th[data-col]').forEach(th => {
  th.addEventListener('click', () => {
    if (sortCol === th.dataset.col) sortDir *= -1;
    else { sortCol = th.dataset.col; sortDir = -1; }
    page = 1; renderDailyTable();
  });
});

// ── BOOT ──────────────────────────────────────────────────────────────────
render();
{trends_script}
</script>
</body>
</html>
"""


TRENDS_CSS = """\
/* phase-plot ramp: earlier → later reads faint → prominent in either theme */
:root { --ramp-from: #b5dbe6; --ramp-to: #075d78; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) { --ramp-from: #2f4f66; --ramp-to: #8be4fb; }
}
:root[data-theme="dark"] { --ramp-from: #2f4f66; --ramp-to: #8be4fb; }
.tdesc { max-width: 78ch; }
.phase-controls { display: flex; align-items: center; gap: 12px; margin: 0 0 10px; }
.phase-controls input[type=range] { flex: 1; min-width: 0; accent-color: var(--accent); }
.phase-controls .when { min-width: 74px; text-align: right; color: var(--muted);
                        font-variant-numeric: tabular-nums; font-size: 0.8rem; }
"""

TRENDS_PANEL = """\
    <!-- ── LONG-TERM TRENDS ── -->
    <div class="section active" id="section-trends">
      <div class="note">These charts always show the complete Garmin history, independent of the date range above.</div>
      <div class="metric-grid wide">

        <div class="metric-card">
          <div class="metric-card-title">Daily steps, averaged by month</div>
          <div class="metric-card-val" id="t-steps-val" style="color:var(--accent)"></div>
          <div class="metric-card-sub tdesc">Faint line is each month's average; solid line is the 12-month rolling average.</div>
          <div id="t-steps-legend"></div>
          <div class="chart-area" id="t-steps" role="img" aria-label="Monthly average daily steps over time"></div>
        </div>

        <div class="metric-card">
          <div class="metric-card-title">Miles run and biked per week</div>
          <div class="metric-card-val card-vals" id="t-miles-val"></div>
          <div class="metric-card-sub tdesc">12-month rolling average of weekly miles; hover for any month's actual pace. Cycling overtook running in 2025.</div>
          <div id="t-miles-legend"></div>
          <div class="chart-area" id="t-miles" role="img" aria-label="Monthly miles run and biked per week"></div>
        </div>

        <div class="metric-card">
          <div class="metric-card-title">Sleep duration, averaged by month</div>
          <div class="metric-card-val" id="t-sleep-val" style="color:var(--purple)"></div>
          <div class="metric-card-sub tdesc">Monthly mean nightly sleep, with the 12-month rolling trend.</div>
          <div id="t-sleep-legend"></div>
          <div class="chart-area" id="t-sleep" role="img" aria-label="Monthly average sleep duration over time"></div>
        </div>

        <div class="metric-card">
          <div class="metric-card-title">Overnight HRV, averaged by month</div>
          <div class="metric-card-val" id="t-hrv-val" style="color:var(--yellow)"></div>
          <div class="metric-card-sub tdesc">Garmin's morning-report HRV (last-night average, ms). Recorded since mid-2022.</div>
          <div id="t-hrv-legend"></div>
          <div class="chart-area" id="t-hrv" role="img" aria-label="Monthly average overnight HRV over time"></div>
        </div>

        <div class="metric-card">
          <div class="metric-card-title">Sleep regularity</div>
          <div class="metric-card-val card-vals" id="t-sri-val"></div>
          <div class="metric-card-sub tdesc">SRI: minute-by-minute sleep/wake agreement between consecutive days (100 = identical schedule).
          Consistency: the same agreement against a weighted window of the previous four days (Whoop-style) — a stricter read.</div>
          <div id="t-sri-legend"></div>
          <div class="chart-area" id="t-sri" role="img" aria-label="Monthly sleep regularity index and 4-day consistency over time"></div>
        </div>

        <div class="metric-card">
          <div class="metric-card-title">Sleep duration × regularity, walked through time</div>
          <div class="metric-card-sub tdesc">Each point is a month (12-month rolling sleep duration against 4-day consistency);
          the line connects them in order, earlier → later. Press play to watch the path unfold, or drag the slider.</div>
          <div class="phase-controls">
            <button class="btn active" id="phase-play" type="button">▶ Play</button>
            <input type="range" id="phase-scrub" min="0" max="1" step="0.001" value="0" aria-label="Scrub through time">
            <span class="when" id="phase-when"></span>
          </div>
          <div class="chart-area" id="t-phase" role="img" aria-label="Connected scatterplot of sleep duration against 4-day consistency, traced chronologically"></div>
        </div>

      </div>
    </div>
"""

TRENDS_SCRIPT = r"""
// ── LONG-TERM TRENDS ──────────────────────────────────────────────────────
(function () {
  const T = TRENDS;
  if (!T.months || !T.months.length) return;
  const last = a => { for (let i = a.length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };
  const fmtSteps = v => Math.round(v).toLocaleString();
  const fmtSleep = v => { const h = Math.floor(v), m = Math.round((v - h) * 60); return h + 'h ' + String(m).padStart(2, '0') + 'm'; };
  const niceMax = (v, step) => Math.ceil(v / step) * step;
  const GRAY = 'var(--gray)';

  // cfg: { series:[{name, values, color, width, lbl}], fmt (tooltip/value), tick (axis), yDomain, yTicks }
  // The latest value of every series that has `lbl` goes in the card's big value line.
  function trend(id, cfg) {
    AR.legend('#' + id + '-legend', cfg.series);
    const head = cfg.series.filter(s => s.lbl != null && last(s.values) != null);
    document.getElementById(id + '-val').innerHTML = head.length > 1
      ? head.map(s => `<span style="color:${s.color}">${cfg.fmt(last(s.values))}<span class="lbl">${s.lbl}</span></span>`).join('')
      : head.map(s => cfg.fmt(last(s.values)) + (s.lbl ? ` <span class="lbl">${s.lbl}</span>` : '')).join('');
    AR.lineChart('#' + id, {
      x: T.months, series: cfg.series, height: 240,
      yDomain: cfg.yDomain, yFmt: cfg.tick, yTicks: cfg.yTicks,
      tip: i => `<b>${AR.fmtDate(T.months[i])}</b><br>` + cfg.series.map(s =>
        `${AR.swatch(s.color)}<span class="dim">${s.name}</span> ${s.values[i] == null ? '—' : cfg.fmt(s.values[i])}`).join('<br>'),
    });
  }

  trend('t-steps', {
    yDomain: [0, 20000], yTicks: 4, tick: v => v === 0 ? '0' : (v / 1000) + 'k', fmt: fmtSteps,
    series: [
      { name: 'Monthly avg', values: T.steps, color: GRAY, width: 1.5 },
      { name: '12-month rolling', values: T.steps_roll, color: 'var(--accent)', width: 2.5, lbl: '' },
    ],
  });

  {
    const peak = Math.max(1, ...T.run_roll.filter(v => v != null), ...T.bike_roll.filter(v => v != null));
    trend('t-miles', {
      yDomain: [0, niceMax(peak, 25)], yTicks: 5, tick: v => String(v), fmt: v => Math.round(v) + ' mi/wk',
      series: [
        { name: 'Running', values: T.run_roll, color: 'var(--orange)', width: 2.5, lbl: 'run' },
        { name: 'Cycling', values: T.bike_roll, color: 'var(--teal)', width: 2.5, lbl: 'bike' },
      ],
    });
  }

  trend('t-sleep', {
    yDomain: [5, 10], yTicks: 5, tick: v => v + 'h', fmt: fmtSleep,
    series: [
      { name: 'Monthly avg', values: T.sleep, color: GRAY, width: 1.5 },
      { name: '12-month rolling', values: T.sleep_roll, color: 'var(--purple)', width: 2.5, lbl: '' },
    ],
  });

  {
    const hv = [...T.hrv, ...T.hrv_roll].filter(v => v != null);
    if (hv.length) {
      const yMax = niceMax(Math.max(...hv), 10), yMin = Math.max(0, Math.floor(Math.min(...hv) / 10) * 10 - 10);
      trend('t-hrv', {
        yDomain: [yMin, yMax], yTicks: 5, tick: v => String(v), fmt: v => Math.round(v) + ' ms',
        series: [
          { name: 'Monthly avg', values: T.hrv, color: GRAY, width: 1.5 },
          { name: '12-month rolling', values: T.hrv_roll, color: 'var(--yellow)', width: 2.5, lbl: '' },
        ],
      });
    }
  }

  trend('t-sri', {
    yDomain: [40, 100], yTicks: 4, tick: v => String(v), fmt: v => v.toFixed(0),
    series: [
      { name: 'SRI monthly', values: T.sri, color: GRAY, width: 1.5 },
      { name: 'SRI 12-mo', values: T.sri_roll, color: 'var(--teal)', width: 2.5, lbl: 'SRI' },
      { name: 'Consistency (4-day) 12-mo', values: T.cons_roll, color: 'var(--pink)', width: 2.5, lbl: 'consistency' },
    ],
  });

  // ── phase plot (sleep duration × 4-day consistency, animated; d3) ──
  (function () {
    const pts = T.phase;
    const box = document.getElementById('t-phase');
    if (!box || pts.length < 2) return;
    const nP = pts.length;
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const xMin = Math.floor((Math.min(...xs) - 0.05) * 2) / 2, xMax = Math.ceil((Math.max(...xs) + 0.05) * 2) / 2;
    const yMin = Math.floor((Math.min(...ys) - 0.5) / 2.5) * 2.5, yMax = Math.ceil((Math.max(...ys) + 0.5) / 2.5) * 2.5;

    const playBtn = document.getElementById('phase-play');
    const scrub = document.getElementById('phase-scrub');
    const when = document.getElementById('phase-when');
    let progress = 0, raf = null, lastTs = null;
    let x, y, segs, marks, head;      // rebuilt by build()

    function build() {
      const W = box.clientWidth || 900, H = Math.round(Math.max(280, Math.min(420, W * 0.6)));
      const m = { t: 14, r: 20, b: 40, l: 48 };
      d3.select(box).selectAll('*').remove();
      const svg = d3.select(box).append('svg').attr('class', 'ar-chart')
        .attr('viewBox', `0 0 ${W} ${H}`).attr('height', H);
      x = d3.scaleLinear().domain([xMin, xMax]).range([m.l, W - m.r]);
      y = d3.scaleLinear().domain([yMin, yMax]).range([H - m.b, m.t]);

      let xt = d3.range(xMin, xMax + 0.01, 0.5);
      if ((W - m.l - m.r) / xt.length < 64) xt = xt.filter((_, i) => i % 2 === 0);
      svg.append('g').attr('class', 'grid').attr('transform', `translate(${m.l},0)`)
        .call(d3.axisLeft(y).tickValues(d3.range(yMin, yMax + 0.01, 2.5)).tickSize(-(W - m.l - m.r))
          .tickFormat(v => v % 5 === 0 ? String(v) : '').tickPadding(6));
      svg.append('g').attr('class', 'grid').attr('transform', `translate(0,${H - m.b})`)
        .call(d3.axisBottom(x).tickValues(xt).tickSize(-(H - m.t - m.b)).tickFormat(fmtSleep).tickPadding(6));
      svg.append('text').attr('class', 'axis-title').attr('x', (m.l + W - m.r) / 2).attr('y', H - 6)
        .attr('text-anchor', 'middle').text('average sleep per night →');
      svg.append('text').attr('class', 'axis-title').attr('text-anchor', 'middle')
        .attr('transform', `translate(12,${(m.t + H - m.b) / 2}) rotate(-90)`).text('4-day consistency →');

      // sequential ramp, earliest → latest (endpoints are theme tokens)
      const css = getComputedStyle(document.documentElement);
      const ramp = d3.interpolateRgb(css.getPropertyValue('--ramp-from').trim(),
                                     css.getPropertyValue('--ramp-to').trim());
      segs = svg.append('g').selectAll('line').data(d3.range(1, nP)).join('line')
        .attr('x1', i => x(pts[i - 1].x)).attr('y1', i => y(pts[i - 1].y))
        .attr('x2', i => x(pts[i].x)).attr('y2', i => y(pts[i].y))
        .attr('stroke', i => ramp(i / (nP - 1))).attr('stroke-width', 2).attr('stroke-linecap', 'round');
      marks = svg.append('g').selectAll('g').data(d3.range(nP).filter(i => pts[i].m.endsWith('-01'))).join('g');
      marks.append('circle').attr('class', 'ring').attr('r', 3.5)
        .attr('cx', i => x(pts[i].x)).attr('cy', i => y(pts[i].y)).attr('fill', i => ramp(i / (nP - 1)));
      marks.append('text').attr('x', i => x(pts[i].x) + 6).attr('y', i => y(pts[i].y) - 6)
        .text(i => pts[i].m.slice(0, 4));
      head = svg.append('circle').attr('class', 'ring').attr('r', 5.5).attr('fill', 'var(--accent)');

      // nearest-month hover
      const focus = svg.append('circle').attr('r', 7).attr('fill', 'none').attr('stroke', 'var(--text)')
        .attr('stroke-width', 1.5).style('display', 'none').style('pointer-events', 'none');
      svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', Math.max(0, W - m.l - m.r))
        .attr('height', Math.max(0, H - m.t - m.b)).attr('fill', 'transparent')
        .on('mousemove', ev => {
          const [mx, my] = d3.pointer(ev, svg.node());
          let best = -1, bd = 30;
          pts.forEach((p, i) => { const d = Math.hypot(x(p.x) - mx, y(p.y) - my); if (d < bd) { bd = d; best = i; } });
          if (best < 0) { focus.style('display', 'none'); AR.tip.hide(); return; }
          const p = pts[best];
          focus.style('display', null).attr('cx', x(p.x)).attr('cy', y(p.y));
          AR.tip.show(`<b>${AR.fmtDate(p.m)}</b><br>${fmtSleep(p.x)} · consistency ${p.y.toFixed(0)}`, ev);
        })
        .on('mouseleave', () => { focus.style('display', 'none'); AR.tip.hide(); });
      draw();
    }

    function draw() {
      const t = progress * (nP - 1);
      segs.attr('opacity', i => i <= t ? 1 : 0);
      marks.attr('opacity', i => i <= t ? 1 : 0);
      const i0 = Math.min(Math.floor(t), nP - 2), f = t - i0;
      head.attr('cx', x(pts[i0].x + (pts[i0 + 1].x - pts[i0].x) * f))
          .attr('cy', y(pts[i0].y + (pts[i0 + 1].y - pts[i0].y) * f));
      when.textContent = AR.fmtDate(pts[Math.round(t)].m);
      scrub.value = progress;
    }

    const DURATION = 12000;
    const stop = () => { if (raf) cancelAnimationFrame(raf); raf = null; lastTs = null; playBtn.textContent = progress >= 1 ? '↻ Replay' : '▶ Play'; };
    const tick = ts => {
      if (lastTs != null) { progress = Math.min(1, progress + (ts - lastTs) / DURATION); draw(); if (progress >= 1) { stop(); return; } }
      lastTs = ts; raf = requestAnimationFrame(tick);
    };
    const play = () => { if (progress >= 1) progress = 0; playBtn.textContent = '❚❚ Pause'; raf = requestAnimationFrame(tick); };
    let io = null;                   // autoplay-on-scroll, cancelled by any manual control
    const manual = () => { if (io) { io.disconnect(); io = null; } };
    playBtn.addEventListener('click', () => { manual(); raf ? stop() : play(); });
    scrub.addEventListener('input', () => { manual(); stop(); progress = +scrub.value; draw(); });

    // redraw at the container's width, and when the theme (ramp endpoints) changes
    let lastW = -1;
    new ResizeObserver(() => {
      const w = Math.round(box.clientWidth);
      if (w && w !== lastW) { lastW = w; build(); }
    }).observe(box);
    document.addEventListener('ar:theme', build);

    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { progress = 1; build(); playBtn.textContent = '↻ Replay'; }
    else {
      progress = 0; build();
      io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) { manual(); play(); } }, { threshold: 0.4 });
      io.observe(box);
    }
  })();
})();
"""


def build(db_path: str, out_path: str) -> None:
    print(f"Loading data from {db_path} …")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    days = load_data(db_path)
    trends = build_trends(con, days)
    con.close()

    data_js = f"const ALL_DAYS = {json.dumps(days, separators=(',', ':'))};\n"
    trends_js = f"const TRENDS = {json.dumps(trends, separators=(',', ':'))};\n"

    html = (HTML_TEMPLATE
            .replace("{generated}", datetime.now().strftime("%Y-%m-%d"))
            .replace("{trends_css}", TRENDS_CSS)
            .replace("{trends_panel}", TRENDS_PANEL)
            .replace("{trends_script}", TRENDS_SCRIPT)
            .replace("{trends_js}", trends_js)
            .replace("{data_js}", data_js))
    Path(out_path).write_text(html, encoding="utf-8")
    size = Path(out_path).stat().st_size
    print(f"Written {out_path} ({size/1024:.0f} KB, {len(days)} days, "
          f"{len(trends.get('months', []))} trend months)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate index.html from garmin.db")
    ap.add_argument("--db",  default="garmin.db",  help="SQLite DB path")
    ap.add_argument("--out", default="index.html", help="Output HTML path")
    args = ap.parse_args()
    build(args.db, args.out)


if __name__ == "__main__":
    main()
