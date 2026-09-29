# Blight Early-Warning System (Kinangop)

Final-year BSc ICS project, Strathmore University.
Student: Edgar Kareithi (164951). Supervisor: Juliet Kirui.

## What the system does

Forecasts potato late blight risk for one smallholder potato farm in Kinangop (Nyandarua, Kenya) from on-farm microclimate data, confirms visual symptoms with a CNN, fuses the two, and alerts the farmer by SMS.

The farmer grows the Shangi variety, prefers SMS, has a smartphone, and has weak network coverage at the plot. He will NOT act on an automated recommendation until it proves itself, so all output is framed as **decision support, never as instructions**.

## Components

1. **Sensor ingest**: ESP32 + SHT31 node (hardware comes LATER). Until then, `tools/simulate_node.py` replays real hourly weather into the same ingest endpoint using the exact JSON contract the firmware will use.
2. **Hutton Criteria risk (baseline)**:
   - A "Hutton day" = local calendar day (Africa/Nairobi) with min temperature >= 10.0 °C AND at least 6 hours with relative humidity >= 90%.
   - A "Hutton period" = two consecutive Hutton days (flagged on the second day).
   - Days with fewer than 20 of 24 hourly values are "insufficient" (null), never False.
   - Gaps are flagged, never interpolated.
   - **Missing data policy.** An unjudged day is still written as a row (`status='insufficient_data'` or `'no_data'`, `score=NULL`, `valid_hours` set), so a gap shows up as a gap in the history. 0.0 is never substituted for an unknown day: 0.0 claims conditions do not favour blight, which missing data cannot support, and the dashboard and the model would both read it as a safe day. Downstream: the dashboard shows "No reading" / "Incomplete data (N of 24 hours)", never "Low risk"; alerts never send a low-risk message from an unjudged day and never let one clear a standing warning; fusion falls back to the most recent `status='ok'` day within 3 days (recording which date) and otherwise uses the neutral prior r=0.5 and says so; the learned model drops unjudged target days instead of treating them as negatives, and carries a `gap_in_window` feature.
3. **Learned risk model**: forecasts whether Hutton conditions will occur in the next 24h / 48h at the plot. It forecasts infection-favourable WEATHER, not blight itself. Benchmarked against persistence, climatology and Hutton-on-forecast baselines.
4. **CNN**: MobileNetV2 transfer learning on the PlantVillage potato subset (classes: `early_blight`, `late_blight`, `healthy`), exported to TFLite, run SERVER-SIDE by the backend.
5. **Fusion**: the risk score r in [0,1] shifts the CNN's late-blight confidence threshold:
   `tau(r) = clip(tau0 - k*(r - 0.5), tau_min, tau_max)`.
   High risk lowers the bar, low risk raises it.
6. **SMS alerts** via Africa's Talking (sandbox during development).
7. **Flutter mobile app**: login, dashboard, leaf capture, diagnosis result, history, report symptom onset date.

## Stack

Python 3.11 or 3.12 · FastAPI · SQLAlchemy 2.x + Alembic · PostgreSQL · pydantic v2 · passlib[bcrypt] + PyJWT (auth) · pytest · TensorFlow/Keras (training in Google Colab) · scikit-learn · pandas · Africa's Talking Python SDK · Open-Meteo (weather) · Flutter (app).

Note: `bcrypt` is pinned to 4.0.1 because passlib 1.7.4 cannot use bcrypt 4.1+.

## Repo layout

Keep it FLAT; do not add nested sub-packages unless asked.

```
backend/   FastAPI app: app.py, config.py, db.py, models.py, schemas.py, auth.py, ids.py,
           ingest.py, hutton.py, risk_model.py, risk_job.py, cnn_service.py, fusion.py,
           alerts.py, sms.py, tests/
ml/        fetch_weather.py, build_features.py, train_risk.py, eval_risk.py, train_cnn.ipynb,
           eval_cnn.py, compare_sensor_vs_api.py, cost_threshold.py
tools/     simulate_node.py, seed.py
app/       Flutter project
firmware/  (later)
models/    cnn.tflite, labels.txt, risk_model_24h.joblib, risk_model_48h.joblib
data/      gitignored: raw weather, images, uploads
docs/      evaluation outputs (tables/figures) for Chapter 5
```

## Database

10 tables; string IDs with prefixes (e.g. `FRM-xxxx`, `RD-xxxx`).

