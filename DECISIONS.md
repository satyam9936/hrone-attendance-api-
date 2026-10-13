# DECISIONS.md

1. **Indexes**
   On `employees`, I added a unique index on `emp_code` for lookups, plus a compound index on `(department, joined_on)` and a single index on `joined_on` for headcount filtering. On `attendance_logs`, I added a unique compound index on `(emp_code, date)` to block duplicate punches, `(date, emp_code)` for pagination sorting, `(date, late_minutes)` for the leaderboard, and `(emp_code, punch_in)` for open punch-outs. I skipped indexing `status` alone because with only five values, its selectivity is too low to help.

2. **Punch-in race**
   Both requests compute the same date and try to insert. Because of the unique `(emp_code, date)` index, MongoDB's storage engine enforces atomicity: one insert succeeds with 201, while the other hits a `DuplicateKeyError` and returns 409 Conflict. There is no check-then-insert gap.

3. **Ties**
   I used standard 1, 2, 2, 4 competition ranking. When employees tie at the cutoff rank, all of them are returned, so the list can exceed the requested `limit`.

4. **Headcount**
   The pipeline starts from `employees` (filtering `joined_on <= month_end`) and does a `$lookup` into `attendance_logs`. This ensures people with zero attendance records still show up in headcount.

5. **At 100x scale**
   I would stop running aggregations across raw logs on every request and instead maintain daily pre-aggregated summaries using background workers or MongoDB Change Streams.
