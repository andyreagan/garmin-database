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
{trends_css}
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
    <div class="tab active" data-tab="trends">Long-term Trends</div>
    <div class="tab" data-tab="steps">Steps</div>
    <div class="tab" data-tab="heart">Heart Rate</div>
    <div class="tab" data-tab="sleep">Sleep</div>
    <div class="tab" data-tab="body">Body</div>
    <div class="tab" data-tab="stress">Stress & Battery</div>
    <div class="tab" data-tab="daily">Daily Log</div>
  </div>

{trends_panel}

  <!-- STEPS TAB -->
  <div class="panel" id="panel-steps">
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
{trends_js}

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
{trends_script}
</script>
</body>
</html>
"""


TRENDS_CSS = """\
  /* ── long-term trends ── */
  .trend-card { background: var(--surface); border: 1px solid var(--border);
                border-radius: var(--radius); padding: 16px; margin-bottom: 20px; }
  .trend-card h3 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: .06em;
                   color: var(--muted); margin-bottom: 4px; }
  .trend-card p.tdesc { color: var(--muted); font-size: 0.78rem; margin-bottom: 10px; max-width: 78ch; }
  .trend-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 8px;
                  font-size: 0.75rem; color: var(--muted); }
  .trend-legend .k { display: inline-flex; align-items: center; gap: 6px; }
  .trend-legend .sw { width: 16px; height: 0; border-top: 2.5px solid; border-radius: 2px; }
  .trend-box { position: relative; }
  .trend-box svg { display: block; width: 100%; height: auto; }
  .trend-box:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  .trend-tip { position: absolute; pointer-events: none; display: none; z-index: 5;
               background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
               padding: 7px 10px; font-size: 0.75rem; min-width: 150px;
               box-shadow: 0 4px 14px rgba(0,0,0,.45); }
  .trend-tip .tm { color: var(--muted); font-weight: 600; margin-bottom: 3px; }
  .trend-tip .tr { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .trend-tip .tk { width: 11px; height: 0; border-top: 2.5px solid; border-radius: 2px; flex: none; }
  .trend-tip .tv { font-variant-numeric: tabular-nums; font-weight: 700; }
  .trend-tip .tl { color: var(--muted); }
  .phase-controls { display: flex; align-items: center; gap: 12px; margin: 6px 0 8px; }
  .phase-controls button { background: var(--accent); color: #fff; border: none;
                           border-radius: 6px; padding: 5px 14px; cursor: pointer;
                           font-size: 0.8rem; font-weight: 600; }
  .phase-controls button:hover { background: var(--accent2); }
  .phase-controls input[type=range] { flex: 1; accent-color: var(--accent); }
  .phase-controls .when { min-width: 74px; text-align: right; color: var(--muted);
                          font-variant-numeric: tabular-nums; font-size: 0.8rem; }
"""

TRENDS_PANEL = """\
  <!-- LONG-TERM TRENDS TAB -->
  <div class="panel active" id="panel-trends">
    <div class="trend-card">
      <h3>Daily steps, averaged by month</h3>
      <p class="tdesc">Faint line is each month's average; solid line is the 12-month rolling average.</p>
      <div class="trend-legend">
        <span class="k"><span class="sw" style="border-color:#4b5066"></span>Monthly avg</span>
        <span class="k"><span class="sw" style="border-color:#00b4d8"></span>12-month rolling</span>
      </div>
      <div class="trend-box" id="t-steps" tabindex="0" role="img" aria-label="Monthly average daily steps over time"></div>
    </div>

    <div class="trend-card">
      <h3>Miles run and biked per week</h3>
      <p class="tdesc">12-month rolling average of weekly miles; hover for any month's actual pace. Cycling overtook running in 2025.</p>
      <div class="trend-legend">
        <span class="k"><span class="sw" style="border-color:#f4a261"></span>Running</span>
        <span class="k"><span class="sw" style="border-color:#2ec4b6"></span>Cycling</span>
      </div>
      <div class="trend-box" id="t-miles" tabindex="0" role="img" aria-label="Monthly miles run and biked per week"></div>
    </div>

    <div class="trend-card">
      <h3>Sleep duration, averaged by month</h3>
      <p class="tdesc">Monthly mean nightly sleep, with the 12-month rolling trend.</p>
      <div class="trend-legend">
        <span class="k"><span class="sw" style="border-color:#4b5066"></span>Monthly avg</span>
        <span class="k"><span class="sw" style="border-color:#b892ff"></span>12-month rolling</span>
      </div>
      <div class="trend-box" id="t-sleep" tabindex="0" role="img" aria-label="Monthly average sleep duration over time"></div>
    </div>

    <div class="trend-card">
      <h3>Overnight HRV, averaged by month</h3>
      <p class="tdesc">Garmin's morning-report HRV (last-night average, ms). Recorded since mid-2022.</p>
      <div class="trend-legend">
        <span class="k"><span class="sw" style="border-color:#4b5066"></span>Monthly avg</span>
        <span class="k"><span class="sw" style="border-color:#ffd166"></span>12-month rolling</span>
      </div>
      <div class="trend-box" id="t-hrv" tabindex="0" role="img" aria-label="Monthly average overnight HRV over time"></div>
    </div>

    <div class="trend-card">
      <h3>Sleep regularity</h3>
      <p class="tdesc">SRI: minute-by-minute sleep/wake agreement between consecutive days (100 = identical schedule).
      Consistency: the same agreement against a weighted window of the previous four days (Whoop-style) — a stricter read.</p>
      <div class="trend-legend">
        <span class="k"><span class="sw" style="border-color:#4b5066"></span>SRI monthly</span>
        <span class="k"><span class="sw" style="border-color:#43aa8b"></span>SRI 12-mo</span>
        <span class="k"><span class="sw" style="border-color:#e76f9e"></span>Consistency (4-day) 12-mo</span>
      </div>
      <div class="trend-box" id="t-sri" tabindex="0" role="img" aria-label="Monthly sleep regularity index and 4-day consistency over time"></div>
    </div>

    <div class="trend-card">
      <h3>Sleep duration × regularity, walked through time</h3>
      <p class="tdesc">Each point is a month (12-month rolling sleep duration against 4-day consistency);
      the line connects them in order, earlier → later. Press play to watch the path unfold, or drag the slider.</p>
      <div class="phase-controls">
        <button id="phase-play" type="button">▶ Play</button>
        <input type="range" id="phase-scrub" min="0" max="1" step="0.001" value="0" aria-label="Scrub through time">
        <span class="when" id="phase-when"></span>
      </div>
      <div class="trend-box" id="t-phase" tabindex="0" role="img" aria-label="Connected scatterplot of sleep duration against 4-day consistency, traced chronologically"></div>
    </div>
  </div>
"""

TRENDS_SCRIPT = r"""
// ── LONG-TERM TRENDS ──────────────────────────────────────────────────────
(function () {
  const T = TRENDS;
  if (!T.months || !T.months.length) return;
  const NS = 'http://www.w3.org/2000/svg';
  const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const el = (p, name, attrs) => {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    p.appendChild(e);
    return e;
  };
  const monthLabel = m => { const [y, mo] = m.split('-'); return MONTH_NAMES[+mo - 1] + ' ' + y; };
  const W = 900, H = 260, M = { t: 12, r: 66, b: 24, l: 46 };
  const iw = W - M.l - M.r, ih = H - M.t - M.b;

  function lineChart(boxId, cfg) {
    const box = document.getElementById(boxId);
    if (!box) return;
    const months = T.months, n = months.length;
    const x = i => M.l + (n === 1 ? 0 : i / (n - 1) * iw);
    const y = v => M.t + ih - (v - cfg.yMin) / (cfg.yMax - cfg.yMin) * ih;
    const svg = el(box, 'svg', { viewBox: `0 0 ${W} ${H}` });

    for (const tv of cfg.ticks) {
      el(svg, 'line', { x1: M.l, x2: M.l + iw, y1: y(tv), y2: y(tv), stroke: 'var(--border)', 'stroke-width': 1 });
      const t = el(svg, 'text', { x: M.l - 7, y: y(tv) + 4, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)' });
      t.textContent = cfg.tickFmt(tv);
    }
    months.forEach((m, i) => {
      const [yy, mo] = m.split('-');
      if (mo === '01' && +yy % 2 === 0) {
        const t = el(svg, 'text', { x: x(i), y: M.t + ih + 16, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--muted)' });
        t.textContent = yy;
      }
    });
    el(svg, 'line', { x1: M.l, x2: M.l + iw, y1: M.t + ih, y2: M.t + ih, stroke: 'var(--border)', 'stroke-width': 1 });

    const pathOf = vals => {
      let s = '', pen = false;
      vals.forEach((v, i) => {
        if (v == null) { pen = false; return; }
        s += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
        pen = true;
      });
      return s;
    };
    cfg.series.forEach(se => {
      el(svg, 'path', { d: pathOf(se.vals), fill: 'none', stroke: se.color,
        'stroke-width': se.width || 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        opacity: se.faint ? 0.55 : 1 });
    });
    cfg.series.filter(se => se.label).forEach(se => {
      let last = -1;
      se.vals.forEach((v, i) => { if (v != null) last = i; });
      if (last < 0) return;
      el(svg, 'circle', { cx: x(last), cy: y(se.vals[last]), r: 3.5, fill: se.color,
        stroke: 'var(--surface)', 'stroke-width': 1.5 });
      const t = el(svg, 'text', { x: x(last) + 7, y: y(se.vals[last]) + 3.5 + (se.dy || 0),
        'font-size': 11, 'font-weight': 700, fill: 'var(--text)' });
      t.textContent = se.label(se.vals[last]);
    });

    const cross = el(svg, 'line', { y1: M.t, y2: M.t + ih, stroke: 'var(--muted)', 'stroke-width': 1, visibility: 'hidden' });
    const dots = cfg.series.map(se => el(svg, 'circle', { r: 3.5, fill: se.color,
      stroke: 'var(--surface)', 'stroke-width': 1.5, visibility: 'hidden' }));
    const tip = document.createElement('div');
    tip.className = 'trend-tip';
    const tm = document.createElement('div'); tm.className = 'tm'; tip.appendChild(tm);
    const rows = cfg.series.map(se => {
      const r = document.createElement('div'); r.className = 'tr';
      const k = document.createElement('span'); k.className = 'tk'; k.style.borderColor = se.color;
      const v = document.createElement('span'); v.className = 'tv';
      const l = document.createElement('span'); l.className = 'tl'; l.textContent = se.name;
      r.append(k, v, l); tip.appendChild(r);
      return v;
    });
    box.appendChild(tip);
    let cur = -1;
    const show = i => {
      cur = i;
      cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
      tm.textContent = monthLabel(months[i]);
      cfg.series.forEach((se, k) => {
        const v = se.vals[i];
        if (v != null) {
          dots[k].setAttribute('cx', x(i)); dots[k].setAttribute('cy', y(v)); dots[k].setAttribute('visibility', 'visible');
          rows[k].textContent = cfg.fmt(v);
        } else { dots[k].setAttribute('visibility', 'hidden'); rows[k].textContent = '—'; }
      });
      tip.style.display = 'block';
      const rect = box.getBoundingClientRect();
      const px = x(i) / W * rect.width;
      tip.style.left = (px > rect.width * 0.6 ? px - tip.offsetWidth - 12 : px + 12) + 'px';
      tip.style.top = '8px';
    };
    const hide = () => { cur = -1; cross.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); tip.style.display = 'none'; };
    box.addEventListener('pointermove', ev => {
      const rect = box.getBoundingClientRect();
      const fx = (ev.clientX - rect.left) / rect.width * W;
      show(Math.max(0, Math.min(n - 1, Math.round((fx - M.l) / iw * (n - 1)))));
    });
    box.addEventListener('pointerleave', hide);
    box.addEventListener('keydown', ev => {
      if (ev.key === 'ArrowRight') { show(Math.min(n - 1, cur < 0 ? n - 1 : cur + 1)); ev.preventDefault(); }
      else if (ev.key === 'ArrowLeft') { show(Math.max(0, cur < 0 ? n - 1 : cur - 1)); ev.preventDefault(); }
      else if (ev.key === 'Escape') hide();
    });
    box.addEventListener('blur', hide);
  }

  const fmtSteps = v => Math.round(v).toLocaleString();
  const fmtSleep = v => { const h = Math.floor(v), m = Math.round((v - h) * 60); return h + 'h ' + String(m).padStart(2, '0') + 'm'; };
  const niceMax = (v, step) => Math.ceil(v / step) * step;

  lineChart('t-steps', {
    yMin: 0, yMax: 20000, ticks: [0, 5000, 10000, 15000, 20000],
    tickFmt: v => v === 0 ? '0' : (v / 1000) + 'k', fmt: fmtSteps,
    series: [
      { vals: T.steps, color: '#4b5066', width: 1.5, faint: true, name: 'monthly' },
      { vals: T.steps_roll, color: '#00b4d8', width: 2.5, name: v => fmtSteps(v), label: v => fmtSteps(v) },
    ],
  });

  {
    const peak = Math.max(1, ...T.run_roll.filter(v => v != null), ...T.bike_roll.filter(v => v != null));
    const yMax = niceMax(peak, 25);
    const ticks = []; const step = yMax > 100 ? 25 : 10;
    for (let t = 0; t <= yMax; t += step) ticks.push(t);
    lineChart('t-miles', {
      yMin: 0, yMax, ticks, tickFmt: v => String(v), fmt: v => Math.round(v) + ' mi/wk',
      series: [
        { vals: T.run_roll, color: '#f4a261', width: 2.5, name: 'run', label: v => Math.round(v) + ' run' },
        { vals: T.bike_roll, color: '#2ec4b6', width: 2.5, name: 'bike', label: v => Math.round(v) + ' bike', dy: 12 },
      ],
    });
  }

  lineChart('t-sleep', {
    yMin: 5, yMax: 10, ticks: [5, 6, 7, 8, 9, 10], tickFmt: v => v + 'h', fmt: fmtSleep,
    series: [
      { vals: T.sleep, color: '#4b5066', width: 1.5, faint: true, name: 'monthly' },
      { vals: T.sleep_roll, color: '#b892ff', width: 2.5, name: v => fmtSleep(v), label: v => fmtSleep(v) },
    ],
  });

  {
    const hv = [...T.hrv, ...T.hrv_roll].filter(v => v != null);
    if (hv.length) {
      const yMax = niceMax(Math.max(...hv), 10), yMin = Math.max(0, Math.floor(Math.min(...hv) / 10) * 10 - 10);
      const ticks = []; for (let t = yMin; t <= yMax; t += 10) ticks.push(t);
      lineChart('t-hrv', {
        yMin, yMax, ticks, tickFmt: v => String(v), fmt: v => Math.round(v) + ' ms',
        series: [
          { vals: T.hrv, color: '#4b5066', width: 1.5, faint: true, name: 'monthly' },
          { vals: T.hrv_roll, color: '#ffd166', width: 2.5, name: v => Math.round(v) + ' ms', label: v => Math.round(v) + ' ms' },
        ],
      });
    }
  }

  lineChart('t-sri', {
    yMin: 40, yMax: 100, ticks: [40, 55, 70, 85, 100], tickFmt: v => String(v), fmt: v => v.toFixed(0),
    series: [
      { vals: T.sri, color: '#4b5066', width: 1.5, faint: true, name: 'SRI monthly' },
      { vals: T.cons_roll, color: '#e76f9e', width: 2.5, name: v => v.toFixed(0), label: v => v.toFixed(0), dy: 12 },
      { vals: T.sri_roll, color: '#43aa8b', width: 2.5, name: v => v.toFixed(0), label: v => v.toFixed(0) },
    ],
  });

  // ── phase plot (sleep duration × 4-day consistency, animated) ──
  (function () {
    const pts = T.phase;
    const box = document.getElementById('t-phase');
    if (!box || pts.length < 2) return;
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const xMin = Math.floor((Math.min(...xs) - 0.05) * 2) / 2, xMax = Math.ceil((Math.max(...xs) + 0.05) * 2) / 2;
    const yMin = Math.floor((Math.min(...ys) - 0.5) / 2.5) * 2.5, yMax = Math.ceil((Math.max(...ys) + 0.5) / 2.5) * 2.5;
    const PW = 900, PH = 420, PM = { t: 14, r: 20, b: 40, l: 44 };
    const piw = PW - PM.l - PM.r, pih = PH - PM.t - PM.b;
    const px = v => PM.l + (v - xMin) / (xMax - xMin) * piw;
    const py = v => PM.t + pih - (v - yMin) / (yMax - yMin) * pih;
    const svg = el(box, 'svg', { viewBox: `0 0 ${PW} ${PH}` });
    const fmtSleepP = v => { const h = Math.floor(v), m = Math.round((v - h) * 60); return h + 'h ' + String(m).padStart(2, '0') + 'm'; };

    for (let v = yMin; v <= yMax + 0.01; v += 2.5) {
      el(svg, 'line', { x1: PM.l, x2: PM.l + piw, y1: py(v), y2: py(v), stroke: 'var(--border)', 'stroke-width': 1 });
      const t = el(svg, 'text', { x: PM.l - 7, y: py(v) + 4, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)' });
      t.textContent = v % 5 === 0 ? String(v) : '';
    }
    for (let v = xMin; v <= xMax + 0.01; v += 0.5) {
      el(svg, 'line', { x1: px(v), x2: px(v), y1: PM.t, y2: PM.t + pih, stroke: 'var(--border)', 'stroke-width': 1 });
      const t = el(svg, 'text', { x: px(v), y: PM.t + pih + 16, 'text-anchor': 'middle', 'font-size': 10, fill: 'var(--muted)' });
      t.textContent = fmtSleepP(v);
    }
    const xt = el(svg, 'text', { x: PM.l + piw / 2, y: PH - 4, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--muted)' });
    xt.textContent = 'average sleep per night →';
    const yt = el(svg, 'text', { x: 12, y: PM.t + pih / 2, 'font-size': 11, fill: 'var(--muted)', 'text-anchor': 'middle', transform: `rotate(-90 12 ${PM.t + pih / 2})` });
    yt.textContent = '4-day consistency →';

    const hex = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
    const cA = hex('#35507a'), cB = hex('#00e0ff');
    const lerp = t => 'rgb(' + cA.map((a, k) => Math.round(a + (cB[k] - a) * t)).join(',') + ')';
    const nP = pts.length;
    const segs = [];
    for (let i = 1; i < nP; i++) {
      segs.push(el(svg, 'line', { x1: px(pts[i - 1].x), y1: py(pts[i - 1].y), x2: px(pts[i].x), y2: py(pts[i].y),
        stroke: lerp(i / (nP - 1)), 'stroke-width': 2, 'stroke-linecap': 'round', opacity: 0 }));
    }
    const yearMarks = [];
    pts.forEach((p, i) => {
      if (p.m.endsWith('-01')) {
        const g = el(svg, 'g', { opacity: 0 });
        el(g, 'circle', { cx: px(p.x), cy: py(p.y), r: 3, fill: lerp(i / (nP - 1)), stroke: 'var(--surface)', 'stroke-width': 1.5 });
        const t = el(g, 'text', { x: px(p.x) + 6, y: py(p.y) - 6, 'font-size': 10, fill: 'var(--muted)' });
        t.textContent = p.m.slice(0, 4);
        yearMarks.push({ i, g });
      }
    });
    const head = el(svg, 'circle', { r: 5.5, fill: '#00e0ff', stroke: 'var(--surface)', 'stroke-width': 2 });

    const tip = document.createElement('div'); tip.className = 'trend-tip';
    const tm = document.createElement('div'); tm.className = 'tm';
    const tv = document.createElement('div'); tv.className = 'tv';
    tip.append(tm, tv); box.appendChild(tip);
    box.addEventListener('pointermove', ev => {
      const rect = box.getBoundingClientRect();
      const mx = (ev.clientX - rect.left) / rect.width * PW, my = (ev.clientY - rect.top) / rect.height * PH;
      let best = -1, bd = 1e9;
      pts.forEach((p, i) => { const dd = (px(p.x) - mx) ** 2 + (py(p.y) - my) ** 2; if (dd < bd) { bd = dd; best = i; } });
      if (best >= 0 && bd < 900) {
        const p = pts[best];
        tm.textContent = monthLabel(p.m);
        tv.textContent = fmtSleepP(p.x) + ' · consistency ' + p.y.toFixed(0);
        tip.style.display = 'block';
        const cx = px(p.x) / PW * rect.width;
        tip.style.left = (cx > rect.width * 0.6 ? cx - tip.offsetWidth - 12 : cx + 12) + 'px';
        tip.style.top = (py(p.y) / PH * rect.height - 42) + 'px';
      } else tip.style.display = 'none';
    });
    box.addEventListener('pointerleave', () => { tip.style.display = 'none'; });

    const playBtn = document.getElementById('phase-play');
    const scrub = document.getElementById('phase-scrub');
    const when = document.getElementById('phase-when');
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const DURATION = 12000;
    let progress = 0, raf = null, lastTs = null;
    const draw = () => {
      const t = progress * (nP - 1);
      segs.forEach((s, i) => s.setAttribute('opacity', i + 1 <= t ? 1 : 0));
      yearMarks.forEach(m => m.g.setAttribute('opacity', m.i <= t ? 1 : 0));
      const i0 = Math.min(Math.floor(t), nP - 2), f = t - i0;
      head.setAttribute('cx', px(pts[i0].x + (pts[i0 + 1].x - pts[i0].x) * f));
      head.setAttribute('cy', py(pts[i0].y + (pts[i0 + 1].y - pts[i0].y) * f));
      when.textContent = monthLabel(pts[Math.round(t)].m);
      scrub.value = progress;
    };
    const stop = () => { if (raf) cancelAnimationFrame(raf); raf = null; lastTs = null; playBtn.textContent = progress >= 1 ? '↻ Replay' : '▶ Play'; };
    const tick = ts => {
      if (lastTs != null) { progress = Math.min(1, progress + (ts - lastTs) / DURATION); draw(); if (progress >= 1) { stop(); return; } }
      lastTs = ts; raf = requestAnimationFrame(tick);
    };
    const play = () => { if (progress >= 1) progress = 0; playBtn.textContent = '❚❚ Pause'; raf = requestAnimationFrame(tick); };
    playBtn.addEventListener('click', () => (raf ? stop() : play()));
    scrub.addEventListener('input', () => { stop(); progress = +scrub.value; draw(); });
    if (reduced) { progress = 1; draw(); playBtn.textContent = '↻ Replay'; }
    else {
      progress = 0; draw();
      const io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) { io.disconnect(); play(); } }, { threshold: 0.4 });
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

    html = HTML_TEMPLATE.format(
        data_js=data_js,
        trends_js=trends_js,
        trends_css=TRENDS_CSS,
        trends_panel=TRENDS_PANEL,
        trends_script=TRENDS_SCRIPT,
    )
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