- **farmers**(farmer_id PK, name, phone_number UNIQUE, password_hash, role ['farmer'|'admin'], farm_id, created_at)
- **sensor_nodes**(node_id PK, farmer_id FK, location, latitude, longitude, last_sync_time)
- **sensor_readings**(reading_id PK, node_id FK, temperature, humidity, leaf_wetness NULLABLE, timestamp; UNIQUE(node_id, timestamp))
- **weather_readings**(weather_id PK, latitude, longitude, source ['archive'|'historical_forecast'|'forecast'], timestamp, temperature, humidity, forecast_issued_at NULLABLE; UNIQUE(source, latitude, longitude, timestamp, forecast_issued_at))
- **risk_scores**(score_id PK, node_id FK, method ['hutton'|'learned'], data_source ['sensor'|'api'], horizon_hours [0|24|48], status ['ok'|'insufficient_data'|'no_data'], score FLOAT 0-1 NULLABLE, valid_hours INT NULL, hutton_day BOOL NULL, hutton_period BOOL NULL, model_version NULL, for_date DATE, computed_at)
- **leaf_images**(image_id PK, farmer_id FK, file_path, capture_date)
- **diagnosis_results**(result_id PK, image_id FK UNIQUE, disease_label, confidence, probabilities JSON, risk_score_used FLOAT, threshold_used FLOAT, risk_level ['low'|'medium'|'high'], late_blight_flagged BOOL, timestamp)
- **reading_contributions**(reading_id FK, result_id FK, composite PK)
- **alerts**(alert_id PK, node_id FK, result_id FK NULLABLE, risk_score_id FK NULLABLE, alert_type ['high_risk'|'diagnosis'|'low_risk'], message TEXT, sent_at, delivery_status, provider_message_id)
- **symptom_reports**(report_id PK, farmer_id FK, node_id FK, onset_date DATE, notes, reported_at)

All timestamps stored in UTC (timestamptz); convert to Africa/Nairobi only for daily aggregation and display.

## Authentication and access (FR i)

- `POST /auth/register` (name, phone_number, password, farm_id) → 201, role is always `farmer`; admins come from seeding. A duplicate phone number gives 409.
- `POST /auth/login` (phone_number, password) → JWT access token, expiry from `JWT_EXPIRE_MINUTES`. A wrong password and an unknown number give the same 401, so registered numbers cannot be probed.
- `GET /me` → the logged-in farmer. `GET /nodes`, `GET /nodes/{node_id}` → own nodes only. `GET /admin/farmers` → admin only (403 otherwise).
- Phone numbers are normalised to `+2547XXXXXXXX` / `+2541XXXXXXXX` on input, accepting `07…`, `01…`, `254…`, `+254…`, with or without spaces and dashes.
- **Ownership** is decided in one place, `auth.OWNER_LOOKUPS`, which records how each table reaches its owning farmer (a reading through its node, a diagnosis through its image). `auth.get_owned_or_404()` answers **404, not 403**, for someone else's record, so a stranger cannot confirm that an ID exists. Admins may read anything. A table with no ownership rule raises `TypeError` rather than being allowed through by default.

## Conventions

- Windows machine, PowerShell. Give PowerShell commands, not bash.
- Every module ships with pytest tests. Run the tests before saying something is done.
- Secrets and config come from a `.env` file (python-dotenv / pydantic-settings); never hard-code keys. Provide `.env.example`.
- Do not invent requirements. If this file and my prompt conflict, or something is ambiguous, ASK me before coding.
- Do not install system-level software (PostgreSQL, Docker, Flutter SDK) without asking me first.
- Keep code plain and readable. This is a student project that gets defended in a viva: favour clarity over cleverness, and comment the domain logic (Hutton, fusion) so I can explain it.
- Commit to git at the end of each task with a clear message.

## Sensor ingest (FR ii)

- `POST /nodes/{node_id}/readings` takes a **batch** (max 500) and authenticates with the node's own `X-Node-Key` header, never a farmer JWT. Replaying a batch is harmless: a duplicate `(node_id, timestamp)` is skipped via `INSERT … ON CONFLICT DO NOTHING` and counted as a duplicate, not an error. Out-of-order and old timestamps are accepted, since the node buffers while offline. Rows are validated one by one (temperature -10…50 °C, humidity 0…100%, timestamp no more than 10 minutes in the future, timezone required) and rejects are reported by position, so one bad reading does not cost the node its buffer. Response: `{"accepted": n, "duplicates": n, "rejected": [{"index": i, "reason": "…"}]}`. `last_sync_time` records when the node was last heard from, not the newest reading.
- `GET /nodes/{node_id}/readings?from=&to=` uses a farmer JWT and the ownership rule; `from` inclusive, `to` exclusive, ordered by time.
- Node keys are random strings stored only as bcrypt hashes. `tools/seed.py` prints a key once when it issues one, including for an existing node that has none (that is how a lost key is replaced).
- **The full contract for the firmware is in [docs/firmware_contract.md](docs/firmware_contract.md)**: headers, payload, limits, status codes, and the retry/buffer rules the ESP32 must follow.

## Weather fetching (ml/fetch_weather.py)

```powershell
.\.venv\Scripts\python.exe -m ml.fetch_weather --source archive --start 2015-01-01 --end 2026-09-20
.\.venv\Scripts\python.exe -m ml.fetch_weather --source archive --days 30   # shortcut
```

