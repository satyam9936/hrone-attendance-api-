# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | DB connection / startup | `MongoClient` initialized without `tz_aware=True`. | PyMongo returns naive datetimes; comparisons and conversions with UTC/IST raise exceptions or compute wrong offsets. | Added `tz_aware=True` to `MongoClient` configuration. |
| 2 | Startup / Indexes | Starter created zero database indexes at startup. | Explain plans on collections show `COLLSCAN`; queries slow down dramatically on ~100k records. | Added idempotent startup indexes on `employees` and `attendance_logs` covering `emp_code`, `date`, `department`, `joined_on`, and `late_minutes`. |
| 3 | `health()` | No MongoDB ping was performed (`return {"status": "ok"}`). | Returns HTTP 200 even when MongoDB is offline or disconnected (violates readiness probe requirement). | Added `db.command("ping")` within try/except block; raises HTTP 503 if ping fails. |
| 4 | `create_employee()` | Check-then-insert race condition (`find_one` followed by `insert_one`). | Concurrent requests with the same `emp_code` both succeed and insert duplicate records. | Rely on database unique index on `emp_code` and catch `DuplicateKeyError`, raising HTTP 409. |
| 5 | `create_employee()` | `datetime.now()` without timezone used for `created_at`. | Stored creation timestamp is local naive time instead of UTC instant. | Changed to `datetime.now(timezone.utc)`. |
| 6 | `create_employee()` | Returned raw BSON datetime for `created_at` instead of epoch milliseconds. | Response validation error or contract mismatch; client expects integer epoch ms. | Serialized response using `serialize_employee()` which converts `created_at` to epoch milliseconds. |
| 7 | `create_employee()` | Missing input validations: `emp_code` regex (`^EMP\d{4,6}$`), email format, `shift_start != shift_end`, valid date for `joined_on`. | Invalid employee codes, malformed emails, and identical shift times accepted with 200/201 instead of 422. | Replaced `EmployeeIn` with `EmployeeCreate` Pydantic model with strict field and model validators. |
| 8 | `list_employees()` | `skip = page * page_size` skipped the entire first page. | Calling `page=1` skips items 1–20; first page is empty or shows page 2 data. | Fixed skip formula to `(page - 1) * page_size`. |
| 9 | `list_employees()` | `count_documents({})` ignored department filter when computing `total`. | Total count shows entire collection count instead of the filtered count. | Passed filter query `count_documents(q)`. |
| 10 | `list_employees()` | Missing required sort by `emp_code` ascending. | Returned employee records in arbitrary order across pages. | Added `.sort("emp_code", ASCENDING)`. |
| 11 | `list_employees()` | Uncapped `page_size` query parameter. | Client can request arbitrary large page size (e.g. 100,000) leading to memory exhaustion. | Enforced `ge=1, le=100` with default 20 via FastAPI `Query`. |
| 12 | `compute_late_minutes()` | Replaced hour/minute on `punch_in` directly (`punch_in.replace(...)`). | Breaks for overnight shifts where punch-in date differs from the shift start calendar day. | Built shift start datetime using the record's attendance date (`att_date`) with IST timezone. |
| 13 | `compute_late_minutes()` | Checked `minutes > 10` after integer division instead of strictly > 10:00 (600 seconds). | Border cases like `09:40:00` vs `09:40:01` mishandled; 09:40:01 failed to count 10 late minutes. | Evaluated `elapsed_seconds > 600`, then computed whole minutes via floor division `int(elapsed_seconds // 60)`. |
| 14 | `compute_work_hours()` | Used Python's built-in `round()` which performs round-half-to-even (banker's rounding). | E.g., 8.125 rounds to 8.12 instead of 8.13 required by Rule R4. | Implemented `half_up()` utility using `Decimal` with `ROUND_HALF_UP`. |
| 15 | `compute_overtime()` | Missing 30-minute minimum threshold (Rule R3). | Returns positive overtime for durations under 30 minutes (e.g. 5 or 25 minutes). | Added check `elapsed_minutes if elapsed_minutes >= 30 else 0`. |
| 16 | `compute_overtime()` | Shift end constructed without checking for overnight shifts (`shift_end <= shift_start`). | Overtime completely incorrect (negative or huge) for overnight shifts. | Added `+ timedelta(days=1)` to `shift_end_dt` when `shift_end <= shift_start`. |
| 17 | `punch_in()` | No check for existing employee; crashed with `NoneType` attribute error on `emp["shift_start"]`. | Request for non-existent `emp_code` returned 500 Internal Server Error instead of 404. | Added explicit employee check: `if not emp: raise HTTPException(404, "Employee not found")`. |
| 18 | `punch_in()` | `datetime.fromtimestamp()` produced naive local datetime. | Timestamps parsed in server's local timezone instead of UTC instant. | Used `epoch_ms_to_dt()` converting ms to `datetime(..., tz=timezone.utc)`. |
| 19 | `punch_in()` | Used `ts.date()` for attendance date, ignoring IST calendar date and overnight shifts (Rule R1). | Night-shift punches before shift_end assigned to current day instead of previous day. | Implemented `compute_attendance_date()` handling IST timezone conversion and overnight shift rollover. |
| 20 | `punch_in()` | Check-then-insert race condition on attendance record. | Concurrent punch-in requests for same employee and date could both insert records. | Handled via unique compound index on `(emp_code, date)` and catching `DuplicateKeyError` -> HTTP 409. |
| 21 | `punch_in()` | Instants not truncated to whole seconds (Rule R1). | Microseconds/sub-seconds stored in database and propagated into response. | Truncated instants with `truncate_to_seconds()` (`microsecond=0`) before storing and computing. |
| 22 | `punch_in()` | Returned `doc["id"] = str(doc.pop("_id"))`, leaking internal MongoDB identifier into response. | Violates contract convention: `_id` and `id` must never appear in responses. | Removed `_id` and `id` stripping in `serialize_attendance()`. |
| 23 | `punch_in()` | Returned raw BSON datetime for `punch_in` instead of epoch milliseconds. | Contract violation; client expects integer epoch milliseconds. | `serialize_attendance()` converts all instant fields to integer epoch milliseconds. |
| 24 | `punch_in()` | Accepted arbitrary status values (including `ABSENT` or `LEAVE`). | Punch-in allowed creating `ABSENT` or `LEAVE` records which is forbidden by Rule R6 and contract. | Validated status with Pydantic validator to allow only `PRESENT`, `WFH`, `ON_DUTY`. |
| 25 | `punch_in()` | Allowed invalid/out-of-range timestamps (e.g. seconds-looking numbers < 100000000000, strings, floats). | Corrupted dates accepted without raising validation error. | Added validator rejecting non-integers and numbers outside `[100000000000, 4102444800000]`. |
| 26 | `list_attendance()` | Loaded the entire collection into memory and sorted via Python (`docs = list(db.attendance_logs.find(...)); docs.sort(...)`). | Memory exhaustion and extreme latency on large datasets (~100,000 records). | Pushed sort, skip, and limit directly to MongoDB query engine using compound index `(date: -1, emp_code: 1)`. |
| 27 | `list_attendance()` | Sorted only by `date` descending, omitting secondary sort by `emp_code` ascending. | Non-deterministic pagination ordering for records sharing the same date. | Added compound sort: `[("date", DESCENDING), ("emp_code", ASCENDING)]`. |
| 28 | `list_attendance()` | `total = len(docs)` evaluated after in-memory fetch, giving incorrect counts. | Total was either count of fetched page or total collection instead of filtered count. | Computed `total = db.attendance_logs.count_documents(q)`. |
| 29 | `list_attendance()` | Did not validate `date_from > date_to`. | Inverted date ranges returned empty results with HTTP 200 instead of HTTP 422. | Added validation raising HTTP 422 when `date_from > date_to`. |
| 30 | `list_attendance()` | Returned raw datetimes for `punch_in`, `punch_out`, and `history[].at`, plus leaked `id`. | Failed contract validation for instant serialization and ID leakage. | Mapped all documents through `serialize_attendance()`. |

---

### What Was Looked at and Decided Was NOT a Defect

1. **Absence of endpoints to create `ABSENT` or `LEAVE` records:**
   - Looked at whether an endpoint was needed to record leaves or absences.
   - Decided this is **not a defect**: The contract explicitly specifies that no endpoint creates `ABSENT` or `LEAVE` records; they are seeded back-office records or created via manual regularization (`PATCH /attendance/{emp_code}/{date}`).
2. **Missing `_id` in response payloads:**
   - Looked at whether MongoDB's `_id` should be exposed as `id`.
   - Decided stripping it is **correct and intentional**: The contract explicitly states that `emp_code` is the business identifier and `_id` is purely internal and must never be exposed.
3. **Absence of holiday calendar in working days calculation (Rule R7):**
   - Looked at whether public or national holidays should be deducted from working days.
   - Decided this is **not a defect**: Rule R7 specifically defines working days as Monday through Friday with "no holiday calendar".
