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

- `POST /images` (farmer JWT, multipart: `file`, optional `capture_date` with timezone) stores the photo under `data/uploads/<farmer_id>/<image_id>.<ext>`, adds a `leaf_images` row, runs the CNN and returns `{"image": {...}, "prediction": {"label", "confidence", "probabilities"}}`. Fusion with the weather risk is a marked hook in `backend/app.py`, not built yet.
- **Quality check first.** A photo the model cannot judge fairly is refused with 422 and `{"reason", "message"}`, and nothing is stored, so the app can ask for a retake. Reasons: `unreadable`, `too_small` (short side < 224 px), `too_dark` (mean grey < 40), `too_blurry` (variance of the Laplacian < 50). The thresholds are named constants in `backend/cnn_service.py`, calibrated so that none of the 2,152 PlantVillage images is rejected; re-check them once field photos exist.
- `GET /images/{image_id}`: own images only (404 otherwise, like nodes).
- The model (`models/cnn.tflite`, `models/labels.txt`) is loaded once at startup. If it is missing, the API still runs and `POST /images` answers 503.
- Preprocessing mirrors training: RGB, EXIF rotation applied, 224×224 bilinear antialiased resize done in floating point (matches TensorFlow's to within 0.004 grey levels), then `x / 127.5 - 1`. The one unavoidable difference: training decoded JPEGs with TensorFlow's fast IDCT and Pillow uses the accurate one, so pixels can differ by up to 4/255 (about 1/255 on average).

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
- **Model input:** float32 `[1, 224, 224, 3]`, RGB, bilinear antialiased resize, then `x / 127.5 - 1` (MobileNetV2's `preprocess_input`). Outputs are in `labels.txt` order: `early_blight`, `healthy`, `late_blight`.

## Mobile app (app/, Flutter, Android)

Flat layout: `lib/main.dart` (theme, start screen), `lib/api.dart` (every backend call, the login token in `flutter_secure_storage`, errors turned into plain sentences, and no exception other than `ApiException` ever escaping), `lib/models.dart`, `lib/widgets.dart`, and one file per screen: `login_screen.dart`, `register_screen.dart`, `home_screen.dart` (bottom navigation: Home, Scan, History, Report), `dashboard_screen.dart` (with "Send me a status SMS"), `scan_screen.dart` (camera or gallery, upload progress, Retake on a quality-check rejection), `diagnosis_screen.dart` (the CNN result, flag, risk, and the r/tau reasoning on tap), `history_screen.dart` and `report_screen.dart` (symptom onset date, no future days).

- **Server address:** the single constant `apiBaseUrl` in `lib/api.dart` (default: the PC's Wi-Fi address). Override it without editing: `flutter run --dart-define=API_BASE_URL=http://<PC-IP>:8000`. From the Android emulator, the PC is `http://10.0.2.2:8000`.
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

## Setup

Requires Python 3.11 and PostgreSQL 15+ (developed on 18). All commands run from the repo root in PowerShell.

```powershell
# 1. Virtual environment, dependencies, .env from the template, git hooks
.\dev.ps1 setup
#    then open .env and fill in the database password and secrets

# 2. Create / update the database tables
.\dev.ps1 db-migrate

# 3. Seed one admin, one farmer and one sensor node (safe to re-run)
.\dev.ps1 seed

# 4. Run the tests (uses TEST_DATABASE_URL, never the real database)
.\dev.ps1 test

# 5. Start the API, then open http://127.0.0.1:8000/health
.\dev.ps1 serve
```

The database user and the two databases (`blight_ews`, `blight_ews_test`) are created once by hand with `psql` as the `postgres` superuser.
