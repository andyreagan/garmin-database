# garmin-database

A single-script tool that pulls your full Garmin Connect history into a local
SQLite database (`garmin.db`) checked into this repo.  Run it periodically to
keep the database up to date.

## Metrics stored (one row per calendar day)

| Category | Fields |
|---|---|
| **Steps / activity** | total steps, step goal, distance, floors up/down |
| **Calories** | active kcal, BMR kcal, total kcal, daily goal |
| **Heart rate** | avg HR, max HR, resting HR (summary + dedicated endpoint), 7-day rolling RHR avg |
| **Stress** | avg/max stress level, seconds in each stress zone, body battery charged/drained/high/low/current |
| **Sleep** | start/end time, total/deep/light/REM/awake seconds, sleep score, avg SpO2, avg respiration rate, overnight HRV avg |
| **HRV** | weekly avg, last night avg, last night 5-min high, status label |
| **SpO2** | daily avg %, daily min % |
| **Respiration** | avg / min / max breaths per minute |
| **Weight / body comp** | weight (kg), BMI, body fat %, body water %, muscle mass (kg), bone mass (kg) |
| **Intensity minutes** | moderate mins, vigorous mins, daily goal |
| **Hydration** | goal (ml), total intake (ml) |

## Setup

```bash
# 1. Copy and fill in credentials
cp .env.example .env
# edit .env with your Garmin Connect username/password

# 2. Install dependencies (using uv)
uv sync
# or: pip install garminconnect python-dotenv

# 3. Authenticate (saves tokens to .garth/ so you only need to do this once)
python garmin_db.py login
# If your account has MFA enabled, you'll be prompted on the terminal.
```

## Usage

```bash
# First time – pull your entire history (may take 30-60 min for many years)
python garmin_db.py build

# Ongoing – re-pull the last 7 days and append any new days
python garmin_db.py update

# Re-pull more days to catch retroactive edits
python garmin_db.py update --days 30

# Pull only from a specific start date
python garmin_db.py build --start 2020-01-01

# Quick stats summary
python garmin_db.py stats

# Verbose mode (shows per-endpoint errors, useful for debugging)
python garmin_db.py build --verbose
```

## How it works

The script uses the [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
library, which is an unofficial Python wrapper around the same internal API that
the Garmin Connect website uses.  This is the most widely-used community
approach for reading your own data.

For each calendar day the script calls ~8 Garmin endpoints:

1. `get_user_summary` – steps, calories, floors, HR, stress, body battery, intensity minutes
2. `get_sleep_data` – sleep stages, score, SpO2/respiration during sleep, overnight HRV
3. `get_rhr_day` – dedicated resting heart rate
4. `get_hrv_data` – HRV summary
5. `get_spo2_data` – daily blood oxygen
6. `get_respiration_data` – daily respiration
7. `get_daily_weigh_ins` – body weight & composition
8. `get_hydration_data` – fluid intake

Results are stored with `INSERT … ON CONFLICT DO UPDATE` so re-running is safe
and idempotent.

## Auth / MFA

The `login` command authenticates once and caches OAuth tokens in `.garth/`
(gitignored).  Subsequent runs load from that cache — no password needed until
the tokens expire (typically 90 days).

If your account uses two-factor authentication (MFA / OTP), the first `login`
will pause and prompt you to enter the code on the terminal.

## Token expiry

If you see authentication errors after a few months, just run:

```bash
python garmin_db.py login
```

This will re-authenticate and refresh the cached tokens.

## Database

`garmin.db` is a SQLite file checked into this repo so you have a portable,
queryable record of your health data.  Query it with any SQLite tool:

```bash
sqlite3 garmin.db "SELECT date, total_steps, resting_heart_rate, weight_kg FROM daily ORDER BY date DESC LIMIT 14"
```
