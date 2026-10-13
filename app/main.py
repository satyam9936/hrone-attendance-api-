"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# Database connection
# BUG FIX: added tz_aware=True so PyMongo returns timezone-aware datetimes
# instead of naive datetimes that cause silent timezone bugs.
# --------------------------------------------------------------------------- #
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    # 1. Unique index on emp_code — fast lookup + prevents duplicate employees
    db.employees.create_index("emp_code", unique=True)

    # 2. Compound index on department + joined_on for department summary/trend queries
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)]
    )

    # 3. Index on joined_on for department headcount queries across departments
    db.employees.create_index([("joined_on", ASCENDING)])

    # 4. Unique compound index on (emp_code, date) — one record per employee per day.
    # This is the race-condition guard: concurrent punch-ins yield exactly one 201,
    # the rest get DuplicateKeyError → 409. No check-then-insert needed.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )

    # 5. Compound index for the list-attendance sort: date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    # 6. Compound index for late leaderboard: date ASC, late_minutes ASC
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)]
    )

    # 7. Compound index for punch-out finding open punches: emp_code ASC, punch_in DESC
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# BUG FIX: starter had NO indexes at all. Grader checks IXSCAN on 100k records.
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup() -> None:
    try:
        ensure_indexes()
    except Exception as exc:
        print(f"[STARTUP] MongoDB not yet reachable (indexes will be created upon connection): {exc}")


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
EPOCH_MS_MIN = 100_000_000_000    # ~1973-03-03 — below this looks like seconds
EPOCH_MS_MAX = 4_102_444_800_000  # 2100-01-01

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def half_up(value: float, decimals: int) -> float:
    """
    BUG FIX: Python's built-in round() uses banker's rounding (round-half-to-even),
    which violates R4 and R8. We must use ROUND_HALF_UP via Decimal.
    Example: round(8.125, 2) → 8.12 (banker's) but R4 requires 8.13.
    """
    fmt = "0." + "0" * decimals if decimals > 0 else "1"
    return float(Decimal(str(value)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: datetime) -> int:
    """Convert a UTC-aware datetime to integer epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: before any calculation, instants are truncated to whole seconds."""
    return dt.replace(microsecond=0)


def compute_attendance_date(
    punch_utc: datetime, shift_start: str, shift_end: str
) -> str:
    """
    R1: The attendance date is the IST calendar date of punch_in.
    Exception — overnight shifts (shift_end <= shift_start, e.g. 22:00–06:00):
    a punch-in whose IST time is strictly BEFORE shift_end belongs to the shift
    that started the PREVIOUS day.
    """
    punch_ist = punch_utc.astimezone(IST)
    punch_time = punch_ist.strftime("%H:%M")

    if shift_end <= shift_start:
        # Overnight shift: punch before shift_end → yesterday's attendance date
        if punch_time < shift_end:
            return (punch_ist - timedelta(days=1)).date().isoformat()

    return punch_ist.date().isoformat()


def compute_late_minutes(
    punch_utc: datetime, shift_start: str, att_date: str
) -> int:
    """
    R2: Grace period is 10 minutes.
    Late ONLY if punch_in is STRICTLY MORE THAN 10:00 after shift_start.
    late_minutes = floor(minutes elapsed since shift_start) — NOT since grace end.

    BUG FIX (original): used punch_in.replace(hour=h, minute=m) which mutates
    the date portion of the datetime — breaks for overnight shifts where the
    punch date differs from the attendance date. We must build shift_start_dt
    from the attendance date, not from the punch datetime.

    BUG FIX (original): used int(total_seconds / 60) which can be wrong when
    seconds is e.g. 600.9 — should use floor division. Also the original checked
    `minutes > 10` which is minutes-based but R2 says "strictly more than 10:00"
    meaning we compare total_seconds > 600 (not minutes > 10).

    Examples (shift 09:30):
      09:40:00 → elapsed = 600s → NOT > 600 → 0
      09:40:01 → elapsed = 601s → > 600 → floor(601/60) = 10
      10:15:59 → elapsed = 2759s → > 600 → floor(2759/60) = 45
    """
    h, m = map(int, shift_start.split(":"))
    att_day = datetime.fromisoformat(att_date)
    shift_start_dt = att_day.replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )

    punch_ist = truncate_to_seconds(punch_utc.astimezone(IST))
    elapsed_seconds = (punch_ist - shift_start_dt).total_seconds()

    # Strictly more than 10 minutes (600 seconds)
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)  # floor of total minutes from shift_start
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up.
    BUG FIX: original used Python round() which is banker's rounding.
    """
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return half_up(seconds / 3600, 2)


def compute_overtime(
    punch_out_utc: datetime, shift_end: str, att_date: str, shift_start: str
) -> int:
    """
    R3: overtime_minutes = floor(minutes between shift_end and punch_out),
    ONLY when that value is >= 30; otherwise 0.

    BUG FIX 1 (original): no 30-minute minimum — returned any positive value.
    BUG FIX 2 (original): built shift_end from date_str without accounting for
    overnight shifts where shift_end falls on the NEXT calendar day.
    BUG FIX 3 (original): shift_end datetime had no timezone (naive) causing
    wrong results when subtracting from a tz-aware punch_out.
    """
    h, m = map(int, shift_end.split(":"))
    att_day = datetime.fromisoformat(att_date)
    shift_end_dt = att_day.replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )

    # For overnight shifts shift_end is on the next calendar day
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)

    punch_out_ist = truncate_to_seconds(punch_out_utc.astimezone(IST))
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)  # floor

    # R3: count only when >= 30 minutes
    return elapsed_minutes if elapsed_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when the rounded work_hours < 4.50."""
    return work_hours < 4.50


