import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any
from fastapi import HTTPException

# IST Timezone (UTC + 05:30)
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt: datetime) -> datetime:
    """Ensure datetime has timezone and convert to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def round_half_up(val: float | Decimal, decimals: int = 2) -> float:
    """R8: Round half-up (e.g. 0.005 -> 0.01)."""
    d = Decimal(str(val))
    return float(d.quantize(Decimal(10) ** -decimals, rounding=ROUND_HALF_UP))


def epoch_to_datetime(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime (truncated to seconds per R1)."""
    if not (100000000000 <= ms <= 4102444800000):
        raise HTTPException(422, "Epoch milliseconds out of range")
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def datetime_to_epoch(dt: Any) -> Optional[int]:
    """Convert datetime object to integer epoch milliseconds."""
    if isinstance(dt, datetime):
        return int(to_ist(dt).timestamp() * 1000)
    return int(dt) if isinstance(dt, (int, float)) else None


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Attendance date is IST calendar date.
    Overnight shift: punch-in before morning shift_end belongs to the previous day's shift.
    """
    t = to_ist(punch_in)
    if shift_end <= shift_start and t.strftime("%H:%M") < shift_end:
        return (t.date() - timedelta(days=1)).isoformat()
    return t.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: 10-minute grace period. If punch-in is > 10 min late, count total minutes since shift_start."""
    t = to_ist(punch_in).replace(microsecond=0)
    sh, sm = map(int, shift_start.split(":"))
    start = datetime.fromisoformat(date_str).replace(hour=sh, minute=sm, tzinfo=IST)
    diff_sec = (t - start).total_seconds()
    return int(diff_sec // 60) if diff_sec > 600 else 0


def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, date_str: str) -> int:
    """R3: Overtime in minutes if >= 30 min after shift_end."""
    t = to_ist(punch_out).replace(microsecond=0)
    eh, em = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=eh, minute=em, tzinfo=IST)
    if shift_end <= shift_start:  # overnight shift ends next calendar day
        end += timedelta(days=1)
    diff_min = int((t - end).total_seconds() // 60)
    return diff_min if diff_min >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: Work duration in hours, rounded to 2 decimals half-up."""
    hours = (punch_out - punch_in).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day if worked < 4.50 hours."""
    return bool(work_hours is not None and work_hours < 4.50)


def get_month_date_range(month: str) -> tuple[str, str]:
    """Return ISO strings for the first and last day of a month (YYYY-MM)."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    return f"{month}-01", f"{month}-{num_days:02d}"


def get_working_days_in_month(month: str, joined_on: str) -> int:
    """R7: Working days (Mon-Fri) from joined_on to end of month."""
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    start = max(date(year, m, 1), date.fromisoformat(joined_on))
    end = date(year, m, num_days)
    if start > end:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def format_attendance_record(doc: dict) -> dict:
    """Serialize MongoDB attendance document to OpenAPI AttendanceRecord."""
    history = []
    for h in doc.get("history") or []:
        changes = {
            k: {
                "from": datetime_to_epoch(v.get("from")) if k in ("punch_in", "punch_out") else v.get("from"),
                "to": datetime_to_epoch(v.get("to")) if k in ("punch_in", "punch_out") else v.get("to"),
            }
            for k, v in h.get("changes", {}).items()
        }
        history.append({
            "at": datetime_to_epoch(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch(doc.get("punch_in")),
        "punch_out": datetime_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee(doc: dict) -> dict:
    """Serialize MongoDB employee document to OpenAPI Employee."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch(doc.get("created_at")),
    }
