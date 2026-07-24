#!/usr/bin/env python3
"""
garmin_db.py  –  Build and maintain a local SQLite daily-metrics database
                 pulled from Garmin Connect.

MODES
-----
  login           Authenticate with Garmin Connect and save a token cache to
                  .garth/ so subsequent runs don't need your password.
                  Usage: python garmin_db.py login

  build           Pull every available day from Garmin Connect (full history).
                  Starts from GARMIN_START_DATE in .env (default 2010-01-01).
                  Usage: python garmin_db.py build

  update          Pull only the last N days (default 7) to catch any
                  retroactive edits, then append any new days since the
                  most-recent row in the DB.
                  Usage: python garmin_db.py update [--days 14]

  stats           Print a quick summary to stdout.
                  Usage: python garmin_db.py stats

METRICS STORED (one row per calendar day)
-----------------------------------------
  Steps, distance, floors, active/rest calories, BMR calories
  Average / max / resting heart rate
  Stress (avg, max, rest, body battery start/end)
  Sleep (total, deep, light, REM, awake, score, avg SpO2, avg respiration)
  Weight / BMI (from weigh-ins)
  Avg blood oxygen (SpO2)
  HRV (overnight avg ms)
  Intensity minutes (moderate / vigorous)
  Hydration (goal / intake ml)
  Resting heart rate (dedicated endpoint, sometimes more accurate)

AUTH / .env
-----------
  USERNAME            Garmin Connect email
  PASSWORD            Garmin Connect password
  GARMIN_START_DATE   Earliest date to pull (YYYY-MM-DD, default 2010-01-01)
  DB_PATH             SQLite file path (default: garmin.db)
  GARMIN_TOKEN_STORE  Directory for cached OAuth tokens (default: .garth)

TOKEN CACHE
-----------
  After a successful `login` the library saves OAuth tokens to GARMIN_TOKEN_STORE
  (.garth/ by default).  Subsequent runs load from that cache without prompting
  for credentials.  If the cache is absent or expired the script falls back to
  USERNAME / PASSWORD from .env.  If MFA is required it will prompt interactively
  on the terminal.
"""

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    -- primary key
    date                        TEXT PRIMARY KEY,   -- YYYY-MM-DD (local calendar day)

    -- steps / activity
    total_steps                 INTEGER,
    step_goal                   INTEGER,
    total_distance_m            REAL,               -- metres (includes GPS activities)
    wellness_distance_m         REAL,               -- metres (step/walking distance only)
    floors_ascended             REAL,
    floors_descended            REAL,
    floors_ascended_goal        INTEGER,

    -- calories
    active_kilocalories         REAL,
    bmr_kilocalories            REAL,
    total_kilocalories          REAL,               -- active + BMR
    wellness_kilocalories_goal  REAL,

    -- heart rate
    avg_heart_rate              REAL,               -- bpm, computed from HR timeseries
    min_heart_rate              INTEGER,            -- bpm
    max_heart_rate              INTEGER,
    resting_heart_rate          INTEGER,            -- from user summary
    rhr_value                   INTEGER,            -- from dedicated RHR endpoint (sometimes different)
    last_7_days_avg_rhr         REAL,               -- 7-day rolling avg from RHR endpoint

    -- stress
    avg_stress_level            INTEGER,            -- 0-100
    max_stress_level            INTEGER,
    stress_duration_secs        INTEGER,            -- seconds in "stressed" state
    rest_stress_duration_secs   INTEGER,
    activity_stress_duration_secs INTEGER,
    low_stress_duration_secs    INTEGER,
    medium_stress_duration_secs INTEGER,
    high_stress_duration_secs   INTEGER,

    -- body battery
    body_battery_charged        INTEGER,
    body_battery_drained        INTEGER,
    body_battery_highest        INTEGER,
    body_battery_lowest         INTEGER,
    body_battery_most_recent    INTEGER,

    -- sleep
    sleep_start                 TEXT,               -- ISO local datetime
    sleep_end                   TEXT,
    sleep_total_seconds         INTEGER,
    sleep_deep_seconds          INTEGER,
    sleep_light_seconds         INTEGER,
    sleep_rem_seconds           INTEGER,
    sleep_awake_seconds         INTEGER,
    sleep_score                 REAL,               -- 0-100
    sleep_avg_spo2              REAL,               -- % blood oxygen during sleep
    sleep_avg_respiration       REAL,               -- breaths / min
    sleep_hrv_avg               REAL,               -- ms (overnight avg from sleep)

    -- HRV (dedicated endpoint)
    hrv_weekly_avg              REAL,               -- ms
    hrv_last_night_avg          REAL,               -- ms
    hrv_last_night_5_min_high   REAL,               -- ms
    hrv_status                  TEXT,               -- e.g. "BALANCED"

    -- SpO2 / respiration (daily)
    spo2_avg                    REAL,               -- %
    spo2_min                    REAL,
    respiration_avg             REAL,               -- breaths / min
    respiration_min             REAL,
    respiration_max             REAL,

    -- weight / body composition
    weight_kg                   REAL,
    bmi                         REAL,
    body_fat_pct                REAL,
    body_water_pct              REAL,
    muscle_mass_kg              REAL,
    bone_mass_kg                REAL,

    -- intensity minutes
    moderate_intensity_mins     INTEGER,
    vigorous_intensity_mins     INTEGER,
    intensity_goal_mins         INTEGER,

    -- hydration
    hydration_goal_ml           REAL,
    hydration_intake_ml         REAL,

    -- derived from the all-day HR timeseries (see hr_timeseries table)
    hr_zone1_secs               INTEGER,            -- 50-60% of HR max
    hr_zone2_secs               INTEGER,            -- 60-70%
    hr_zone3_secs               INTEGER,            -- 70-80%
    hr_zone4_secs               INTEGER,            -- 80-90%
    hr_zone5_secs               INTEGER,            -- 90%+
    trimp                       REAL,               -- Banister TRIMP over the whole day
    strain                      REAL,               -- 0-21 log-scaled TRIMP (Whoop-like)

    -- metadata
    fetched_at                  TEXT                -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Raw all-day wellness heart-rate samples (~2-minute resolution)
