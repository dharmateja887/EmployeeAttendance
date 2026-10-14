# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee attendance (punch-in/out, manual
corrections with an audit trail) and analytics built on aggregation pipelines.

## Requirements
* Python 3.11+
* MongoDB 6.0 or newer (local Docker or Atlas)

## Run
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit MONGO_URI / MONGO_DB
# local Mongo: docker run -p 27017:27017 mongo:7
python sample_seed.py         # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` is
loaded but real environment variables take precedence. All MongoDB indexes are
created idempotently at startup. `GET /health` returns 200 only while MongoDB
answers a ping, else 503.

All application code is in `app/main.py`. The HTTP contract and business rules
(R1-R10) are in `openapi.yaml`; the stored data model is in `DATA_MODEL.md`.

## Endpoints
* `POST/GET /employees`
* `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
* `PATCH /attendance/{emp_code}/{date}` (regularization + history)
* `GET /analytics/employees/{emp_code}/monthly`
* `GET /analytics/departments/summary`
* `GET /analytics/leaderboard/late`
* `GET /analytics/departments/{department}/trend`
* `GET /admin/explain/{endpoint}` (raw MongoDB `explain` for the main query)

## Notes
* Instants cross the HTTP boundary as epoch **milliseconds** and are stored as
  UTC BSON dates; calendar values stay strings (`YYYY-MM-DD`, `HH:MM`).
* Punch-in is race-safe via the unique `(emp_code, date)` index; punch-out and
  regularization use atomic conditional updates, so concurrent edits either
  win once or return 409 — never silently lose a history entry.
* Analytics read the stored derived values (`work_hours`, `late_minutes`,
  `overtime_minutes`, `half_day`); they are never recomputed from punch times.