| Source | Endpoint | Coverage | Notes |
|---|---|---|---|
| `archive` | `archive-api.open-meteo.com/v1/archive` (ERA5) | 1940 → today−5 days | The long, consistent record; ~5-day publication lag, so it never covers this morning |
| `historical_forecast` | `historical-forecast-api.open-meteo.com/v1/forecast` | ~2022 → today | What the models predicted at the time; needed for the Hutton-on-forecast benchmark |

- Free, no API key (600 calls/min, 10,000/day). Fetched in yearly chunks with retry and backoff; 429/5xx are retried, a 400 is not.
- Re-running is safe: `ON CONFLICT DO NOTHING` on the weather uniqueness constraint, so a repeated range stores 0 rows.
- Hours the API returns as null are dropped, never interpolated; `hutton.py` then decides whether the day still has its 20 of 24 values.
- Raw responses are also written to `data/weather/*.csv` (gitignored) for inspection.
- **`forecast_issued_at` stays null.** The historical forecast API is a continuous series stitched from the first hours of successive model runs and exposes no per-hour issue time. A true fixed-lead-time forecast needs the Previous Runs API (`*_previous_day1/2`, archived from 2024) or the Single Runs API (`&run=`).

## Data preparation and documentation

```powershell
.\dev.ps1 prepare-images          # PlantVillage potato export + data/splits/*.csv
.\dev.ps1 register-field-images   # register the farmer's weekly photos
.\dev.ps1 weather-quality         # weather coverage / gap report
```

`.\dev.ps1` with no argument lists every command (test, serve, migrate, revision, seed, fetch-weather, and the three above).

| Document | What it holds |
|---|---|
| [docs/data_dictionary.md](docs/data_dictionary.md) | Every table and dataset: columns, units, meaning, source, timezone, and which script produces it |
| [docs/data_report.md](docs/data_report.md) | PlantVillage class counts per split, the class imbalance, integrity checks, licence and citations |
| [docs/data_quality.md](docs/data_quality.md) | Weather coverage, missing hours per month, out-of-range values, share of Hutton-ready days |
| [docs/firmware_contract.md](docs/firmware_contract.md) | The ingest contract the ESP32 must follow |

Notes on the image export:

- The split CSVs (`data/splits/*.csv`), the `data/field_images/` structure and its `metadata.csv` are **committed**; the images themselves are gitignored.
- Splits are 70/15/15, stratified per class, seed 42, so anyone can rebuild the exact same split.
- **Getting the images.** Mendeley, which hosts the PlantVillage archive, answers TFDS's downloader with HTTP 403 while serving the same file to a browser or curl, so the TFDS path cannot be relied on. Download the archive once and export from it:

  ```powershell
  New-Item -ItemType Directory -Force data\raw | Out-Null
  curl.exe -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36" `
    -o data\raw\plantvillage_mendeley.zip `
    "https://data.mendeley.com/public-files/datasets/tywbtsjrjv/files/d5652a28-c1d8-4b76-97f3-72fb80f94efc/file_downloaded"
  .\dev.ps1 prepare-images --from-archive data\raw\plantvillage_mendeley.zip
  ```

  Same CC0 source, and this path needs no TensorFlow. Only the non-augmented copy in the archive is used: the augmented images are rotations and colour shifts of the originals, and including them would put near-duplicates on both sides of the train/test split.
- The TFDS path (`prepare-images` with no `--from-archive`) is still there for when the download works, and needs `pip install -r requirements-ml.txt`. On Windows, TensorFlow's very long internal paths break that install inside a deeply nested project folder; either enable Windows long-path support or install it into a venv at a short path (e.g. `C:\blightml`) and run the script with that interpreter.

## Continuous integration

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs on every push to `main` and every pull request: it starts a PostgreSQL 16 service, applies the Alembic migrations, and runs the full pytest suite on Python 3.11.

## Setup

Requires Python 3.11 and PostgreSQL 15+ (developed on 18). All commands run from the repo root in PowerShell.

```powershell
# 1. Virtual environment and dependencies
& 'C:\Program Files\Python311\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Config: copy the template, then fill in the database password etc.
Copy-Item .env.example .env

# 3. Create / update the database tables
.\.venv\Scripts\alembic.exe upgrade head

# 4. Seed one admin, one farmer and one sensor node (safe to re-run)
.\.venv\Scripts\python.exe -m tools.seed

# 5. Run the tests (uses TEST_DATABASE_URL, never the real database)
.\.venv\Scripts\python.exe -m pytest

# 6. Start the API, then open http://127.0.0.1:8000/health
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --reload
```

The database user and the two databases (`blight_ews`, `blight_ews_test`) are created once by hand with `psql` as the `postgres` superuser.