CREATE TABLE IF NOT EXISTS hr_timeseries (
    date    TEXT NOT NULL,      -- YYYY-MM-DD calendar day (joins to daily.date)
    ts      INTEGER NOT NULL,   -- sample time, unix seconds UTC
    hr      INTEGER NOT NULL,   -- bpm
    PRIMARY KEY (date, ts)
) WITHOUT ROWID;

-- Overnight 5-minute HRV readings
CREATE TABLE IF NOT EXISTS hrv_readings (
    date    TEXT NOT NULL,      -- wake-up day (joins to daily.date)
    ts      TEXT NOT NULL,      -- reading time, ISO UTC
    hrv_ms  INTEGER NOT NULL,
    PRIMARY KEY (date, ts)
) WITHOUT ROWID;

-- Recorded activities (runs, rides, …) — one row per activity
CREATE TABLE IF NOT EXISTS activities (
    activity_id      INTEGER PRIMARY KEY,
    start_time_local TEXT,
    activity_type    TEXT,               -- e.g. running, cycling, lap_swimming
    name             TEXT,
    distance_m       REAL,
    duration_secs    REAL,
    avg_hr           INTEGER,
    max_hr           INTEGER,
    calories         REAL,
    elevation_gain_m REAL,
    fetched_at       TEXT
);

-- Sleep stage segments (one row per contiguous stage interval)
CREATE TABLE IF NOT EXISTS sleep_segments (
    date        TEXT NOT NULL,  -- wake-up day (joins to daily.date)
    seq         INTEGER NOT NULL,
    start_time  TEXT,           -- ISO UTC
    end_time    TEXT,           -- ISO UTC
    stage       TEXT,           -- deep / light / rem / awake
    PRIMARY KEY (date, seq)
) WITHOUT ROWID;
"""

# Columns added after the initial release; open_db() back-fills them onto
# existing databases with ALTER TABLE.
DAILY_MIGRATIONS = [
    ("hr_zone1_secs", "INTEGER"),
    ("hr_zone2_secs", "INTEGER"),
    ("hr_zone3_secs", "INTEGER"),
    ("hr_zone4_secs", "INTEGER"),
    ("hr_zone5_secs", "INTEGER"),
    ("trimp", "REAL"),
    ("strain", "REAL"),
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def open_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    existing = {r[1] for r in con.execute("PRAGMA table_info(daily)").fetchall()}
    for col, typ in DAILY_MIGRATIONS:
        if col not in existing:
            con.execute(f"ALTER TABLE daily ADD COLUMN {col} {typ}")
    con.commit()
    return con


def upsert_day(con: sqlite3.Connection, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join("?" * len(cols))
    col_names = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "date")
    sql = (f"INSERT INTO daily ({col_names}) VALUES ({placeholders}) "
           f"ON CONFLICT(date) DO UPDATE SET {updates}")
    con.execute(sql, list(row.values()))


def replace_day_rows(con: sqlite3.Connection, table: str, day: str,
                     rows: list) -> None:
    """Idempotently replace all child-table rows for one day."""
    con.execute(f"DELETE FROM {table} WHERE date=?", (day,))
    if rows:
        cols = list(rows[0].keys())
        sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        con.executemany(sql, [[r[c] for c in cols] for r in rows])


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value))


def get_meta(con: sqlite3.Connection, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def date_range(start: date, end: date):
    """Yield each date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Garmin login / client
# ---------------------------------------------------------------------------