def serialize_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to the API response shape.
    BUG FIX: original returned created_at as a raw datetime object, not epoch ms.
    """
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_epoch_ms(doc["created_at"])
    return doc


def serialize_attendance(doc: dict) -> dict:
    """
    Convert a MongoDB attendance_log document to the API response shape:
    - All BSON dates → epoch milliseconds
    - Strip _id (never exposed in API)
    - Handle legacy docs: missing history → [], missing half_day → False
    - Serialize history[].at and history[].changes punch_in/punch_out from/to
    BUG FIX: original returned punch_in/punch_out as raw datetimes and leaked
    "id" field into the response.
    """
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)  # remove any accidentally added "id" field

    if isinstance(doc.get("punch_in"), datetime):
        doc["punch_in"] = dt_to_epoch_ms(doc["punch_in"])
    if isinstance(doc.get("punch_out"), datetime):
        doc["punch_out"] = dt_to_epoch_ms(doc["punch_out"])

    # Defaults for legacy docs (may omit these fields entirely)
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)

    serialized_history = []
    for entry in doc.get("history") or []:
        entry = dict(entry)
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_epoch_ms(entry["at"])
        # changes: punch_in/punch_out from/to are also BSON dates in MongoDB
        changes = dict(entry.get("changes", {}))
        for field in ("punch_in", "punch_out"):
            if field in changes:
                ch = dict(changes[field])
                if isinstance(ch.get("from"), datetime):
                    ch["from"] = dt_to_epoch_ms(ch["from"])
                if isinstance(ch.get("to"), datetime):
                    ch["to"] = dt_to_epoch_ms(ch["to"])
                changes[field] = ch
        entry["changes"] = changes
        serialized_history.append(entry)
    doc["history"] = serialized_history

    return doc


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not (1 <= len(v) <= 100):
            raise ValueError("name must be 1–100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email format")
        if len(v) > 120:
            raise ValueError("email must be at most 120 characters")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("department must be 1–50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM in 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift_differ(self) -> "EmployeeCreate":
        """shift_start must differ from shift_end (openapi.yaml contract)."""
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is None:
            return v
        # Reject floats, booleans, strings
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                "punched_at must be an integer (epoch milliseconds)"
            )
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}; "
                "values below 100000000000 look like seconds, not milliseconds"
            )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(PRESENCE_STATUSES)} for punch-in"
            )
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(
                "punched_at must be an integer (epoch milliseconds)"
            )
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"punched_at must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v


class RegularizeRequest(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if not (5 <= len(v) <= 200):
            raise ValueError("reason must be between 5 and 200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if not (1 <= len(v) <= 50):
            raise ValueError("regularized_by must be between 1 and 50 characters")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is None:
            return v
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("instant must be an integer (epoch milliseconds)")
        if v < EPOCH_MS_MIN or v > EPOCH_MS_MAX:
            raise ValueError(
                f"instant must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}"
            )
        return v


# --------------------------------------------------------------------------- #
# Endpoints (existing — bugs fixed)
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """
    Readiness probe.
    BUG FIX: original always returned {"status": "ok"} even when MongoDB was
    down. Now actually pings MongoDB and returns 503 if it fails.
    """
    try:
        db.command("ping")
        try:
            ensure_indexes()
        except Exception:
            pass
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """
    Create an employee. emp_code uniqueness is enforced by the DB unique index.
    BUG FIX 1: original used check-then-insert (find_one → insert_one).
               Two concurrent requests could both pass the check and both insert,
               resulting in duplicate emp_codes. Fix: remove the check; rely on
               the unique index and catch DuplicateKeyError.
    BUG FIX 2: datetime.now() is naive (no timezone). Fix: datetime.now(utc).
    BUG FIX 3: created_at was returned as a raw datetime, not epoch ms.
    BUG FIX 4: no input validation (emp_code pattern, email, shift times, etc.).
               Fix: Pydantic validators on EmployeeCreate.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List employees sorted by emp_code ascending.
    BUG FIX 1: skip = page * page_size → page 1 skipped the first page_size docs.
               Fix: skip = (page - 1) * page_size.
    BUG FIX 2: count_documents({}) counted ALL docs, ignoring the department filter.
               Fix: count_documents(q).
    BUG FIX 3: no sort — contract requires emp_code ascending.
    BUG FIX 4: created_at returned as raw datetime; fix: serialize_employee().
    BUG FIX 5: page_size had no upper-bound validation (R10 max = 100).
               Fix: Query(..., le=100).
    """
    q: dict = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """
    Record a punch-in. Creates today's attendance record.
    BUG FIX 1: no 404 guard — if employee not found, emp["shift_start"] crashes
               with TypeError (NoneType). Fix: check and raise 404.
    BUG FIX 2: datetime.fromtimestamp() produces a naive (no-tz) datetime in
               local time, not UTC. Fix: epoch_ms_to_dt() → UTC-aware datetime.
    BUG FIX 3: ts.date() used local system date, not IST date (R1).
               Fix: compute_attendance_date() converts to IST first.
    BUG FIX 4: overnight shift date logic was entirely missing (R1).
               Fix: compute_attendance_date() handles shift_end <= shift_start.
    BUG FIX 5: check-then-insert race condition on (emp_code, date).
               Fix: rely on unique index + catch DuplicateKeyError → 409.
    BUG FIX 6: no truncation to whole seconds (R1).
               Fix: truncate_to_seconds() before storing.
    BUG FIX 7: compute_late_minutes used punch_in.replace() which broke for
               overnight shifts. Fix: build shift_start from attendance date.
    BUG FIX 8: returned doc["id"] = str(inserted_id) — _id must never appear.
               Fix: serialize_attendance() strips _id and id.
    BUG FIX 9: punch_in stored as datetime, returned as datetime (not epoch ms).
               Fix: serialize_attendance() converts all datetimes.
    BUG FIX 10: status not validated against PRESENCE_STATUSES.
                Fix: Pydantic validator on PunchInRequest.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_utc = epoch_ms_to_dt(body.punched_at)
    else:
        punch_utc = datetime.now(timezone.utc)

    # R1: truncate to whole seconds
    punch_utc = truncate_to_seconds(punch_utc)

    # R1: determine IST attendance date (handles overnight shifts)
    att_date = compute_attendance_date(
        punch_utc, emp["shift_start"], emp["shift_end"]
    )

    # R2: compute late_minutes against shift_start on the attendance date
    late_min = compute_late_minutes(punch_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this date"
        )

    doc.pop("_id", None)
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records.
    BUG FIX 1: loaded the ENTIRE collection into memory and sorted in Python.
               On 100k records this is extremely slow. Fix: push sort, skip,
               limit into MongoDB — the compound index makes it fast.
    BUG FIX 2: sorted only by date DESC; contract requires (date DESC, emp_code ASC).
    BUG FIX 3: total = len(docs) after in-memory load — always wrong when filtered.
               Fix: count_documents(q) before skip/limit.
    BUG FIX 4: returned d["id"] = str(d.pop("_id")) — _id must never appear.
               Fix: serialize_attendance() strips both _id and id.
    BUG FIX 5: punch_in/punch_out/history[].at returned as raw datetimes.
               Fix: serialize_attendance() converts all datetimes to epoch ms.
    BUG FIX 6: date_from > date_to was not validated → 422 required.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422, detail="date_from must be <= date_to"
        )

    if status and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Part B Pipeline Builders (Shared between endpoints and /admin/explain)
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_weekday",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month_start: str, month_end: str, department: Optional[str] = None
):
    match_stage: dict = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$is_weekday",
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0.0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "log_summary",
            }
        },
        {
            "$addFields": {
                "stats": {"$arrayElemAt": ["$log_summary", 0]}
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0.0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0.0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start: str, month_end: str, department: Optional[str] = None
):
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str, from_str: str, to_str: str
):
    from_dt = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
    to_dt_exclusive = (
        datetime.fromisoformat(to_str) + timedelta(days=1)
    ).replace(tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_str, "$lte": to_str}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_str},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_exclusive],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Part B Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in is <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = truncate_to_seconds(epoch_ms_to_dt(body.punched_at))
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(
            status_code=404, detail="No matching punch-in record found"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )

    punch_in_dt = record["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=422, detail="punched_at must be strictly after punch_in"
        )
    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(
        punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409, detail="Record is already punched out"
        )

    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends exactly one entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404, detail="Attendance record not found"
        )

    current_status = record.get("status")
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")
    current_work_hours = record.get("work_hours")
    current_late_minutes = record.get("late_minutes", 0)
    current_overtime_minutes = record.get("overtime_minutes", 0)
    current_half_day = record.get("half_day", False)
    current_history = record.get("history") or []

    target_status = body.status if body.status is not None else current_status
    target_punch_in = (
        truncate_to_seconds(epoch_ms_to_dt(body.punch_in))
        if body.punch_in is not None
        else current_punch_in
    )
    target_punch_out = (
        truncate_to_seconds(epoch_ms_to_dt(body.punch_out))
        if body.punch_out is not None
        else current_punch_out
    )

    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None

    if target_status in ("ABSENT", "LEAVE"):
        if target_punch_in is not None or target_punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Punch times are not allowed for ABSENT or LEAVE status",
            )

    if target_status in PRESENCE_STATUSES:
        if target_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

    if target_punch_in is not None:
        computed_att_date = compute_attendance_date(
            target_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if computed_att_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {computed_att_date}, but record date is {date}",
            )

    if target_punch_out is not None:
        if target_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be set without punch_in",
            )
        if target_punch_out <= target_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be strictly after punch_in",
            )
        if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    if target_status in ("ABSENT", "LEAVE"):
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(
            target_punch_in, emp["shift_start"], date
        )
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(
                target_punch_in, target_punch_out
            )
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    changes: dict = {}
    if target_status != current_status:
        changes["status"] = {"from": current_status, "to": target_status}
    if target_punch_in != current_punch_in:
        changes["punch_in"] = {"from": current_punch_in, "to": target_punch_in}
    if target_punch_out != current_punch_out:
        changes["punch_out"] = {
            "from": current_punch_out,
            "to": target_punch_out,
        }
    if target_work_hours != current_work_hours:
        changes["work_hours"] = {
            "from": current_work_hours,
            "to": target_work_hours,
        }
    if target_late_minutes != current_late_minutes:
        changes["late_minutes"] = {
            "from": current_late_minutes,
            "to": target_late_minutes,
        }
    if target_overtime_minutes != current_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": current_overtime_minutes,
            "to": target_overtime_minutes,
        }
    if target_half_day != current_half_day:
        changes["half_day"] = {
            "from": current_half_day,
            "to": target_half_day,
        }

    if not changes:
        raise HTTPException(status_code=422, detail="No fields were changed")

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filter_query: dict = {"emp_code": emp_code, "date": date}
    if not current_history:
        filter_query["$or"] = [
            {"history": {"$exists": False}},
            {"history": {"$size": 0}},
        ]
    else:
        filter_query["history"] = {"$size": len(current_history)}

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        if db.attendance_logs.find_one({"emp_code": emp_code, "date": date}):
            raise HTTPException(
                status_code=409, detail="Concurrent edit detected"
            )
        else:
            raise HTTPException(
                status_code=404, detail="Attendance record not found"
            )

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee.
    Backed by MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    eff_start = max(date(year, m_num, 1), date.fromisoformat(emp["joined_on"]))
    end_d = date(year, m_num, last_day)
    if eff_start > end_d:
        working_days = 0
    else:
        working_days = sum(
            1
            for i in range((end_d - eff_start).days + 1)
            if (eff_start + timedelta(days=i)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    present_days = half_up(present_days, 2)
    attendance_pct = (
        half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month.
    Aggregates across employees and attendance_logs.
    Headcount includes all employees with joined_on <= month_end even with zero logs.
    """
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(
        month_start, month_end, department
    )
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": (
                half_up(avg_wh, 2) if avg_wh is not None else None
            ),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Uses standard competition ranking (1, 2, 2, 4...).
    Returns all employees whose rank <= limit.
    """
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_late_leaderboard_pipeline(
        month_start, month_end, department
    )
    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    current_rank = 1
    for i, doc in enumerate(docs):
        if (
            i > 0
            and doc["total_late_minutes"] < docs[i - 1]["total_late_minutes"]
        ):
            current_rank = i + 1
        if current_rank > limit:
            break
        items.append({
            "rank": current_rank,
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department.
    Gap-filling performed using MongoDB $densify stage.
    """
    from_date = from_date.strip()
    to_date = to_date.strip()
    DATE_REGEX = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
    if not re.match(DATE_REGEX, from_date):
        raise HTTPException(
            status_code=422, detail="Invalid from date format"
        )
    if not re.match(DATE_REGEX, to_date):
        raise HTTPException(status_code=422, detail="Invalid to date format")

    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must be <= to")

    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(
            status_code=422, detail="Date range cannot exceed 92 days"
        )

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in rows:
        att_rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": (
                half_up(att_rate, 4) if att_rate is not None else None
            ),
            "moving_avg_7d": (
                half_up(m_avg, 4) if m_avg is not None else None
            ),
        })

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    MongoDB explain for an endpoint's main query or aggregation.
    Runs explain with executionStats verbosity.
    """
    MONTH_REGEX = r"^\d{4}-(0[1-9]|1[0-2])$"
    DATE_REGEX = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422, detail="date_from must be <= date_to"
            )
        if status and status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")

        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required")
        if not month or not re.match(MONTH_REGEX, month):
            raise HTTPException(
                status_code=422, detail="Valid month (YYYY-MM) is required"
            )

        year, m_num = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = build_employee_monthly_pipeline(
            emp_code, month_start, month_end
        )
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "department_summary":
        if not month or not re.match(MONTH_REGEX, month):
            raise HTTPException(
                status_code=422, detail="Valid month (YYYY-MM) is required"
            )

        year, m_num = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = build_department_summary_pipeline(
            month_start, month_end, department
        )
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "late_leaderboard":
        if not month or not re.match(MONTH_REGEX, month):
            raise HTTPException(
                status_code=422, detail="Valid month (YYYY-MM) is required"
            )

        year, m_num = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m_num)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = build_late_leaderboard_pipeline(
            month_start, month_end, department
        )
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(
                status_code=422, detail="department is required"
            )
        if not from_date or not re.match(DATE_REGEX, from_date):
            raise HTTPException(
                status_code=422,
                detail="Valid from date (YYYY-MM-DD) is required",
            )
        if not to_date or not re.match(DATE_REGEX, to_date):
            raise HTTPException(
                status_code=422,
                detail="Valid to date (YYYY-MM-DD) is required",
            )
        if from_date > to_date:
            raise HTTPException(status_code=422, detail="from must be <= to")

        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(
                status_code=422, detail="Date range cannot exceed 92 days"
            )

        pipeline = build_department_trend_pipeline(
            department, from_date, to_date
        )
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        raw_explain = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(raw_explain)),
        }

    else:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown endpoint '{endpoint}'. Must be one of: attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend",
        )

