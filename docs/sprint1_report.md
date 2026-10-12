# Sprint 1 Review: Blight Early-Warning System (Kinangop)

**Student:** Edgar Kareithi (164951) · **Supervisor:** Juliet Kirui · **Sprint 1:** 21 September – 12 October 2026
**Repository:** <https://github.com/crappyrizz/blight-ews>

[![tests](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml)

## Summary

Sprint 1 set up the foundations the rest of the project builds on: a reproducible Python 3.11 and PostgreSQL development environment that installs with one command; a flat, documented FastAPI codebase that is lint-clean and covered by 218 automated tests; a GitHub repository with branch-and-pull-request workflow, a clean history and continuous integration; and the project's data, prepared and documented. The data comprises a 10-table database built by migrations, a working API for farmer accounts and sensor ingest, 11.8 years of hourly weather for the farm site, and the 2,152-image potato leaf dataset split for training. Every deliverable was re-verified on 12 October 2026 by cloning the repository into an empty folder and building it from scratch. That check, and the review itself, found and fixed two problems: the documented weather backfill could not run (a database parameter limit), and linting and pre-commit hooks were not yet in place.

## Deliverables

| # | Deliverable | What was done | Evidence |
|---|---|---|---|
| 1 | A properly set up development environment | Python 3.11 virtual environment; PostgreSQL 18 locally (16 in CI); all configuration and secrets in a gitignored `.env`, loaded by pydantic-settings, with a committed template. `.\dev.ps1 setup` builds the environment in one step. **Verified from scratch** on 12 Oct 2026: fresh clone → `setup` → `db-migrate` → `seed` → `test` (218 passed) → `lint` (clean). | [dev.ps1](../dev.ps1), [.env.example](../.env.example), [requirements.txt](../requirements.txt), [README: Setup](../README.md#setup) · Figure 5 |
| 2 | A clean, professionally organised codebase | Flat layout fixed in the README (`backend/`, `ml/`, `tools/`, `docs/`…). Domain logic commented for the viva. Ruff linting (pyflakes, import order, pyupgrade, bugbear) with **0 findings**; pre-commit hooks on every commit; 218 pytest tests. | `.\dev.ps1 lint`, `.\dev.ps1 test`, [ruff.toml](../ruff.toml), [.pre-commit-config.yaml](../.pre-commit-config.yaml), [backend/tests/](../backend/tests/) · Figure 1 |
| 3 | All code under Git | GitHub repository; feature branches merged by pull request; `.gitignore` keeps data, models and secrets out. **History audited:** no `.env` was ever committed, none of the real secrets appears in any commit, and no file over 500 KB is in history. A pre-commit hook now refuses `.env` files and private keys. | [Commit history](https://github.com/crappyrizz/blight-ews/commits/main), [.gitignore](../.gitignore), [Git summary](#git-history) |
| 4 | Data, databases, APIs and files prepared | **Database:** 10 tables from 3 Alembic migrations, seeded with an admin, a farmer and a sensor node. **API:** 9 endpoints: health, register/login, profile, nodes, sensor ingest (batch, idempotent, per-node keys), reading history, admin list. **Data:** 103,272 hourly weather values (2015–2026, none missing); 2,152 PlantVillage potato images in stratified 70/15/15 splits; field-photo register ready. All documented. | [docs/data_dictionary.md](data_dictionary.md), [docs/data_report.md](data_report.md), [docs/data_quality.md](data_quality.md), [docs/firmware_contract.md](firmware_contract.md), [alembic/versions/](../alembic/versions/) · Figures 4, 6 |
| 5 | Appropriate automation for workflow and development management | GitHub Actions on every push to `main` and every pull request: **lint** job (pre-commit) and **pytest** job (PostgreSQL 16 service, migrations, full test suite). Local pre-commit hook. `dev.ps1` wraps every routine task. Data preparation is scripted and reproducible (fixed seed 42). | [.github/workflows/tests.yml](../.github/workflows/tests.yml), [CI runs](https://github.com/crappyrizz/blight-ews/actions), `.\dev.ps1 help` · Figures 2, 3, 5 |

## Repository structure

Two levels deep, as committed. `data/` holds the raw images and weather locally but is gitignored apart from the split CSVs and the field-photo register.

```
blight-ews/
├── .github/
│   └── workflows/            tests.yml (CI: lint + pytest)
├── alembic/
│   ├── versions/             3 migrations
│   ├── env.py
│   ├── script.py.mako
│   └── README
├── app/                      Flutter app (Sprint 3+)
├── backend/
│   ├── tests/                pytest suite (16 test modules)
│   ├── app.py                FastAPI routes
│   ├── auth.py               passwords, JWT, node keys, ownership rule
│   ├── config.py · db.py · ids.py · models.py · schemas.py
│   ├── ingest.py             sensor batch ingest
│   ├── hutton.py · fusion.py · alerts.py
│   └── __init__.py
├── data/
│   ├── field_images/         farmer's photo register (metadata.csv)
│   └── splits/               train.csv · val.csv · test.csv
├── docs/
│   ├── data_dictionary.md · data_report.md · data_quality.md
│   ├── firmware_contract.md · missing_hours_per_month.png
│   └── sprint1_report.md     this report
├── firmware/                 ESP32 firmware (when hardware arrives)
├── ml/
│   ├── fetch_weather.py · weather_quality.py
│   ├── prepare_images.py · register_field_images.py
│   └── build_features.py
├── models/                   trained models (Sprint 2+)
├── tools/
│   └── seed.py
├── .env.example · .gitignore · .pre-commit-config.yaml · ruff.toml
├── alembic.ini · pytest.ini · dev.ps1
├── requirements.txt · requirements-ml.txt
└── README.md
```

> **Figure 1:** VS Code Explorer showing this tree.

## Git history

| | |
|---|---|
| Commits on `main` | 15 (10 before this review, 4 from the review and the merge commit) |
| Pull requests merged | 2: [#1 Data preparation](https://github.com/crappyrizz/blight-ews/pull/1) and {{PR}} (this review) |
| Branches | `main`; `feature/data-prep` (merged in #1, due for deletion); `docs/sprint1-report` (this review); `feature/hutton-simulator` (Sprint 2 work in progress, not yet pushed) |
| First / last commit | 21 Sep 2026 / 12 Oct 2026 |

| Date | Commit (highlights) |
|---|---|
| 21 Sep | Scaffold project: folder layout, .gitignore, README |
| 21 Sep | Backend skeleton: config, database models, Alembic, /health, seed, tests |
| 23 Sep | Record unknown days honestly: nullable score, status, valid_hours |
| 29 Sep | Farmer registration and authentication (FR i) |
| 29 Sep | Sensor ingest with node API keys (FR ii) |
| 29 Sep | ml/fetch_weather.py: pull hourly Open-Meteo weather into weather_readings |
| 29 Sep | Prepare and document all project data (merged as PR #1) |
| 12 Oct | fetch_weather: insert in slices under PostgreSQL's parameter limit |
| 12 Oct | Lint, pre-commit, CI lint job and one-command setup |
| 12 Oct | dev.ps1 setup: explain a venv created without pip (long folder paths) |
| 12 Oct | Sprint 1 review report (this document) |

## Continuous integration

[![tests](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/crappyrizz/blight-ews/actions/workflows/tests.yml)

- Workflow: [.github/workflows/tests.yml](../.github/workflows/tests.yml), two jobs (lint, pytest) on every push to `main` and every pull request.
- Last run before this review, on `main`: [run 36561043189](https://github.com/crappyrizz/blight-ews/actions/runs/36561043189), passed (29 Sep 2026).
- This review's pull request: {{RUN}}.
- All runs: <https://github.com/crappyrizz/blight-ews/actions>

> **Figure 2:** GitHub Actions run for this review's pull request, both jobs green.

## Data summary

### Images (PlantVillage potato subset)

| Split | early_blight | healthy | late_blight | Total |
|---|---|---|---|---|
| train | 700 | 106 | 700 | 1,506 |
| val | 150 | 22 | 150 | 322 |
| test | 150 | 24 | 150 | 324 |
| **all** | **1,000** | **152** | **1,000** | **2,152** |

Stratified 70/15/15 with seed 42; 0 unreadable images, 0 exact duplicates. `healthy` is the minority class (6.6 : 1), so evaluation will report per-class recall and macro-F1, not accuracy alone. Source: PlantVillage, CC0 (details and citations in [data_report.md](data_report.md)).

Field photos from the farm: register and folder structure ready; **0 photos so far**.

> **Figure 6:** [docs/data_report.md](data_report.md) rendered on GitHub (class-count table), and the missing-hours figure from [docs/data_quality.md](data_quality.md).

### Weather (Open-Meteo ERA5 archive, farm coordinates −0.70, 36.60)

| | |
|---|---|
| Coverage | 1 Jan 2015 00:00 → 12 Oct 2026 23:00 UTC |
| Hourly values | 103,272 of 103,272 expected (100 %) |
| Missing hours | 0 |
| Days the Hutton rule can judge (≥ 20 of 24 hours) | 4,302 of 4,302 whole days |
| Out-of-range values | 0 |

### Simulated sensor readings

None on `main`. The node simulator is Sprint 2 work in progress (see [Known gaps](#known-gaps-carried-into-sprint-2)). A trial run of it put 8,414 readings (10-minute interval, 15 Aug – 12 Oct 2026) into the development database through the public ingest API.

### Database (PostgreSQL, development instance, 12 Oct 2026)

| Table | Rows | Note |
|---|---|---|
| farmers | 2 | seeded admin and farmer |
| sensor_nodes | 1 | seeded node |
| sensor_readings | 8,414 | from the trial simulator run |
| weather_readings | 103,272 | archive weather, 2015–2026 |
| risk_scores | 59 | Hutton baseline from the trial run |
| leaf_images · diagnosis_results · reading_contributions · alerts · symptom_reports | 0 | filled from Sprint 2 onwards |

Schema at Alembic revision `b181c764df90` (3 migrations). Full column reference: [data_dictionary.md](data_dictionary.md).

> **Figure 4:** pgAdmin (or `psql \dt`) showing the 10 tables and `alembic_version`.

## Known gaps carried into Sprint 2

1. **Hutton baseline rewrite and node simulator are not yet merged.** Both are written and tested on `feature/hutton-simulator`, but still to be reviewed, rebased onto this sprint's lint rules and merged.
2. **No field photos yet.** The second, harder evaluation set for the CNN depends on the farmer's weekly photos.
3. **Forecast weather not yet fetched.** The `historical_forecast` source is needed for the Hutton-on-forecast benchmark, and true fixed-lead-time forecasts need Open-Meteo's Previous Runs API.
4. **The latest archive days are provisional.** The archive returned values up to the current day, so the last few days may be revised when ERA5 is finalised. They should be re-fetched before model evaluation.
5. **Near-duplicate image check not yet done.** A perceptual-hash check across the PlantVillage splits, to rule out near-identical leaves on both sides of the split.
6. **Planned modules not yet started:** `risk_model.py`, `risk_job.py`, `cnn_service.py`, `sms.py`, the `ml/` training and evaluation scripts, the Flutter app, and the ESP32 firmware (hardware pending).
7. **Housekeeping.** Delete the merged `feature/data-prep` branch.

## Sprint 2 plan (proposed)

| # | Item | Outcome |
|---|---|---|
| 1 | Merge the Hutton pipeline and node simulator; add `risk_job.py` to compute daily Hutton scores | Daily baseline risk per node, from sensor and from API weather |
| 2 | Fetch `historical_forecast` weather; build features; train and evaluate the 24 h / 48 h learned risk model against persistence, climatology and Hutton-on-forecast | `models/risk_model_24h.joblib`, `risk_model_48h.joblib` and an evaluation table for Chapter 5 |
| 3 | Near-duplicate check, then MobileNetV2 transfer learning in Colab; export to TFLite; `cnn_service.py` | `models/cnn.tflite` and per-class evaluation |
| 4 | Wire fusion into a leaf-diagnosis endpoint (risk-shifted threshold) | Diagnosis results stored with the risk and threshold used |
| 5 | SMS alerts through the Africa's Talking sandbox (`sms.py`) | High-risk and diagnosis alerts delivered and logged |
| 6 | Re-fetch the latest archive days; collect the first field photos | Final weather record; start of the field evaluation set |

> **Figure 3:** GitHub Project board for the project (Sprint 1 done, Sprint 2 to do).

## How to reproduce this review

```powershell
git clone https://github.com/crappyrizz/blight-ews.git
cd blight-ews
.\dev.ps1 setup        # then fill in .env
.\dev.ps1 db-migrate
.\dev.ps1 seed
.\dev.ps1 test         # 218 passed
.\dev.ps1 lint         # all hooks pass
```