def make_client(token_store: str, username: str, password: str) -> Garmin:
    """
    Create a Garmin client.  Tries token cache first; falls back to
    username/password (which may trigger an MFA prompt on the terminal).
    """
    from garminconnect import GarminConnectConnectionError as GarminConnError

    client = Garmin(email=username, password=password)
    ts_path = Path(token_store)

    if ts_path.exists() and any(ts_path.iterdir()):
        try:
            print(f"  Loading cached tokens from {token_store} …", flush=True)
            client.login(token_store)
            return client
        except Exception as exc:
            print(f"  Token cache invalid ({exc}); re-authenticating …",
                  flush=True)

    print("  Logging in with username / password …", flush=True)
    try:
        client.login()
    except GarminConnectAuthenticationError as exc:
        sys.exit(f"Authentication failed: {exc}\n\n"
                 "Tip: make sure USERNAME in .env is your full email address.")
    except GarminConnError as exc:
        msg = str(exc)
        if "429" in msg:
            sys.exit(
                f"Rate-limited by Garmin SSO (429).\n\n"
                "Wait 15-30 minutes and try again.\n"
                "Or run `python garmin_db.py login` first to pre-cache tokens."
            )
        sys.exit(f"Connection error: {exc}")

    # Persist tokens for next run
    ts_path.mkdir(parents=True, exist_ok=True)
    client.garth.dump(token_store)
    print(f"  Tokens saved to {token_store}", flush=True)
    return client


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def _f(d: Any, *keys, default=None):
    """Safe nested get from dict / list."""
    for k in keys:
        if d is None:
            return default
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int):
            try:
                d = d[k]
            except IndexError:
                return default
        else:
            return default
    return d if d is not None else default


