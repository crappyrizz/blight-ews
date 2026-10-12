# Developer shortcuts for this project. Run from the repo root:
#     .\dev.ps1 <command> [extra arguments]
#
# First time on a new machine (or a fresh clone):
#     .\dev.ps1 setup        then fill in .env, then
#     .\dev.ps1 db-migrate
#     .\dev.ps1 seed
#     .\dev.ps1 test
#
# Example:
#     .\dev.ps1 test
#     .\dev.ps1 fetch-weather --days 30
#     .\dev.ps1 weather-quality

param(
    [Parameter(Position = 0)]
    [string]$Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$python = ".\.venv\Scripts\python.exe"
$alembic = ".\.venv\Scripts\alembic.exe"

# setup creates the virtual environment, so it runs before the check below.
if ($Command -eq "setup") {
    if (-not (Test-Path $python)) {
        # Python 3.11 is the project's version (TensorFlow support, and CI).
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        $python311 = "C:\Program Files\Python311\python.exe"
        if ($launcher) {
            & py -3.11 -m venv .venv
        } elseif (Test-Path $python311) {
            & $python311 -m venv .venv
        } else {
            Write-Host "Python 3.11 not found. Install it from python.org first." -ForegroundColor Yellow
            exit 1
        }
        if (-not (Test-Path $python)) { exit 1 }
    }
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not (Test-Path ".env")) {
        Copy-Item .env.example .env
        Write-Host "Created .env from .env.example: fill in the database password and secrets." -ForegroundColor Yellow
    }
    # Git hooks: ruff and the other checks run on every commit.
    & $python -m pre_commit install
    Write-Host "Setup done. Next: .\dev.ps1 db-migrate, .\dev.ps1 seed, .\dev.ps1 test" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $python)) {
    Write-Host "No virtual environment found. Run:  .\dev.ps1 setup" -ForegroundColor Yellow
    exit 1
}

switch ($Command) {
    "test" {
        & $python -m pytest @Rest
    }
    "lint" {
        # The same checks as the git hook and CI: ruff, whitespace, secrets, large files.
        & $python -m pre_commit run --all-files @Rest
    }
    "serve" {
        & $python -m uvicorn backend.app:app --reload @Rest
    }
    { $_ -in "db-migrate", "migrate" } {
        & $alembic upgrade head @Rest
    }
    "revision" {
        # .\dev.ps1 revision "message"
        & $alembic revision --autogenerate -m @Rest
    }
    "seed" {
        & $python -m tools.seed @Rest
    }
    "fetch-weather" {
        & $python -m ml.fetch_weather @Rest
    }
    "prepare-images" {
        # Exports the PlantVillage potato subset and rebuilds data/splits/*.csv.
        # Needs tensorflow-cpu and tensorflow-datasets installed (see README).
        & $python -m ml.prepare_images @Rest
    }
    "register-field-images" {
        # Scans data/field_images/ and appends new photos to metadata.csv.
        & $python -m ml.register_field_images @Rest
    }
    "weather-quality" {
        # Writes docs/data_quality.md and docs/missing_hours_per_month.png.
        & $python -m ml.weather_quality @Rest
    }
    default {
        Write-Host "Usage: .\dev.ps1 <command> [arguments]"
        Write-Host ""
        Write-Host "  setup                  create .venv, install requirements, .env, git hooks"
        Write-Host "  test                   run the pytest suite"
        Write-Host "  lint                   run ruff and the pre-commit checks on every file"
        Write-Host "  serve                  start the API with reload"
        Write-Host "  db-migrate             apply database migrations (alias: migrate)"
        Write-Host "  revision `"message`"     autogenerate a migration"
        Write-Host "  seed                   create the seed farmer, admin and node"
        Write-Host "  fetch-weather          pull Open-Meteo weather (see --help)"
        Write-Host "  prepare-images         export PlantVillage potato images and splits"
        Write-Host "  register-field-images  register the farmer's field photos"
        Write-Host "  weather-quality        write the weather data-quality report"
        if ($Command -ne "help") { exit 1 }
    }
}

# Pass the command's exit code on (so a failing test run fails the script).
exit $LASTEXITCODE
