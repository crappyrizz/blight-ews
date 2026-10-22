# Blight Early-Warning System (Kinangop)

Final-year BSc ICS project, Strathmore University.
Student: Edgar Kareithi (164951). Supervisor: Juliet Kirui.

[![tests](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml)

## Summary

An early-warning system for potato late blight on one smallholder farm in Kinangop (Nyandarua, Kenya). A sensor node on the plot logs temperature and humidity; the backend turns those readings into a daily blight-weather risk (the Hutton Criteria, plus a learned 24 h / 48 h forecast); the farmer photographs a leaf in the Flutter app and a CNN on the server classifies it; the weather risk moves the CNN's late-blight threshold (fusion); and the farmer is told by SMS. Everything is framed as **decision support, never instructions**: the farmer has said he will not act on an unproven system, so the system explains itself and lets him decide.

```
ESP32 + SHT31 node ──POST /nodes/{id}/readings──▶  FastAPI backend  ◀──JWT──  Flutter app (Android)
 (simulator until the                              │  hutton.py      Hutton day / period per local day      │ capture leaf, dashboard,
  hardware arrives)                                │  risk_model.py  learned 24 h / 48 h forecast           │ diagnosis, history, report
                                                   │  risk_job.py    hourly: score every node, alert        │
 Open-Meteo ERA5-Land archive ──ml/fetch_weather──▶│  cnn_service.py TFLite CNN, server side ◀──POST /images─┘
                                                   │  fusion.py      tau(r): risk moves the late-blight bar
                                                   │  alerts.py      SMS policy ──▶ Africa's Talking ──▶ farmer's phone
                                                   └─ PostgreSQL (10 tables)
```

## Architecture

![System architecture (Figure 4.11)](docs/architecture.png)

*Figure 4.11: system architecture.* The four layers:

| Layer | What runs there | Code |
|---|---|---|
| Field | ESP32 + SHT31 node: samples, buffers offline, uploads batches with its own key | `docs/firmware_contract.md`; `tools/simulate_node.py` until the hardware exists |
| Backend | FastAPI API, the hourly risk job (APScheduler), the TFLite CNN, fusion, SMS | `backend/` |
| Data | PostgreSQL: readings, weather, risk scores, images, diagnoses, alerts, symptom reports | `backend/models.py`, `alembic/` |
| Farmer | Flutter app (Android) and SMS | `app/`, `backend/alerts.py` |

Training happens off the server: the CNN in Google Colab (`ml/train_cnn.ipynb`), the risk model on the PC (`ml/train_risk.py`). The backend only loads the saved models from `models/`.

## Setup on Windows

Everything runs from the repo root in PowerShell.

**Prerequisites:** Python 3.11; PostgreSQL 15 or newer (developed on 18; the weather table needs `NULLS NOT DISTINCT`, new in 15), from the EnterpriseDB Windows installer; Git. For the app only: Flutter SDK and Android Studio (see [Mobile app](#mobile-app-app-flutter-android)).

```powershell
# 1. PostgreSQL: one user and two databases (the second is only for the tests), as the postgres superuser
psql -U postgres -c "CREATE USER blight WITH PASSWORD 'choose-a-password';"
psql -U postgres -c "CREATE DATABASE blight_ews OWNER blight;"
psql -U postgres -c "CREATE DATABASE blight_ews_test OWNER blight;"

# 2. Virtual environment, dependencies, .env from .env.example, git hooks
.\dev.ps1 setup
#    Open .env and fill in: the database password in DATABASE_URL and TEST_DATABASE_URL,
#    JWT_SECRET (any long random string), AT_API_KEY (Africa's Talking sandbox key).
#    Never commit .env; a pre-commit hook refuses it.

# 3. Create / update the tables
.\dev.ps1 db-migrate

# 4. Seed one admin, one farmer and one sensor node (safe to re-run).
#    Passwords and the node key are printed ONCE: put the node's ID and key in .env as NODE_ID / NODE_KEY.
.\dev.ps1 seed

# 5. Weather history for the plot (ERA5-Land, 2015 to now), used by the risk model and the simulator
.\dev.ps1 fetch-weather --source archive --start 2015-01-01 --end 2026-10-12

# 6. Models: the learned risk model is trained locally; the CNN comes from Colab (see Model training)
.\.venv\Scripts\python.exe -m ml.train_risk
```

`.\dev.ps1` with no argument lists every command.

## Running

| What | Command | Notes |
|---|---|---|
| Backend API | `.\dev.ps1 serve` | http://127.0.0.1:8000/health and the API docs at `/docs`. The hourly risk job starts with it. For the phone, use `--host 0.0.0.0` (see the app section). |
| Simulated node (backfill) | `.\.venv\Scripts\python.exe -m tools.simulate_node --replay --start 2026-08-15 --end 2026-09-20 --speed max` | Replays archive weather through the ingest API, exactly as the firmware will (+4 % canopy RH, noise, outages). |
| Simulated node (live) | `.\.venv\Scripts\python.exe -m tools.simulate_node --live --start 2026-09-01 --every 5` | One reading every 5 s. |
| Risk job, once by hand | `.\.venv\Scripts\python.exe -m backend.risk_job --once [--date YYYY-MM-DD]` | Hutton + learned scores for every node and both sources, then the high-risk SMS check. |
| App on the phone | `cd app; flutter run` | Details, Wi-Fi vs USB, in [Mobile app](#mobile-app-app-flutter-android). |
| Real SMS | set `SMS_DRY_RUN=false` in `.env` | Default is a dry run (logged, not sent). Sandbox messages appear in the [Africa's Talking simulator](https://simulator.africastalking.com). |

**Firmware contract.** The node's API (headers, payload, limits, status codes, buffering and retry rules) is in [docs/firmware_contract.md](docs/firmware_contract.md). The simulator follows it exactly, so the real ESP32 can replace it without a server change.

## Exposing the API for devices

ESP32 nodes in the field, and nodes in the Wokwi simulator (whose free plan cannot reach `localhost`), need a public HTTPS address. For development, a **Cloudflare quick tunnel** gives the local backend one, with no account and no router changes:

```powershell
winget install --id Cloudflare.cloudflared --exact      # once (open a new PowerShell afterwards)

.\dev.ps1 serve                                           # window 1: the backend on 127.0.0.1:8000
cloudflared tunnel --url http://127.0.0.1:8000            # window 2: prints https://<random-words>.trycloudflare.com
```

- Devices then post to `https://<random-words>.trycloudflare.com/readings` with their `X-Device-Key` (see [docs/firmware_contract.md](docs/firmware_contract.md) §4a). The tunnel adds HTTPS; on the ESP32 use `WiFiClientSecure` (for a bench test, `setInsecure()` skips certificate checks).
- The URL is random and **changes every time** the tunnel restarts, so update the firmware or Wokwi sketch each time. Quick tunnels have no uptime guarantee and are for development only.
- While the tunnel runs, the whole API (including `/docs`) is reachable by anyone with the URL. Device keys and farmer logins still apply. Stop it with Ctrl + C when you are done.
- Test it from PowerShell (reads node-01's key from `device_keys.local.env`, written by `tools/register_device.py`):

  ```powershell
  $url  = "https://<random-words>.trycloudflare.com"
  $key  = (Select-String -Path device_keys.local.env -Pattern '^DEVICE_KEY_NODE_01=(.+)$').Matches[0].Groups[1].Value
  $body = @{ device_id = "node-01"; ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"); temp_c = 18.5; rh = 72.0 } | ConvertTo-Json -Compress
  $body | curl.exe -s -X POST "$url/readings" -H "Content-Type: application/json" -H "X-Device-Key: $key" --data-binary "@-"
  ```

  A good answer is `{"stored":1,"skipped":0,"rejected":[],"risk_level":"unknown"}`. (The body is piped in because Windows PowerShell 5.1 strips the double quotes out of a JSON string passed to `curl.exe -d`.)
- **The tunnel needs outbound port 7844** (QUIC or TCP) to reach Cloudflare. Some networks block it, including the mobile hotspot and the Strathmore student Wi-Fi this project was developed on: the log then shows `Allow outbound TCP on port 7844` and `dial tcp …:7844: i/o timeout`.
- **Fallback when 7844 is blocked: localhost.run over SSH** (port 22). It needs no install (Windows has `ssh`) and no account:

  ```powershell
  ssh -o ServerAliveInterval=30 -R 80:127.0.0.1:8000 nokey@localhost.run    # prints https://<random>.lhr.life
  ```

  Write `127.0.0.1`, not `localhost`: on Windows `localhost` can resolve to IPv6 `::1`, where uvicorn is not listening, and every request then answers `no tunnel`. The free URL is random and short-lived like Cloudflare's; the same `$url` test above works with it.

## Deployment (Render + Neon)

The demo server: the API as a free **Render** web service ([render.yaml](render.yaml)), the database as a free **Neon** Postgres (Render's free Postgres is deleted after 30 days; Neon's has no expiry). Both in Frankfurt. Devices and the app reach it at `https://<service>.onrender.com` over ordinary HTTPS (port 443), from any network.

**One-time setup**

1. **Neon** ([neon.com](https://neon.com)): create a project in *AWS Europe Central 1 (Frankfurt)*, Postgres 17. Copy the **direct** connection string (turn *Connection pooling* off: the backend sets the session timezone at connect time, which the pooled endpoint rejects) into `neon.local.env` in the repo root as `DATABASE_URL=postgresql://...`. `*.local.env` is gitignored.
2. **Schema and data**: create the tables on Neon, then copy every table across, so logins, node keys and the weather archive carry over:

   ```powershell
   $neon = (Select-String neon.local.env -Pattern '^DATABASE_URL=(.+)$').Matches[0].Groups[1].Value
   $env:DATABASE_URL = $neon; .\.venv\Scripts\alembic.exe upgrade head; Remove-Item Env:DATABASE_URL
   $pg = "C:\Program Files\PostgreSQL\18\bin"
   & "$pg\pg_dump.exe" --data-only --no-owner --no-privileges --exclude-table=alembic_version "postgresql://blight:<password>@localhost:5432/blight_ews" | & "$pg\psql.exe" -v ON_ERROR_STOP=1 $neon
   ```
3. **Render** ([render.com](https://render.com), sign in with GitHub): *New → Blueprint*, pick this repository; Render reads `render.yaml`. When asked, paste the Neon connection string into `DATABASE_URL` and the Africa's Talking sandbox key into `AT_API_KEY`. `JWT_SECRET` is generated. The build installs the requirements and downloads the models (`tools/fetch_models.py`, from the GitHub release `models-2026-10-12`, checked against `models/models.sha256`); the start command applies migrations and starts uvicorn.

After that, every merge to `main` redeploys.

**How the free tiers behave, and what that means here**

| | Behaviour | Consequence |
|---|---|---|
| Render web service | Sleeps after 15 min without requests; the next request wakes it in about a minute. 750 free hours a month. 512 MB RAM (the app uses about 225 MB). | A device's first upload after a quiet spell waits about a minute: give the ESP32 an HTTP timeout of at least 90 s. The hourly risk job runs at every start-up, so each wake re-scores the risk; while asleep, no job runs and no SMS goes out. |
| Render filesystem | Lost on every restart and redeploy. | Uploaded leaf photos are not kept; the diagnoses (in the database) are. |
| Neon Postgres | Pauses after about 5 idle minutes and resumes on the next query. About 100 compute-hours a month at the smallest size. | Uploading **hourly** (the firmware contract's default) keeps both inside the free allowance; a device posting every few minutes would keep Neon awake around the clock and use the month's hours in about two weeks. |

**Updating the models:** retrain, upload the files to a new GitHub release, and put its tag in `tools/fetch_models.py` and the new checksums in `models/models.sha256`.

## Tests and evaluations

```powershell
.\dev.ps1 test                                    # the whole pytest suite (uses TEST_DATABASE_URL, never the real database)
.\dev.ps1 test backend/tests/test_e2e.py          # end to end: seed → wet week → risk job → photo → fusion → SMS → cooldown
cd app; flutter analyze; flutter test; cd ..      # the app
.\.venv\Scripts\python.exe -m ml.run_all_evals    # regenerate every Chapter 5 table and figure in docs/
```

- **End-to-end test** ([backend/tests/test_e2e.py](backend/tests/test_e2e.py)): seeds the farm, replays a wet week through the simulator and the ingest API, runs the risk job (the plot turns high and the high-risk SMS goes out), uploads a late-blight leaf that fusion flags only because the risk is high, checks the diagnosis SMS was queued (dry run), and checks the 24 h cooldown. One test, readable top to bottom.
- **CI** runs lint and the whole suite against PostgreSQL 16 on every push and pull request (see [Continuous integration](#continuous-integration)).
- **Evaluation outputs**, all in `docs/`: [risk_eval.md](docs/risk_eval.md) (learned model vs baselines), [cnn_domain_gap.md](docs/cnn_domain_gap.md) (CNN, lab vs field), [fusion_eval.md](docs/fusion_eval.md), [sensor_vs_api.md](docs/sensor_vs_api.md), [cost_threshold.md](docs/cost_threshold.md), and the functional test plan [test_plan.md](docs/test_plan.md) (FR i–viii; the manual app checks are filled in from [manual_app_checklist.csv](docs/manual_app_checklist.csv)).

## Model training

- **CNN:** [open ml/train_cnn.ipynb in Google Colab](https://colab.research.google.com/github/crappyrizz/blight-ews/blob/main/ml/train_cnn.ipynb), choose a GPU runtime and *Runtime → Run all*. It rebuilds the committed PlantVillage split, trains MobileNetV2, and downloads `cnn_outputs.zip`; put `cnn.tflite` and `labels.txt` in `models/`. Details in [CNN training](#cnn-training-mltrain_cnnipynb-and-the-lab-to-field-check-mleval_cnnpy).
- **Learned risk model:** `.\.venv\Scripts\python.exe -m ml.train_risk`, then `-m ml.eval_risk`. Details in [Learned risk model](#learned-risk-model-mlbuild_featurespy-mltrain_riskpy-mleval_riskpy-backendrisk_modelpy).
- Model files are gitignored (`labels.txt` excepted); each script above recreates them.

## Known limitations

- **Simulated sensor data until the hardware exists.** Every "sensor" reading so far comes from `tools/simulate_node.py` replaying Open-Meteo weather with a canopy offset, not from the ESP32. Results built on it (the sensor-vs-API comparison, the dashboard) describe the simulator, and the reports say so in a banner. The readings stored before 12 Oct 2026 were replayed from ECMWF IFS data, before the archive was pinned to ERA5-Land.
- **The learned model forecasts Hutton conditions, not blight onset.** Its targets are Hutton days and periods computed from weather; there is no disease ground truth yet. The farmer's symptom-onset reports (`symptom_reports`) are the data that could link the two. The cost analysis treats a Hutton period as an infection, which overstates misses when no inoculum is present.
- **Very few field images.** The CNN scores 99.4 % on PlantVillage lab images (single leaves, plain backgrounds); no field photos from the farm are registered yet, so its accuracy on real field photos is unmeasured. It is overconfident on unusual photos (before the centre-crop fix, a squashed photo of an early-blight leaf on a laptop screen came out 100 % late blight), which is what fusion and the quality check guard against.
- **One farm, one node, one test year.** All risk numbers come from one location and one held-out year of weather.
- **Development settings.** The app talks to the API over plain HTTP on the local network (`network_security_config.xml`); production needs HTTPS. SMS uses the Africa's Talking sandbox.

---

# Reference

The sections below are the detailed design and the working conventions for this repository.

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
           eval_cnn.py, compare_sensor_vs_api.py, cost_threshold.py, make_test_plan.py,
           run_all_evals.py
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
- **sensor_nodes**(node_id PK, farmer_id FK, location, latitude, longitude, last_sync_time, api_key_hash, last_risk_level, created_at): the sensor devices
- **sensor_readings**(reading_id PK, node_id FK, temperature, humidity, leaf_wetness NULLABLE, timestamp; UNIQUE(node_id, timestamp))
- **weather_readings**(weather_id PK, latitude, longitude, source ['archive'|'historical_forecast'|'forecast'], timestamp, temperature, humidity, forecast_issued_at NULLABLE; UNIQUE(source, latitude, longitude, timestamp, forecast_issued_at))
- **risk_scores**(score_id PK, node_id FK, method ['hutton'|'learned'], data_source ['sensor'|'api'], horizon_hours [0|24|48], status ['ok'|'insufficient_data'|'no_data'], score FLOAT 0-1 NULLABLE, valid_hours INT NULL, hutton_day BOOL NULL, hutton_period BOOL NULL, model_version NULL, for_date DATE, computed_at)
- **leaf_images**(image_id PK, farmer_id FK, file_path, capture_date)
- **diagnosis_results**(result_id PK, image_id FK UNIQUE, disease_label, confidence, probabilities JSON, risk_score_used FLOAT, threshold_used FLOAT, risk_level ['low'|'medium'|'high'], late_blight_flagged BOOL, risk_basis ['learned_24h'|'hutton_today'|'hutton_fallback'|'neutral_prior'], risk_for_date DATE NULL, timestamp)
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

## Leaf images and the CNN (FR iv, v)

- `POST /images` (farmer JWT, multipart: `file`, optional `capture_date` with timezone) stores the photo under `data/uploads/<farmer_id>/<image_id>.<ext>`, adds a `leaf_images` row, runs the CNN and returns `{"image": {...}, "prediction": {"label", "confidence", "probabilities"}}`. It also fuses the prediction with the weather risk and returns the `diagnosis` (see Fusion below).
- **Quality check first.** A photo the model cannot judge fairly is refused with 422 and `{"reason", "message"}`, and nothing is stored, so the app can ask for a retake. Reasons: `unreadable`, `too_small` (short side < 224 px), `too_dark` (mean grey < 40), `too_blurry` (variance of the Laplacian < 50). The thresholds are named constants in `backend/cnn_service.py`, calibrated so that none of the 2,152 PlantVillage images is rejected; re-check them once field photos exist.
- `GET /images/{image_id}`: own images only (404 otherwise, like nodes).
- The model (`models/cnn.tflite`, `models/labels.txt`) is loaded once at startup. If it is missing, the API still runs and `POST /images` answers 503.
- Preprocessing mirrors training: RGB, EXIF rotation applied, the largest centred square cropped out (training images are square, so a phone photo is cropped rather than squashed; a square image is unchanged), 224×224 bilinear antialiased resize done in floating point (matches TensorFlow's to within 0.004 grey levels), then `x / 127.5 - 1`. The one unavoidable difference: training decoded JPEGs with TensorFlow's fast IDCT and Pillow uses the accurate one, so pixels can differ by up to 4/255 (about 1/255 on average).

## Risk job, dashboard, history and symptom reports

- **Hourly risk job** (`backend/risk_job.py`, APScheduler, started with the API; `RISK_JOB_ENABLED`, `RISK_JOB_INTERVAL_MINUTES`). For every node, from both `sensor` and `api` data, it upserts Hutton (horizon 0) and the learned 24 h / 48 h scores for today and the 2 days before (a node flushing its buffer late can complete them). Today stays `insufficient_data` until 20 hours exist; the `api` rows stay `no_data` for the ~6 days ERA5-Land lags. Learned rows use `for_date` = the day the forecast was made from (the 24 h row for D is about D+1). Run one pass by hand with `.\.venv\Scripts\python.exe -m backend.risk_job --once [--date YYYY-MM-DD]`. Run the API as a single process, so only one scheduler runs (the upserts make a duplicate run harmless anyway).
- **Risk levels:** low < 0.33 ≤ medium < 0.66 ≤ high, set by `RISK_MEDIUM_FROM` / `RISK_HIGH_FROM` in config and shared by fusion, the dashboard and the history.
- `GET /nodes/{node_id}/dashboard`: latest reading and its age, the last 24 h (min/max temperature, humid hours), today's Hutton status, the learned 24 h / 48 h probabilities (with the day they were issued from), the current risk and level, and a one-sentence summary. The current risk is the same number fusion uses for a diagnosis (`fusion.resolve_risk`: today's sensor Hutton score, else the latest judged day within 3 days, else a neutral 0.5), so the two never disagree. The summary is decision support ("Conditions favour blight over the next 2 days, so consider checking the crop."), never an instruction, and unknown data is called unknown, never low.
- `GET /nodes/{node_id}/history?days=30` (1-365): every day newest first, with gaps shown as gaps, plus diagnoses and alerts, newest first (FR viii).
- `POST /symptom-reports` (`node_id`, `onset_date`, `notes`) and `GET /symptom-reports[?node_id=]`: own reports only; the onset date cannot be after today (local).

## Fusion (backend/fusion.py)

- **The rule:** `tau(r) = clip(TAU0 - K*(r - 0.5), TAU_MIN, TAU_MAX)` with TAU0 0.60, K 0.40, TAU_MIN 0.35, TAU_MAX 0.85 (config: `FUSION_*`), and `late_blight_flagged = p_late >= tau(r)`. With these values and r in [0, 1], tau runs from 0.80 (r = 0) to 0.40 (r = 1), so the clip is a safety net that only binds if TAU0 or K are re-tuned. Only late blight is gated; the CNN's label and confidence are reported as they are.
- **Which r:** the learned 24 h forecast covering the photo's own local day (issued from the previous day's sensor data); else that day's sensor Hutton score; else the latest judged day within 3 days; else the neutral 0.5. The choice is stored with the diagnosis (`risk_basis`, `risk_for_date`). API rows never stand in for the sensor.
- **POST /images** now writes a `diagnosis_results` row and links the node's sensor readings from the 48 h before the photo (`reading_contributions`). The response adds `diagnosis`: label, confidence, r, its basis, tau, `late_blight_flagged`, `risk_level` and a decision-support sentence. Optional form field `node_id`; by default the farmer's only node is used (422 if they have several, the neutral prior if none).
- **Sensitivity:** `.\.venv\Scripts\python.exe -m ml.fusion_eval` sweeps r over 0.1-0.9 on the CNN test set and writes `docs/fusion_eval.md`.

## SMS alerts (FR vii): backend/alerts.py, backend/sms.py

- **Sending:** the official `africastalking` SDK (2.0.3). With `AT_USERNAME=sandbox` it uses the sandbox API by itself. `SMS_DRY_RUN=true` (the default) only logs messages; set it to `false` to send. If the SDK cannot even connect (on the development PC its requests/urllib3 TLS handshake to the sandbox always failed while httpx, curl and plain Python worked), the identical request is sent with httpx; a timeout after sending is never retried, so nothing is sent twice.
- **Messages** (at most 160 GSM-7 characters each, decision support only): `high_risk` (plot, level HIGH, "consider checking your crop"), `diagnosis` (photo result and fused risk level), `low_risk` (status, "no action suggested").
- **Policy:** `high_risk` when a node's judged level CHANGES to high, at most once per 24 h (run by the hourly risk job); `diagnosis` whenever `late_blight_flagged` (from `POST /images`, once per diagnosis); `low_risk` only via `POST /nodes/{node_id}/status-sms`, only when the risk is judged low, at most hourly (409 with a reason otherwise). Unknown risk never changes the alert state and is never called low. A send that never reached Africa's Talking does not count: the next run retries.
- Every message writes an `alerts` row (type, message, `provider_message_id`, `delivery_status`).
- **Delivery reports:** `POST /sms/delivery-report` (form-encoded `id`, `status`, `failureReason`, as Africa's Talking sends them) updates `delivery_status`. Set the URL in the Africa's Talking dashboard (SMS → Callback URLs → Delivery Reports). It must be publicly reachable (e.g. through a tunnel during development); `SMS_CALLBACK_TOKEN` adds a `?token=` secret.
- **Seeing sandbox messages:** sandbox SMS are not delivered to real phones. Open the simulator (https://simulator.africastalking.com, or *Launch Simulator* in the sandbox dashboard), enter the recipient's number (e.g. the seeded farmer, +254700000002), and the message appears in the simulated phone's SMS inbox.

## Sensor ingest (FR ii)

- `POST /nodes/{node_id}/readings` takes a **batch** (max 500) and authenticates with the node's own `X-Node-Key` header, never a farmer JWT. Replaying a batch is harmless: a duplicate `(node_id, timestamp)` is skipped via `INSERT … ON CONFLICT DO NOTHING` and counted as a duplicate, not an error. Out-of-order and old timestamps are accepted, since the node buffers while offline. Rows are validated one by one (temperature -40…85 °C, the SHT31's own range; humidity 0…100%; timestamp no more than 5 minutes in the future, timezone required) and rejects are reported by position, so one bad reading does not cost the node its buffer. Response: `{"accepted": n, "duplicates": n, "rejected": [{"index": i, "reason": "…"}]}`. `last_sync_time` records when the node was last heard from (last seen), not the newest reading; `created_at` when it was registered.
- `POST /readings` is the same ingest in a flatter shape for the ESP32: `X-Device-Key`, a body of one reading or a list, each `{"device_id", "ts", "temp_c", "rh"}` (the device ID is the node ID). Same tables, limits and duplicate handling; the reply is `{"stored", "skipped", "rejected", "risk_level"}`, where `risk_level` comes from `fusion.resolve_risk` (as last scored by the hourly job) and is `unknown` when there is too little history. A node is a "device" here: there is no separate devices table, so the risk layer sees these readings unchanged.
- `GET /nodes/{node_id}/readings?from=&to=` uses a farmer JWT and the ownership rule; `from` inclusive, `to` exclusive, ordered by time.
- **Registering a device:** `.\dev.ps1 register-device node-01 "Bench node A" --key-file device_keys.local.env` creates the node (owned by the admin account unless `--owner-phone` names a farmer, at the farm site unless `--latitude/--longitude`), stores only the hash of a fresh 32-byte key, prints the raw key once, and refuses a device_id that already exists. `*.local.env` files are gitignored and blocked by the pre-commit hook.
- Node keys are random strings stored only as bcrypt hashes. `tools/seed.py` prints a key once when it issues one, including for an existing node that has none (that is how a lost key is replaced).
- **The full contract for the firmware is in [docs/firmware_contract.md](docs/firmware_contract.md)**: headers, payload, limits, status codes, and the retry/buffer rules the ESP32 must follow.

## Weather fetching (ml/fetch_weather.py)

```powershell
.\.venv\Scripts\python.exe -m ml.fetch_weather --source archive --start 2015-01-01 --end 2026-09-20
.\.venv\Scripts\python.exe -m ml.fetch_weather --source archive --days 30   # shortcut
```

| Source | Endpoint | Coverage | Notes |
|---|---|---|---|
| `archive` | `archive-api.open-meteo.com/v1/archive`, pinned to `models=era5_land` | 1950 → today−~6 days | The long, consistent record (ERA5-Land, ~9 km); publication lag, so it never covers this morning |
| `historical_forecast` | `historical-forecast-api.open-meteo.com/v1/forecast` | ~2022 → today | What the models predicted at the time; needed for the Hutton-on-forecast benchmark |

- Free, no API key (600 calls/min, 10,000/day). Fetched in yearly chunks with retry and backoff; 429/5xx are retried, a 400 is not.
- Re-running is safe: `ON CONFLICT DO NOTHING` on the weather uniqueness constraint, so a repeated range stores 0 rows.
- Hours the API returns as null are dropped, never interpolated; `hutton.py` then decides whether the day still has its 20 of 24 values.
- Raw responses are also written to `data/weather/*.csv` (gitignored) for inspection.
- **`forecast_issued_at` stays null.** The historical forecast API is a continuous series stitched from the first hours of successive model runs and exposes no per-hour issue time. A true fixed-lead-time forecast needs the Previous Runs API (`*_previous_day1/2`, archived from 2024) or the Single Runs API (`&run=`).

- **Why the archive model is pinned.** Left to its default (`best_match`), the archive API serves ECMWF IFS forecast data here from about 2017-18 (identical, hour for hour, to the historical-forecast API) and other data before that. The record would switch source partway through, and the Hutton-on-forecast benchmark would be scored against itself.

## Learned risk model (ml/build_features.py, ml/train_risk.py, ml/eval_risk.py, backend/risk_model.py)

```powershell
.\.venv\Scripts\python.exe -m ml.train_risk   # models/risk_model_24h.joblib, risk_model_48h.joblib
.\.venv\Scripts\python.exe -m ml.eval_risk    # docs/risk_eval.md (+ JSON, 3 PNG figures)
```

- **Targets** per local day D (Hutton rule from `backend/hutton.py`, on ERA5-Land): `y24` = D+1 is a Hutton day; `y48` = a Hutton period on D+1 or D+2. Unknown targets are dropped, never counted as negatives.
- **No leakage:** features for D use only hours up to the end of D. `backend/tests/test_build_features.py` alters or deletes every hour after D and requires day D's features not to move; it catches a planted look-ahead lag and a centred rolling window.
- **Time-ordered split, never shuffled:** test = the most recent 12 months, validation = the 12 before, train = the rest, with a 2-day embargo before each boundary (y48 looks two days ahead). The grid and the decision threshold are chosen on validation; the model is refit on train + validation and scored once on test against persistence, monthly climatology and Hutton-on-forecast, with block-bootstrap intervals.
- **Serving:** `backend.risk_model.predict_for_node(node_id, for_date, data_source)` builds day D's features from the database with the same functions as training (a test checks they are identical) and returns `p24` / `p48`, or `None` when day D could not be judged.
- scikit-learn and joblib are pinned exactly in `requirements.txt`: a saved model must be loaded with the version that trained it. The `.joblib` files are gitignored; re-create them with `ml.train_risk`.

## Chapter 5 evaluation (one command)

```powershell
.\.venv\Scripts\python.exe -m ml.run_all_evals                        # everything, sensor data marked SIMULATED
.\.venv\Scripts\python.exe -m ml.run_all_evals --sensor-data real     # once the ESP32 node is in the field
.\.venv\Scripts\python.exe -m ml.run_all_evals --pump-loads 12 --skip-tests
```

It re-runs, in order, `ml.eval_risk`, `ml.eval_cnn`, `ml.fusion_eval`, `ml.compare_sensor_vs_api`, `ml.cost_threshold` and `ml.make_test_plan`, writes their markdown tables and PNG figures to `docs/`, and prints the headline numbers. Nothing is retrained. A failing step does not stop the others; the summary names it and the exit code is 1.

- **Sensor vs API** (`docs/sensor_vs_api.md`): Hutton days and periods from the node vs the ERA5-Land archive at the same point, a 2×2 agreement table with Cohen's kappa, humid-hour differences and a timeline. The node table has no "simulated" flag, so `--sensor-data` says where the readings came from and defaults to **simulated**; every output carries a large banner saying which it is.
- **Cost threshold** (`docs/cost_threshold.md`): C = KES 2,500 chemical + 50 × pump loads (unknown, so 5 / 10 / 20 are shown); L = 20 bags × KES 2,500-5,000. Threshold = C_FP / (C_FP + C_FN) with C_FN = L − C, i.e. C / L. The 48 h model is then played through its test year (a spray covers 7 days; a spell of Hutton-period days with any day uncovered costs L once) against never, always-covered, and a calendar of 4 sprays per 120-day crop (year-round planting).
- **Test plan** (`docs/test_plan.md`): FR i–viii as ID | Module | Test case | Input | Expected | Actual | Pass/Fail. Automated rows are filled from a real run of the whole pytest suite. Manual rows come from `docs/manual_app_checklist.csv`: do each check on the phone and fill in its Actual, Pass/Fail and Date columns there; an unchecked row stays Pending.

## Data preparation and documentation

```powershell
.\dev.ps1 prepare-images          # PlantVillage potato export + data/splits/*.csv
.\dev.ps1 register-field-images   # register the farmer's weekly photos
.\dev.ps1 weather-quality         # weather coverage / gap report
```

`.\dev.ps1` with no argument lists every command (setup, test, lint, serve, db-migrate, revision, seed, fetch-weather, and the three above).

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

## CNN training (ml/train_cnn.ipynb) and the lab-to-field check (ml/eval_cnn.py)

1. Open `ml/train_cnn.ipynb` in Google Colab with a GPU runtime and choose *Runtime → Run all*. It loads PlantVillage's potato classes, rebuilds the split and asserts it is identical to `data/splits/*.csv`, trains MobileNetV2 in two phases, evaluates on the test set, exports TFLite, and downloads `cnn_outputs.zip`. The markdown cell at the top explains every step.
2. Unzip: `cnn.tflite` and `labels.txt` into `models/`, and `cnn_metrics.json`, `confusion_matrix.png` and `training_curves.png` into `docs/`.
3. Compare lab and field performance with the same model and preprocessing:

   ```powershell
   .\.venv\Scripts\python.exe -m ml.eval_cnn    # needs requirements-ml.txt; writes docs/cnn_domain_gap.md
   ```

- **Mendeley and TFDS.** Mendeley answers Python's HTTP client with 403 whatever the headers, while `curl` gets the file. So the notebook tries `tfds` first, then falls back to downloading the same archive with `curl` and reading it with the rule TFDS's `plant_village` builder uses (same files, labels and filenames).
- **Matching the split.** TFDS records each image's original archive filename. `ml/prepare_images.py` numbered images in sorted filename order and dropped none, so the notebook maps them back to the split-CSV names exactly. `backend/tests/test_train_cnn_notebook.py` checks that the rebuild equals the committed CSVs.
- **Model input:** float32 `[1, 224, 224, 3]`, RGB, centre square crop, bilinear antialiased resize, then `x / 127.5 - 1` (MobileNetV2's `preprocess_input`). Outputs are in `labels.txt` order: `early_blight`, `healthy`, `late_blight`.

## Mobile app (app/, Flutter, Android)

Flat layout: `lib/main.dart` (theme, start screen), `lib/api.dart` (every backend call, the login token in `flutter_secure_storage`, errors turned into plain sentences, and no exception other than `ApiException` ever escaping), `lib/models.dart`, `lib/widgets.dart`, and one file per screen: `login_screen.dart`, `register_screen.dart`, `home_screen.dart` (bottom navigation: Home, Scan, History, Report), `dashboard_screen.dart` (with "Send me a status SMS"), `scan_screen.dart` (camera or gallery, upload progress, Retake on a quality-check rejection), `diagnosis_screen.dart` (the CNN result, flag, risk, and the r/tau reasoning on tap), `history_screen.dart` and `report_screen.dart` (symptom onset date, no future days).

- **Server address:** the single constant `apiBaseUrl` in `lib/api.dart`. By default it is the hosted server, `https://blight-ews-api.onrender.com` (see Deployment), so a plain `flutter build apk` works from any network. Requests wait up to 75 s, because the free Render server takes about a minute to wake after 15 quiet minutes. For a backend on the PC, override it without editing: `flutter run --dart-define=API_BASE_URL=http://<PC-IP>:8000`. From the Android emulator, the PC is `http://10.0.2.2:8000`.
- **Plain HTTP is allowed for development only** (`android/app/src/main/res/xml/network_security_config.xml`). Remove it for production and serve the API over HTTPS.
- Flutter SDK at `C:\srclutter` (on the user PATH); Android Studio's JDK (`flutter config --jdk-dir`).

```powershell
# 1. Backend reachable from the phone (same Wi-Fi as the PC):
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 0.0.0.0 --port 8000

# 2. Once, in an ADMIN PowerShell: allow port 8000 on private networks only
New-NetFirewallRule -DisplayName "Blight EWS API (dev)" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private

# 3. Run the app on the phone (USB debugging on, phone on the same Wi-Fi)
cd app
flutter run

# Or over the USB cable only, with no Wi-Fi or firewall involved:
adb reverse tcp:8000 tcp:8000
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000
```

## Continuous integration

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs on every push to `main` and every pull request, as two jobs:

- **lint**: the pre-commit hooks on every file: ruff ([ruff.toml](ruff.toml)), trailing whitespace, YAML/TOML syntax, files over 500 KB, private keys, and any `.env` file.
- **pytest**: starts a PostgreSQL 16 service, applies the Alembic migrations, and runs the full pytest suite on Python 3.11.

The same hooks run locally on every `git commit` (installed by `.\dev.ps1 setup`); `.\dev.ps1 lint` runs them on the whole repo.