def _i(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fl(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Derived HR metrics (time in zone, TRIMP, strain)
# ---------------------------------------------------------------------------

ZONE_WEIGHTS = (1, 2, 4, 8, 16)   # points per minute in Z1…Z5
STRAIN_K = 300.0                  # weighted-load scale for the 0-21 mapping
STAGE_NAMES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def hr_derived_metrics(samples: list, hr_rest: Optional[int],
                       hr_max: int) -> dict:
    """
    samples: list of (unix_seconds, bpm) sorted by time, covering the whole day.

    Zones are %-of-HR-max bands (Z1 50-60% … Z5 90%+).  TRIMP is Banister's
    formula on heart-rate reserve, accumulated across the day.  Strain follows
    the Whoop-patent shape: minutes in each zone earn exponentially increasing
    points (ZONE_WEIGHTS), and the weighted load saturates onto 0-21 via
    21·(1 − e^(−load/STRAIN_K)).  All inputs are kept raw in hr_timeseries,
    so these can be recomputed with different constants.
    """
    if not samples or not hr_rest or hr_max <= hr_rest:
        return {}
    zones = [0, 0, 0, 0, 0]
    trimp = 0.0
    for i, (ts, hr) in enumerate(samples):
        if i + 1 < len(samples):
            dt = min(samples[i + 1][0] - ts, 600)   # cap gaps at 10 min
        else:
            dt = 120                                # typical sample spacing
        if dt <= 0:
            continue
        pct = hr / hr_max
        if   pct >= 0.9: zones[4] += dt
        elif pct >= 0.8: zones[3] += dt
        elif pct >= 0.7: zones[2] += dt
        elif pct >= 0.6: zones[1] += dt
        elif pct >= 0.5: zones[0] += dt
        hrr = (hr - hr_rest) / (hr_max - hr_rest)
        if hrr > 0:
            trimp += (dt / 60) * hrr * 0.64 * math.exp(1.92 * hrr)
    load = sum(w * secs / 60 for w, secs in zip(ZONE_WEIGHTS, zones))
    return {
        "hr_zone1_secs": zones[0],
        "hr_zone2_secs": zones[1],
        "hr_zone3_secs": zones[2],
        "hr_zone4_secs": zones[3],
        "hr_zone5_secs": zones[4],
        "trimp": round(trimp, 1),
        "strain": round(21 * (1 - math.exp(-load / STRAIN_K)), 1),
    }


# ---------------------------------------------------------------------------
# Per-day fetch
# ---------------------------------------------------------------------------

def fetch_day(client: Garmin, d: date, verbose: bool = False) -> tuple:
    """
    Fetch all available metrics for a single calendar day.

    Returns (row, children): `row` upserts into `daily`; `children` maps
    child-table name → list of row dicts.  A child table only appears in
    `children` when its endpoint responded, so failed fetches never wipe
    previously stored rows.
    """
    ds = d.isoformat()          # "YYYY-MM-DD"
    row: dict[str, Any] = {
        "date": ds,
        "fetched_at": now_utc(),
    }
    children: dict[str, list] = {}

    def _safe(fn, *args, label=""):
        """Call fn(*args), return result or None on any error."""
        try:
            return fn(*args)
        except Exception as exc:
            if verbose:
                print(f"    [{label}] {exc}", flush=True)
            return None

    # ---- 1. User summary (steps, calories, floors, HR, stress) ----
    summary = _safe(client.get_user_summary, ds, label="user_summary")
    if summary:
        row["total_steps"]                  = _i(_f(summary, "totalSteps"))
        row["step_goal"]                    = _i(_f(summary, "dailyStepGoal"))
        row["total_distance_m"]             = _fl(_f(summary, "totalDistanceMeters"))
        row["wellness_distance_m"]          = _fl(_f(summary, "wellnessDistanceMeters"))
        row["floors_ascended"]              = _fl(_f(summary, "floorsAscended"))
        row["floors_descended"]             = _fl(_f(summary, "floorsDescended"))
        row["floors_ascended_goal"]         = _i(_f(summary, "floorsAscendedGoal"))
        row["active_kilocalories"]          = _fl(_f(summary, "activeKilocalories"))
        row["bmr_kilocalories"]             = _fl(_f(summary, "bmrKilocalories"))
        row["total_kilocalories"]           = _fl(_f(summary, "totalKilocalories"))
        row["wellness_kilocalories_goal"]   = _fl(_f(summary, "wellnessKilocaloriesGoal"))
        row["max_heart_rate"]               = _i(_f(summary, "maxHeartRate"))
        row["min_heart_rate"]               = _i(_f(summary, "minHeartRate"))
        row["resting_heart_rate"]           = _i(_f(summary, "restingHeartRate"))
        row["avg_stress_level"]             = _i(_f(summary, "averageStressLevel"))
        row["max_stress_level"]             = _i(_f(summary, "maxStressLevel"))
        row["stress_duration_secs"]         = _i(_f(summary, "stressDuration"))
        row["rest_stress_duration_secs"]    = _i(_f(summary, "restStressDuration"))
        row["activity_stress_duration_secs"]= _i(_f(summary, "activityStressDuration"))
        row["low_stress_duration_secs"]     = _i(_f(summary, "lowStressDuration"))
        row["medium_stress_duration_secs"]  = _i(_f(summary, "mediumStressDuration"))
        row["high_stress_duration_secs"]    = _i(_f(summary, "highStressDuration"))
        row["body_battery_charged"]         = _i(_f(summary, "bodyBatteryChargedValue"))
        row["body_battery_drained"]         = _i(_f(summary, "bodyBatteryDrainedValue"))
        row["body_battery_highest"]         = _i(_f(summary, "bodyBatteryHighestValue"))
        row["body_battery_lowest"]          = _i(_f(summary, "bodyBatteryLowestValue"))
        row["body_battery_most_recent"]     = _i(_f(summary, "bodyBatteryMostRecentValue"))
        row["moderate_intensity_mins"]      = _i(_f(summary, "moderateIntensityMinutes"))
        row["vigorous_intensity_mins"]      = _i(_f(summary, "vigorousIntensityMinutes"))
        row["intensity_goal_mins"]          = _i(_f(summary, "intensityMinutesGoal"))
        row["hydration_goal_ml"]            = _fl(_f(summary, "hydrationGoal"))
        row["hydration_intake_ml"]          = _fl(_f(summary, "hydrationMeasurementUnit"))
        # hydration intake is separate endpoint; override placeholder
        row.pop("hydration_intake_ml", None)

    # ---- 2. Heart rate timeseries (stored raw; also avg HR + zones/strain) ----
    hr_data = _safe(client.get_heart_rates, ds, label="heart_rates")
    hr_samples: list[tuple[int, int]] = []
    if hr_data:
        for v in (hr_data.get("heartRateValues") or []):
            if v and v[0] is not None and v[1] is not None:
                hr_samples.append((int(v[0] / 1000), int(v[1])))
        hr_samples.sort()
        children["hr_timeseries"] = [
            {"date": ds, "ts": t, "hr": h} for t, h in hr_samples]
        if hr_samples:
            values = [h for _, h in hr_samples]
            row["avg_heart_rate"] = round(sum(values) / len(values), 1)

    # ---- 3. Sleep ----
    sleep = _safe(client.get_sleep_data, ds, label="sleep")
    if sleep:
        sd = _f(sleep, "dailySleepDTO") or {}
        row["sleep_start"]          = _f(sd, "sleepStartTimestampLocal")  # epoch ms → str later
        row["sleep_end"]            = _f(sd, "sleepEndTimestampLocal")
        # Garmin's *Local timestamps are epochs already shifted into local
        # wall-clock time, so read them as UTC — fromtimestamp() without tz
        # would apply the machine's UTC offset a second time.
        for k in ("sleep_start", "sleep_end"):
            v = row.get(k)
            if isinstance(v, (int, float)) and v:
                row[k] = (datetime.fromtimestamp(v / 1000, tz=timezone.utc)
                          .replace(tzinfo=None).isoformat())
        row["sleep_total_seconds"]  = _i(_f(sd, "sleepTimeSeconds"))
        row["sleep_deep_seconds"]   = _i(_f(sd, "deepSleepSeconds"))
        row["sleep_light_seconds"]  = _i(_f(sd, "lightSleepSeconds"))
        row["sleep_rem_seconds"]    = _i(_f(sd, "remSleepSeconds"))
        row["sleep_awake_seconds"]  = _i(_f(sd, "awakeSleepSeconds"))
        row["sleep_score"]          = _fl(_f(sd, "sleepScores", "overall", "value"))
        # These keys have moved between dailySleepDTO and the payload root
        # across API versions — try both.
        row["sleep_avg_spo2"]       = (_fl(_f(sd, "averageSpO2Value"))
                                       or _fl(_f(sleep, "wellnessSpO2SleepSummaryDTO", "averageSPO2"))
                                       or _fl(_f(sleep, "wellnessSpO2SleepSummaryDTO", "averageSpO2")))
        row["sleep_avg_respiration"]= (_fl(_f(sd, "averageRespirationValue"))
                                       or _fl(_f(sleep, "avgSleepRespirationValue")))
        row["sleep_hrv_avg"]        = (_fl(_f(sd, "avgOvernightHrv"))
                                       or _fl(_f(sleep, "avgOvernightHrv")))
        segments = _f(sleep, "sleepLevels") or []
        children["sleep_segments"] = [
            {"date": ds, "seq": i,
             "start_time": _f(seg, "startGMT"),
             "end_time": _f(seg, "endGMT"),
             "stage": STAGE_NAMES.get(_i(_f(seg, "activityLevel")),
                                      str(_f(seg, "activityLevel")))}
            for i, seg in enumerate(segments)]

    # ---- 4. Resting heart rate (dedicated endpoint) ----
    rhr = _safe(client.get_rhr_day, ds, label="rhr")
    if rhr:
        val = _f(rhr, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
        if isinstance(val, list) and val:
            row["rhr_value"]           = _i(_f(val, 0, "value"))
        row["last_7_days_avg_rhr"] = _fl(_f(rhr, "allMetrics", "metricsMap",
                                            "WELLNESS_RESTING_HEART_RATE_7_DAYS_AVG",
                                            0, "value"))

    # ---- 5. HRV ----
    hrv = _safe(client.get_hrv_data, ds, label="hrv")
    if hrv:
        summary_hrv = _f(hrv, "hrvSummary") or {}
        row["hrv_weekly_avg"]           = _fl(_f(summary_hrv, "weeklyAvg"))
        row["hrv_last_night_avg"]       = _fl(_f(summary_hrv, "lastNightAvg"))
        row["hrv_last_night_5_min_high"]= _fl(_f(summary_hrv, "lastNight5MinHigh"))
        row["hrv_status"]               = _f(summary_hrv, "status")
        children["hrv_readings"] = [
            {"date": ds, "ts": _f(r, "readingTimeGMT"),
             "hrv_ms": _i(_f(r, "hrvValue"))}
            for r in (_f(hrv, "hrvReadings") or [])
            if _f(r, "readingTimeGMT") and _i(_f(r, "hrvValue")) is not None]

    # ---- 6. SpO2 ----
    spo2 = _safe(client.get_spo2_data, ds, label="spo2")
    if spo2:
        row["spo2_avg"] = _fl(_f(spo2, "averageSpO2"))
        row["spo2_min"] = _fl(_f(spo2, "lowestSpO2"))

    # ---- 7. Respiration ----
    resp = _safe(client.get_respiration_data, ds, label="respiration")
    if resp:
        row["respiration_avg"] = _fl(_f(resp, "avgWakingRespirationValue"))
        row["respiration_min"] = _fl(_f(resp, "lowestRespirationValue"))
        row["respiration_max"] = _fl(_f(resp, "highestRespirationValue"))

    # ---- 8. Weight / body composition ----
    weigh = _safe(client.get_daily_weigh_ins, ds, label="weigh_ins")
    if weigh:
        entries = _f(weigh, "dateWeightList") or []
        if entries:
            # Use the most-recent entry for the day
            entry = entries[0]
            row["weight_kg"]      = _fl(_f(entry, "weight"))
            if row.get("weight_kg") is not None:
                row["weight_kg"] = row["weight_kg"] / 1000  # Garmin returns grams
            row["bmi"]            = _fl(_f(entry, "bmi"))
            row["body_fat_pct"]   = _fl(_f(entry, "bodyFat"))
            row["body_water_pct"] = _fl(_f(entry, "bodyWater"))
            row["muscle_mass_kg"] = _fl(_f(entry, "muscleMass"))
            if row.get("muscle_mass_kg"):
                row["muscle_mass_kg"] = row["muscle_mass_kg"] / 1000
            row["bone_mass_kg"]   = _fl(_f(entry, "boneMass"))
            if row.get("bone_mass_kg"):
                row["bone_mass_kg"] = row["bone_mass_kg"] / 1000

    # ---- 9. Hydration ----
    hydration = _safe(client.get_hydration_data, ds, label="hydration")
    if hydration:
        row["hydration_goal_ml"]   = _fl(_f(hydration, "goalInML"))
        row["hydration_intake_ml"] = _fl(_f(hydration, "totalIntakeInML"))

    # ---- 10. Derived zone / TRIMP / strain from the HR series ----
    if hr_samples:
        hr_rest = row.get("resting_heart_rate") or row.get("rhr_value")
        hr_max = int(os.environ.get("GARMIN_HR_MAX", "202"))
        row.update(hr_derived_metrics(hr_samples, hr_rest, hr_max))

    return row, children


# ---------------------------------------------------------------------------
# Pull a range of dates
# ---------------------------------------------------------------------------

def pull_range(client: Garmin, con: sqlite3.Connection,
               start: date, end: date,
               delay: float = 0.5, verbose: bool = False) -> int:
    """
    Fetch and upsert every day from start to end (inclusive).
    Returns the number of days processed.
    """
    days = list(date_range(start, end))
    total = len(days)
    count = 0

    for i, d in enumerate(days, 1):
        label = f"[{i}/{total}] {d}"
        print(label, end="  ", flush=True)
        try:
            row, children = fetch_day(client, d, verbose=verbose)
            upsert_day(con, row)
            for table, rows in children.items():
                replace_day_rows(con, table, row["date"], rows)
            count += 1
            # Summarise what we got
            summary_bits = []
            if row.get("total_steps") is not None:
                summary_bits.append(f"steps={row['total_steps']}")
            if row.get("resting_heart_rate") is not None:
                summary_bits.append(f"rhr={row['resting_heart_rate']}")
            if row.get("weight_kg") is not None:
                summary_bits.append(f"weight={row['weight_kg']:.1f}kg")
            if row.get("sleep_total_seconds") is not None:
                hrs = row["sleep_total_seconds"] / 3600
                summary_bits.append(f"sleep={hrs:.1f}h")
            if row.get("strain") is not None:
                summary_bits.append(f"strain={row['strain']}")
            n_hr = len(children.get("hr_timeseries") or [])
            if n_hr:
                summary_bits.append(f"hr×{n_hr}")
            print("  ".join(summary_bits) or "(no data)", flush=True)
        except Exception as exc:
            print(f"ERROR: {exc}", flush=True)

        if i % 50 == 0:
            con.commit()
        if i < total:
            time.sleep(delay)

    con.commit()
    return count


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def do_login(token_store: str, username: str, password: str) -> None:
    """Authenticate and save tokens."""
    from garminconnect import GarminConnectConnectionError as GarminConnError

    client = Garmin(email=username, password=password)
    print("Logging in to Garmin Connect …", flush=True)
    try:
        client.login()
    except GarminConnectAuthenticationError as exc:
        sys.exit(f"Authentication failed: {exc}\n\n"
                 "Tip: make sure USERNAME in .env is your full email address.")
    except GarminConnError as exc:
        msg = str(exc)
        if "429" in msg:
            sys.exit(
                f"Rate-limited by Garmin (429): {exc}\n\n"
                "Garmin's SSO aggressively rate-limits login attempts.\n"
                "Wait 15-30 minutes and try again, or log in via the website\n"
                "first (which sometimes clears the lock), then retry."
            )
        sys.exit(f"Connection error: {exc}")
    ts_path = Path(token_store)
    ts_path.mkdir(parents=True, exist_ok=True)
    client.garth.dump(token_store)
    name = client.get_full_name()
    print(f"Logged in as: {name}")
    print(f"Tokens saved to {token_store}")


def do_build(client: Garmin, con: sqlite3.Connection,
             start_date: str, verbose: bool = False,
             delay: float = 0.5) -> None:
    start = date.fromisoformat(start_date)
    end   = date.today()
    print(f"Pulling full history from {start} → {end} …", flush=True)
    print("(This will take a while; each day makes ~8 API calls.)\n", flush=True)
    n = pull_range(client, con, start, end, delay=delay, verbose=verbose)
    set_meta(con, "last_full_build", now_utc())
    set_meta(con, "build_start_date", start_date)
    con.commit()
    print(f"\nDone. {n} days written.")


def do_update(client: Garmin, con: sqlite3.Connection,
              lookback_days: int = 7, verbose: bool = False,
              delay: float = 0.5) -> None:
    """
    Re-pull the last `lookback_days` days (to catch edits), then pull
    any days newer than the most-recent row in the DB.
    """
    today = date.today()

    # Re-pull recent window
    window_start = today - timedelta(days=lookback_days - 1)
    print(f"Re-pulling last {lookback_days} days ({window_start} → {today}) …",
          flush=True)
    pull_range(client, con, window_start, today, delay=delay, verbose=verbose)

    set_meta(con, "last_update", now_utc())
    con.commit()
    print(f"\nUpdate complete.")


def do_activities(client: Garmin, con: sqlite3.Connection,
                  delay: float = 1.0) -> None:
    """
    Pull the full activity list (newest first, 100 per page) and upsert into
    the activities table.  Idempotent; re-running refreshes everything.
    """
    page_size = 100
    start = 0
    total = 0
    while True:
        batch = client.get_activities(start, page_size)
        if not batch:
            break
        for a in batch:
            con.execute(
                """INSERT INTO activities
                   (activity_id, start_time_local, activity_type, name,
                    distance_m, duration_secs, avg_hr, max_hr, calories,
                    elevation_gain_m, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(activity_id) DO UPDATE SET
                     start_time_local=excluded.start_time_local,
                     activity_type=excluded.activity_type,
                     name=excluded.name,
                     distance_m=excluded.distance_m,
                     duration_secs=excluded.duration_secs,
                     avg_hr=excluded.avg_hr,
                     max_hr=excluded.max_hr,
                     calories=excluded.calories,
                     elevation_gain_m=excluded.elevation_gain_m,
                     fetched_at=excluded.fetched_at""",
                (_i(_f(a, "activityId")),
                 _f(a, "startTimeLocal"),
                 _f(a, "activityType", "typeKey"),
                 _f(a, "activityName"),
                 _fl(_f(a, "distance")),
                 _fl(_f(a, "duration")),
                 _i(_f(a, "averageHR")),
                 _i(_f(a, "maxHR")),
                 _fl(_f(a, "calories")),
                 _fl(_f(a, "elevationGain")),
                 now_utc()))
        total += len(batch)
        con.commit()
        print(f"  fetched {total} activities "
              f"(oldest so far: {_f(batch[-1], 'startTimeLocal')})", flush=True)
        if len(batch) < page_size:
            break
        start += page_size
        time.sleep(delay)
    print(f"Done. {total} activities stored.")


def do_recompute(con: sqlite3.Connection) -> None:
    """
    Recompute zone / TRIMP / strain columns for every day that has raw
    hr_timeseries rows.  Pure local computation — no API calls.  Useful after
    changing ZONE_WEIGHTS / STRAIN_K / GARMIN_HR_MAX.
    """
    hr_max = int(os.environ.get("GARMIN_HR_MAX", "202"))
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM hr_timeseries ORDER BY date").fetchall()]
    print(f"Recomputing derived HR metrics for {len(days)} days "
          f"(HR max = {hr_max}) …", flush=True)
    n = 0
    for day in days:
        samples = [(r[0], r[1]) for r in con.execute(
            "SELECT ts, hr FROM hr_timeseries WHERE date=? ORDER BY ts",
            (day,)).fetchall()]
        rest = con.execute(
            "SELECT COALESCE(resting_heart_rate, rhr_value) FROM daily "
            "WHERE date=?", (day,)).fetchone()
        metrics = hr_derived_metrics(samples, rest[0] if rest else None, hr_max)
        if metrics:
            sets = ", ".join(f"{k}=?" for k in metrics)
            con.execute(f"UPDATE daily SET {sets} WHERE date=?",
                        list(metrics.values()) + [day])
            n += 1
        if n % 500 == 0:
            con.commit()
    con.commit()
    print(f"Done. {n} days recomputed.")


def do_stats(db_path: str) -> None:
    if not Path(db_path).exists():
        sys.exit(f"Database not found: {db_path}  (run build first)")

    con = open_db(db_path)

    total = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    print(f"\n{'='*60}")
    print(f"  Garmin Daily Database  —  {db_path}")
    print(f"{'='*60}")
    print(f"  Total days: {total}")
    last_update = (get_meta(con, "last_update")
                   or get_meta(con, "last_full_build"))
    print(f"  Last sync:  {last_update or 'never'}")

    rng = con.execute("SELECT MIN(date), MAX(date) FROM daily").fetchone()
    print(f"  Date range: {rng[0] or '?'} → {rng[1] or '?'}")

    # Days with steps data
    with_steps = con.execute(
        "SELECT COUNT(*) FROM daily WHERE total_steps IS NOT NULL"
    ).fetchone()[0]
    print(f"  Days with steps data: {with_steps}")

    print()
    print("── Steps (last 30 days with data) ────────────────────────")
    rows = con.execute("""
        SELECT date, total_steps, total_distance_m
        FROM daily
        WHERE total_steps IS NOT NULL
        ORDER BY date DESC
        LIMIT 30
    """).fetchall()
    for r in rows:
        dist_km = (r[2] or 0) / 1000
        print(f"  {r[0]}   {(r[1] or 0):>8,} steps   {dist_km:>5.1f} km")

    print()
    print("── Sleep (last 14 days with data) ────────────────────────")
    rows = con.execute("""
        SELECT date, sleep_total_seconds, sleep_deep_seconds,
               sleep_rem_seconds, sleep_score
        FROM daily
        WHERE sleep_total_seconds IS NOT NULL
        ORDER BY date DESC
        LIMIT 14
    """).fetchall()
    for r in rows:
        total_h = (r[1] or 0) / 3600
        deep_h  = (r[2] or 0) / 3600
        rem_h   = (r[3] or 0) / 3600
        score   = f"{r[4]:.0f}" if r[4] else "—"
        print(f"  {r[0]}   {total_h:.1f}h total  "
              f"deep={deep_h:.1f}h  rem={rem_h:.1f}h  score={score}")

    print()
    print("── Weight (last 10 weigh-ins) ────────────────────────────")
    rows = con.execute("""
        SELECT date, weight_kg, bmi, body_fat_pct
        FROM daily
        WHERE weight_kg IS NOT NULL
        ORDER BY date DESC
        LIMIT 10
    """).fetchall()
    if rows:
        for r in rows:
            bmi  = f"{r[2]:.1f}" if r[2] else "—"
            fat  = f"{r[3]:.1f}%" if r[3] else "—"
            print(f"  {r[0]}   {r[1]:.1f} kg   BMI={bmi}   fat={fat}")
    else:
        print("  (no weigh-in data)")

    print()
    print("── Heart rate (last 14 days) ─────────────────────────────")
    rows = con.execute("""
        SELECT date, resting_heart_rate, avg_heart_rate, max_heart_rate, hrv_last_night_avg
        FROM daily
        WHERE resting_heart_rate IS NOT NULL OR avg_heart_rate IS NOT NULL
        ORDER BY date DESC
        LIMIT 14
    """).fetchall()
    for r in rows:
        hrv = f"hrv={r[4]:.0f}ms" if r[4] else ""
        print(f"  {r[0]}   rhr={r[1] or '—'}   "
              f"avg={r[2] or '—'}   max={r[3] or '—'}   {hrv}")

    print()
    print("── Monthly step averages (last 12 months) ────────────────")
    rows = con.execute("""
        SELECT strftime('%Y-%m', date)   AS month,
               COUNT(*)                  AS days,
               ROUND(AVG(total_steps))   AS avg_steps,
               SUM(total_steps)          AS total_steps
        FROM daily
        WHERE total_steps IS NOT NULL
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """).fetchall()
    for r in rows:
        print(f"  {r[0]}   {r[1]:>2} days   "
              f"avg={int(r[2] or 0):>7,}   total={int(r[3] or 0):>9,}")

    print(f"\n{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build / update a local SQLite daily-metrics DB from Garmin Connect.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "mode",
        choices=["login", "build", "update", "stats", "recompute",
                 "activities"],
        help="Operation to perform",
    )
    parser.add_argument("--db", default=None, help="Override DB_PATH from .env")
    parser.add_argument("--days", type=int, default=7,
                        help="(update) Days to re-pull for edits (default: 7)")
    parser.add_argument("--start", default=None,
                        help="(build) Override GARMIN_START_DATE, e.g. 2015-01-01")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds to sleep between days (default: 0.5; "
                             "raise it to be gentler on the API)")
    parser.add_argument("--env", default=".env",
                        help="Path to .env file (default: .env)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-endpoint errors")
    args = parser.parse_args()

    env_path = Path(args.env)
    load_dotenv(env_path, override=False)

    username    = os.environ.get("USERNAME", "")
    password    = os.environ.get("PASSWORD", "")
    db_path     = args.db or os.environ.get("DB_PATH", "garmin.db")
    token_store = os.environ.get("GARMIN_TOKEN_STORE", ".garth")
    start_date  = args.start or os.environ.get("GARMIN_START_DATE", "2010-01-01")

    # ---- login (no DB needed) ----
    if args.mode == "login":
        if not username or not password:
            sys.exit("Set USERNAME and PASSWORD in .env before running login.")
        do_login(token_store, username, password)
        return

    # ---- stats / recompute (no client needed) ----
    if args.mode == "stats":
        do_stats(db_path)
        return

    if args.mode == "recompute":
        do_recompute(open_db(db_path))
        return

    # ---- build / update need a client ----
    if not username or not password:
        sys.exit("Set USERNAME and PASSWORD in .env.")
    client = make_client(token_store, username, password)
    con    = open_db(db_path)

    if args.mode == "activities":
        do_activities(client, con, delay=max(args.delay, 1.0))

    elif args.mode == "build":
        do_build(client, con, start_date, verbose=args.verbose,
                 delay=args.delay)

    elif args.mode == "update":
        do_update(client, con, lookback_days=args.days, verbose=args.verbose,
                  delay=args.delay)


if __name__ == "__main__":
    main()
