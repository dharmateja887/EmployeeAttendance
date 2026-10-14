# DECISIONS.md

**1. Indexes.** `employees`: unique `emp_code` (identity + race-safe create); `{department, emp_code}` for the filtered, sorted list; `{department, joined_on}` and `{joined_on}` so headcount/trend match by join date without a scan. `attendance_logs`: unique `{emp_code, date}` (the natural key, also what makes punch-in race-safe); `{date, emp_code}` for list, monthly, summary, leaderboard and trend; `{status, date, emp_code}` for status-filtered lists. I rejected `{emp_code, status, date}` because the unique key already serves per-employee access.

**2. Punch-in race.** Both requests target the same `(emp_code, date)`. The insert goes straight to MongoDB; the unique index lets exactly one through and the loser gets `DuplicateKeyError`, which I map to 409. No check-then-insert.

**3. Ties.** `$rank` gives standard competition ranking (1, 2, 2, 4). Filtering `rank <= limit` after ranking returns every employee sharing a cutoff rank, ordered by total late minutes descending then `emp_code`.

**4. Headcount.** A second `$unionWith` branch aggregates `employees` with `joined_on <= end-of-month`, grouped by department, so zero-log employees count. Branches merge with `$max`/`$sum`; headcount 0 is dropped.

**5. At 100x.** Precompute per-day department rollups updated on write, archive old months, and materialize the daily trend series instead of densifying raw logs.
