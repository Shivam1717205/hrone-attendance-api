"""Employee Attendance & Analytics API."""

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, AsyncGenerator, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=2_000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance_logs = db.attendance_logs

def ensure_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_code",
    )
    attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_code",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_employee_date",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


def truncate_instant(value: Union[datetime, int]) -> datetime:
    if isinstance(value, int) and not isinstance(value, bool):
        converted = validate_epoch(value)
        if converted is None:
            raise HTTPException(status_code=422, detail="Timestamp cannot be null")
        value = converted
    elif not isinstance(value, datetime):
        raise HTTPException(status_code=422, detail="Timestamp must be a datetime or integer epoch milliseconds")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    seconds, milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=milliseconds)


def datetime_to_epoch_ms(value: Optional[Union[datetime, int]]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_instant(value).timestamp()) * 1000


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_shift(shift: str) -> time:
    hour, minute = map(int, shift.split(":"))
    return time(hour, minute)


def is_overnight(employee: dict) -> bool:
    return parse_shift(employee["shift_end"]) <= parse_shift(employee["shift_start"])


def attendance_date(punch_in: Union[datetime, int], employee: dict) -> date:
    local = truncate_instant(punch_in).astimezone(IST)
    shift_end = parse_shift(employee["shift_end"])
    local_time = local.timetz().replace(tzinfo=None)
    if is_overnight(employee) and local_time < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_boundary(day: date, shift: str) -> datetime:
    local_time = parse_shift(shift)
    return datetime.combine(day, local_time, tzinfo=IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, employee: dict, record_date: date) -> int:
    punched = truncate_instant(punch_in)
    start = shift_boundary(record_date, employee["shift_start"])
    elapsed_seconds = (punched - start).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_instant(punch_out) - truncate_instant(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime_minutes(punch_out: datetime, employee: dict, record_date: date) -> int:
    end_date = record_date + timedelta(days=1) if is_overnight(employee) else record_date
    shift_end = shift_boundary(end_date, employee["shift_end"])
    elapsed_seconds = (truncate_instant(punch_out) - shift_end).total_seconds()
    if elapsed_seconds < 1800:
        return 0
    return int(elapsed_seconds // 60)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def count_weekdays(start: date, end: date) -> int:
    days = (end - start).days + 1
    if days <= 0:
        return 0
    full_weeks, extra_days = divmod(days, 7)
    weekdays = full_weeks * 5
    weekday_index = start.weekday()
    weekdays += min(extra_days, max(0, 5 - weekday_index))
    weekdays += max(0, extra_days - (7 - weekday_index))
    return weekdays


def validate_date_string(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Date must use YYYY-MM-DD")
    return parsed


def ensure_month(value: Optional[str]) -> tuple[date, date, str]:
    if value is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    year, month = map(int, value.split("-"))
    if year < 1:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last), value


def validate_epoch(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(status_code=422, detail="Timestamp must be an integer epoch-millisecond value")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail="Timestamp is outside the supported epoch-millisecond range")
    return epoch_ms_to_datetime(value)


def round_expr(expression: object, places: int) -> dict:
    scale = str(10**places)
    return {
        "$divide": [
            {
                "$trunc": {
                    "$add": [
                        {"$multiply": [{"$toDecimal": expression}, {"$toDecimal": scale}]},
                        {"$toDecimal": "0.5"},
                    ]
                }
            },
            {"$toDecimal": scale},
        ]
    }


def serialize_history(history: list[dict]) -> list[dict]:
    result = []
    for entry in history:
        converted = dict(entry)
        converted["at"] = datetime_to_epoch_ms(converted.get("at"))
        changes = {}
        for field_name, change in converted.get("changes", {}).items():
            values = dict(change)
            if field_name in ("punch_in", "punch_out"):
                values["from"] = datetime_to_epoch_ms(values.get("from"))
                values["to"] = datetime_to_epoch_ms(values.get("to"))
            changes[field_name] = values
        converted["changes"] = changes
        result.append(converted)
    return result


def attendance_response(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result["history"] = serialize_history(result.get("history", []))
    result["punch_in"] = datetime_to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = datetime_to_epoch_ms(result.get("punch_out"))
    return result


def employee_response(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result["created_at"] = datetime_to_epoch_ms(result.get("created_at"))
    return result


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    return query


def attendance_list_pipeline(
    query: dict,
    page: int,
    page_size: int,
) -> list[dict]:
    return [
        {"$match": query},
        {"$sort": {"date": -1, "emp_code": 1}},
        {"$skip": (page - 1) * page_size},
        {"$limit": page_size},
    ]


def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$set": {
                "_day": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "_status": "$status",
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
                "_half_day": {"$ifNull": ["$half_day", False]},
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
                                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                                    {"$gte": [{"$dayOfWeek": "$_day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$_day"}, 6]},
                                ]
                            },
                            {"$cond": ["$_half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]


def department_summary_pipeline(
    month_end: date,
    start: date,
    end: date,
    department: Optional[str],
) -> list[dict]:
    employee_match: dict = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_day": {
                    "$cond": [
                        {"$ifNull": ["$_logs.date", False]},
                        {"$dateFromString": {"dateString": "$_logs.date", "format": "%Y-%m-%d"}},
                        None,
                    ]
                },
                "_status": "$_logs.status",
                "_late": {"$ifNull": ["$_logs.late_minutes", 0]},
                "_work_hours": "$_logs.work_hours",
                "_half_day": {"$ifNull": ["$_logs.half_day", False]},
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$_day", None]},
                                    {"$gte": [{"$dayOfWeek": "$_day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$_day"}, 6]},
                                ]
                            },
                            {"$cond": ["$_half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$_work_hours", None]},
                                ]
                            },
                            "$_work_hours",
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$_work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "hours_sum": {"$sum": "$hours_sum"},
                "hours_count": {"$sum": "$hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
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
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": ["$hours_sum", "$hours_count"]},
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


def leaderboard_pipeline(
    start: date,
    end: date,
    limit: int,
    department: Optional[str],
) -> list[dict]:
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    start_datetime = datetime.combine(start, time.min, tzinfo=UTC)
    end_datetime = datetime.combine(end, time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$sort": {"emp_code": 1}},
        {"$limit": 1},
        {
            "$project": {
                "department": {"$literal": department},
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {"$dateDiff": {"startDate": start_datetime, "endDate": end_datetime, "unit": "day"}},
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {"$dateAdd": {"startDate": start_datetime, "unit": "day", "amount": "$$offset"}},
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "date": "$dates",
                "date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates", "timezone": "UTC"}},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"dept": "$department", "day": "$date_string"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$department", "$$dept"]},
                        {"$lte": ["$joined_on", "$$day"]},
                    ]}}},
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date_string", "dept": "$department"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "_employee",
                        }
                    },
                    {"$unwind": "$_employee"},
                    {"$match": {"$expr": {"$eq": ["$_employee.department", "$$dept"]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "_daily",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily.late_count", 0]}, 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_avg_7d", None]},
                        None,
                        round_expr("$_avg_7d", 4),
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


def explain_aggregate(collection_name: Optional[str], pipeline: list[dict]) -> dict:
    aggregate_target = collection_name if collection_name is not None else 1
    command = {
        "explain": {
            "aggregate": aggregate_target,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    }
    return db.command(command)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


EpochMillis = Annotated[
    StrictInt,
    Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS, json_schema_extra={"format": "int64"}),
]


class HealthResponse(APIModel):
    status: Literal["ok"]


class ErrorResponse(APIModel):
    detail: str


class ValidationErrorResponse(APIModel):
    detail: list[dict[str, object]]


class Employee(APIModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str = Field(json_schema_extra={"format": "date"})
    created_at: EpochMillis


class EmployeePage(APIModel):
    items: list[Employee]
    total: int
    page: int
    page_size: int


class HistoryEntry(APIModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, dict[str, object]]


class AttendanceRecord(APIModel):
    emp_code: str
    date: str = Field(json_schema_extra={"format": "date"})
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    punch_in: Optional[EpochMillis]
    punch_out: Optional[EpochMillis]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntry]


class AttendancePage(APIModel):
    items: list[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(APIModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(APIModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(APIModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(APIModel):
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(APIModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(APIModel):
    date: str = Field(json_schema_extra={"format": "date"})
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(APIModel):
    department: str
    items: list[TrendItem]


class ExplainResponse(APIModel):
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    collection: str
    explain: dict[str, object]


OptionalEpochMillis = Annotated[
    Optional[EpochMillis],
    WithJsonSchema(
        {
            "type": "integer",
            "format": "int64",
            "minimum": MIN_EPOCH_MS,
            "maximum": MAX_EPOCH_MS,
        }
    ),
]
OptionalRegularizedStatus = Annotated[
    Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]],
    WithJsonSchema(
        {
            "type": "string",
            "enum": ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"],
        }
    ),
]


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(json_schema_extra={"format": "date"})

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            validate_date_string(value)
        except ValueError as exc:
            raise ValueError("joined_on must use YYYY-MM-DD") from exc
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: OptionalEpochMillis = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: OptionalEpochMillis = None

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(APIModel):
    status: OptionalRegularizedStatus = None
    punch_in: OptionalEpochMillis = None
    punch_out: OptionalEpochMillis = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_patch_fields(self):
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


@app.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse, "description": "MongoDB unavailable"}},
)
def health() -> dict:
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post(
    "/employees",
    status_code=201,
    response_model=Employee,
    responses={
        409: {"model": ErrorResponse, "description": "Employee code conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def create_employee(body: EmployeeIn) -> dict:
    document = body.model_dump()
    document["created_at"] = now_utc()
    try:
        employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return employee_response(document)


@app.get(
    "/employees",
    response_model=EmployeePage,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    items = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_response(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceRecord,
    responses={
        404: {"model": ErrorResponse, "description": "Employee not found"},
        409: {"model": ErrorResponse, "description": "Attendance conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def punch_in(body: PunchInIn) -> dict:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = validate_epoch(body.punched_at) or now_utc()
    punched_at = truncate_instant(punched_at)
    record_day = attendance_date(punched_at, employee)
    document = {
        "emp_code": body.emp_code,
        "date": record_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee, record_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return attendance_response(document)


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecord,
    responses={
        404: {"model": ErrorResponse, "description": "Employee or punch-in not found"},
        409: {"model": ErrorResponse, "description": "Attendance conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def punch_out(body: PunchOutIn) -> dict:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_out = validate_epoch(body.punched_at) or now_utc()
    punched_out = truncate_instant(punched_out)
    record = attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "status": {"$in": list(PRESENCE_STATUSES)},
            "punch_in": {"$type": "date", "$lte": punched_out},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punched_in = truncate_instant(record["punch_in"])
    elapsed = (punched_out - punched_in).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be after punch-in and within 24 hours")
    record_day = validate_date_string(record["date"])
    work_hours = compute_work_hours(punched_in, punched_out)
    update = {
        "punch_out": punched_out,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(punched_out, employee, record_day),
        "half_day": work_hours < 4.5,
    }
    result = attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record is already punched out")
    record.update(update)
    return attendance_response(record)


@app.get(
    "/attendance",
    response_model=AttendancePage,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = attendance_logs.count_documents(query)
    cursor = attendance_logs.find(query).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_response(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecord,
    responses={
        404: {"model": ErrorResponse, "description": "Employee or attendance not found"},
        409: {"model": ErrorResponse, "description": "Concurrent regularization conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn) -> dict:
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    day = date.isoformat()
    record = attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")

    current_status = record.get("status")
    current_in = record.get("punch_in")
    current_out = record.get("punch_out")
    new_status = body.status if "status" in supplied else current_status
    new_in = validate_epoch(body.punch_in) if "punch_in" in supplied else current_in
    new_out = validate_epoch(body.punch_out) if "punch_out" in supplied else current_out

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    elif new_status in PRESENCE_STATUSES and new_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    if new_in is not None:
        new_in = truncate_instant(new_in)
        if attendance_date(new_in, employee).isoformat() != day:
            raise HTTPException(status_code=422, detail="punch_in must remain on the record attendance date")
    if new_out is not None:
        new_out = truncate_instant(new_out)
        if new_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        new_late = new_overtime = 0
        new_hours = None
        new_half_day = False
    else:
        new_late = compute_late_minutes(new_in, employee, date) if new_in is not None else 0
        if new_out is None:
            new_hours = None
            new_overtime = 0
            new_half_day = False
        else:
            if new_in is None:
                raise HTTPException(status_code=422, detail="punch_out requires punch_in")
            new_hours = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime_minutes(new_out, employee, date)
            new_half_day = new_hours < 4.5

    before = {
        "status": current_status,
        "punch_in": current_in,
        "punch_out": current_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    after = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    requested_change = any(
        before[key] != after[key] for key in ("status", "punch_in", "punch_out")
    )
    if not requested_change:
        raise HTTPException(status_code=422, detail="regularization does not change the record")
    changes = {}
    for key, new_value in after.items():
        old_value = before[key]
        if old_value != new_value:
            if key in ("punch_in", "punch_out"):
                old_value = datetime_to_epoch_ms(old_value)
                new_value = datetime_to_epoch_ms(new_value)
            changes[key] = {"from": old_value, "to": new_value}

    update_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }
    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    concurrency_filter: dict = {"_id": record["_id"]}
    if "history" in record:
        concurrency_filter["history"] = record["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}
    result = attendance_logs.update_one(
        concurrency_filter,
        {"$set": update_values, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry regularization")
    record.update(update_values)
    record.setdefault("history", []).append(history_entry)
    return attendance_response(record)


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    responses={
        404: {"model": ErrorResponse, "description": "Employee not found"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict:
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    start, end, month_value = ensure_month(month)
    joined_on = validate_date_string(employee["joined_on"])
    working_start = max(start, joined_on)
    working_days = count_weekdays(working_start, end)
    aggregate_result = list(attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    values = aggregate_result[0] if aggregate_result else {}
    present_days = float(values.get("present_days", 0))
    attendance_pct = (
        round_half_up(present_days * 100 / working_days, 4)
        if working_days > 0
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month_value,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": values.get("leave_days", 0),
        "late_count": values.get("late_count", 0),
        "total_late_minutes": values.get("total_late_minutes", 0),
        "total_overtime_minutes": values.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict:
    start, end, month_value = ensure_month(month)
    rows = list(employees.aggregate(department_summary_pipeline(end, start, end, department)))
    for row in rows:
        row["present_days"] = round_half_up(row["present_days"], 2)
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
    return {"month": month_value, "items": rows}


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict:
    start, end, month_value = ensure_month(month)
    rows = list(attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department)))
    return {"month": month_value, "items": rows}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    responses={
        404: {"model": ErrorResponse, "description": "Department not found"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def department_trend(department: str, from_date: date = Query(alias="from"), to_date: date = Query(alias="to")) -> dict:
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    rows = list(employees.aggregate(department_trend_pipeline(department, from_date, to_date)))
    for row in rows:
        for field_name in ("attendance_rate", "moving_avg_7d"):
            if row[field_name] is not None:
                row[field_name] = float(row[field_name].to_decimal())
    return {"department": department, "items": rows}


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    explain_collection: Optional[str] = None
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        collection_name = "attendance_logs"
        explain_collection = collection_name
        pipeline = attendance_list_pipeline(query, page, page_size)
    elif endpoint == "employee_monthly":
        if emp_code is None:
            raise HTTPException(status_code=422, detail="emp_code is required")
        start, end, _ = ensure_month(month)
        collection_name = "attendance_logs"
        explain_collection = collection_name
        pipeline = employee_monthly_pipeline(emp_code, start, end)
    elif endpoint == "department_summary":
        start, end, _ = ensure_month(month)
        collection_name = "employees"
        explain_collection = collection_name
        pipeline = department_summary_pipeline(end, start, end, department)
    elif endpoint == "late_leaderboard":
        start, end, _ = ensure_month(month)
        collection_name = "attendance_logs"
        explain_collection = collection_name
        pipeline = leaderboard_pipeline(start, end, limit, department)
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid department trend range")
        collection_name = "employees"
        explain_collection = collection_name
        pipeline = department_trend_pipeline(department, from_date, to_date)
    explain = json.loads(json_util.dumps(explain_aggregate(explain_collection, pipeline)))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain,
    }
