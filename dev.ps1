# Developer shortcuts for this project. Run from the repo root:
#     .\dev.ps1 <command> [extra arguments]
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

if (-not (Test-Path $python)) {
    Write-Host "No virtual environment found. Create one with:" -ForegroundColor Yellow
    Write-Host "  & 'C:\Program Files\Python311\python.exe' -m venv .venv"
    Write-Host "  $python -m pip install -r requirements.txt"
    exit 1
}

switch ($Command) {
    "test" {
        & $python -m pytest @Rest
    }
    "serve" {
        & $python -m uvicorn backend.app:app --reload @Rest
    }
    "migrate" {
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
        Write-Host "  test                   run the pytest suite"
        Write-Host "  serve                  start the API with reload"
        Write-Host "  migrate                apply database migrations"
        Write-Host "  revision `"message`"     autogenerate a migration"
        Write-Host "  seed                   create the seed farmer, admin and node"
        Write-Host "  fetch-weather          pull Open-Meteo weather (see --help)"
        Write-Host "  prepare-images         export PlantVillage potato images and splits"
        Write-Host "  register-field-images  register the farmer's field photos"
        Write-Host "  weather-quality        write the weather data-quality report"
        if ($Command -ne "help") { exit 1 }
    }
}
