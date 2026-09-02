# Aircraft Spotting Archive

A local-first Flask application for transforming a personal aircraft-spotting log into a reproducible relational archive and linking observations to historical ADS-B flight candidates.

The project was developed as the practical artefact for the University of Sunderland PROM02 MSc dissertation. Its emphasis is explainable record linkage: the system preserves the original observation, ranks multiple candidate flights with visible component scores, and keeps automated recommendations separate from manually verified ground truth.

## Features

- Normalises compact and legacy Excel spotting-log layouts into relational data.
- Preserves 50 controlled observations while retaining source-row provenance.
- Imports targeted historical ADS-B data from ADSB.lol archives.
- Supports OpenSky historical queries when access is available.
- Generates explainable weighted-v1 candidate scores from registration, time, airport, callsign and route evidence.
- Displays candidate score breakdowns, trajectories, altitude and ground-speed charts.
- Provides responsive Archive, aircraft detail and Evaluation pages.
- Supports observation photographs, capture timestamps and special notes.
- Persists manual ground-truth decisions and exports evaluation data as CSV/JSON.
- Reports Precision, Recall, F1 and reproducible feature-ablation results.
- Runs on SQLite for local development and has been validated against MariaDB/MySQL.

## Evaluation snapshot

The dissertation evaluation uses 25 manually reviewed positive cases from 5 November 2025:

- correct candidate ranked first: 25/25;
- automatic acceptance at the 80-point threshold: 19/25;
- Precision: 1.000;
- Recall: 0.760;
- F1: 0.864.

These figures describe the fixed reviewed subset, not universal system accuracy. The set contains no verified negative/no-match cases and does not support cross-date generalisation.

## Project structure

```text
app/
  integrations/      OpenSky and ADSB.lol adapters
  services/          import, matching, evaluation and visualisation logic
  static/            responsive CSS
  templates/         Flask/Jinja pages
tests/                automated test suite
reports/              evaluation and linkage summary
run.py                Flask entry point
requirements.txt      pinned Python dependencies
```

## Quick start

Python 3.12 is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
flask --app run.py init-db
flask --app run.py run --debug
```

Open `http://127.0.0.1:5000`.

Without `DATABASE_URL`, the application uses a local SQLite database under `instance/`. To use MySQL or MariaDB, create an empty database and set a connection string in `.env`, for example:

```text
DATABASE_URL=mysql+pymysql://USER:PASSWORD@127.0.0.1:3306/aircraft_archive?charset=utf8mb4
```

## Importing a workbook

Preview the transformation before applying it:

```powershell
flask --app run.py import-excel "C:\path\to\transport.xlsx" --dry-run --limit 50
```

After checking the preview, import into an empty or backed-up database:

```powershell
flask --app run.py import-excel "C:\path\to\transport.xlsx" --replace-existing --limit 50
```

`--replace-existing` is intentionally destructive to observation and match rows. Back up the database and uploads together before using it on an existing archive.

## Historical ADS-B ingestion

The ADSB.lol importer reads split daily tar archives as one continuous stream and extracts only ICAO24 values relevant to the controlled observation set:

```powershell
flask --app run.py adsblol import-archive `
  "C:\path\to\archive.tar.aa" "C:\path\to\archive.tar.ab" `
  --date 2025-11-05 --location "London Heathrow" --replace-source-day

flask --app run.py match run --date 2025-11-05 --location "London Heathrow"
```

OpenSky integration is also implemented, but historical access depends on external account approval.

## Tests

```powershell
python -m pytest -q
```

The publication snapshot passes 50 automated tests covering import safety, matching, evaluation, photo handling, trajectory visualisation and SQLite/MySQL compatibility boundaries.

## Data and privacy

This public repository contains source code and tests only. It intentionally excludes:

- the author's Excel workbook;
- SQLite/MySQL database files;
- uploaded aircraft photographs;
- ADS-B raw archives and extracted traces;
- local backups, credentials and `.env` values.

Reviewers can run the complete automated test suite without those private artefacts. Reproducing the dissertation's exact numerical snapshot additionally requires the controlled workbook and historical source archives described in the report.

## Technology

Flask, SQLAlchemy, SQLite, MariaDB/MySQL, Jinja, pytest, OpenSky Trino and ADSB.lol historical archives.

## Academic context

This repository is provided for assessment and reproducibility. The dissertation remains the authoritative source for research questions, methods, limitations and interpretation of results.
