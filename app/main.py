"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query, Path
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from motor.motor_asyncio import AsyncIOMotorDatabase
from modal.employe1 import EmployeeIn,PunchInIn,PunchOutIn,RegularizeIn

from database import connect_to_mongo, close_mongo_connection, get_db

load_dotenv()

from helpers import (
    IST,
    round_half_up,
    epoch_to_datetime,
    datetime_to_epoch,
    get_attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    get_working_days_in_month,
    format_attendance_record,
    format_employee,
)


# --------------------------------------------------------------------------- #
# Application Lifespan & Index Creation
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    client, db = await connect_to_mongo()
    # Create indexes idempotently per DATA_MODEL.md & openapi.yaml
    await db.employees.create_index("emp_code", unique=True)
    await db.employees.create_index([("department", 1), ("joined_on", 1)])
    await db.employees.create_index("joined_on")
    await db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    await db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    await db.attendance_logs.create_index([("date", 1), ("status", 1)])
    await db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    await db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    yield
    await close_mongo_connection()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health(db: AsyncIOMotorDatabase = Depends(get_db)):
    try:
        await db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
async def create_employee(body: EmployeeIn, db: AsyncIOMotorDatabase = Depends(get_db)):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    if await db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = now_utc
    try:
        await db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
async def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = await db.employees.count_documents(q)
    items = await db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size).to_list(length=page_size)
    return {
        "items": [format_employee(it) for it in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
async def punch_in(body: PunchInIn, db: AsyncIOMotorDatabase = Depends(get_db)):
    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(422, "Invalid status for punch-in")

    emp = await db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_to_datetime(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    d = get_attendance_date(ts_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    if await db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    late_min = compute_late_minutes(ts_utc, emp.get("shift_start", "09:30"), d)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        await db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
async def punch_out(body: PunchOutIn, db: AsyncIOMotorDatabase = Depends(get_db)):
    emp = await db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_to_datetime(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes most recent record whose punch_in is <= punched_at
    record = await db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found for this punch-out")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "That record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    punch_in_ist = punch_in_dt.astimezone(IST)
    punch_out_ist = punch_out_dt.astimezone(IST)
    work_hours = compute_work_hours(punch_in_ist, punch_out_ist)
    ot = compute_overtime(punch_out_ist, emp.get("shift_end", "18:30"), emp.get("shift_start", "09:30"), record["date"])
    half_day = compute_half_day(work_hours)

    updated = await db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": ot,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "That record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
async def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    emp_code = emp_code if isinstance(emp_code, str) else None
    date_from = date_from if isinstance(date_from, str) else None
    date_to = date_to if isinstance(date_to, str) else None
    status = status if isinstance(status, str) else None
    page = page if isinstance(page, int) else 1
    page_size = page_size if isinstance(page_size, int) else 20

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")

    q = {}
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

    total = await db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    docs = await cursor.to_list(length=page_size)
    items = [format_attendance_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
async def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    emp = await db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = await db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else rec.get("status")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")

    if new_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        if body.punch_in is not None:
            target_punch_in = epoch_to_datetime(body.punch_in)
        else:
            target_punch_in = rec.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(422, "A presence status requires punch_in")

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        calc_date = get_attendance_date(target_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if calc_date != date:
            raise HTTPException(422, f"punch_in belongs to date {calc_date}, not {date}")

        if body.punch_out is not None:
            target_punch_out = epoch_to_datetime(body.punch_out)
        else:
            target_punch_out = rec.get("punch_out")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
            if target_punch_out <= target_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
            target_work_hours = compute_work_hours(target_punch_in.astimezone(IST), target_punch_out.astimezone(IST))
            target_overtime_minutes = compute_overtime(target_punch_out.astimezone(IST), emp.get("shift_end", "18:30"), emp.get("shift_start", "09:30"), date)
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(target_punch_in.astimezone(IST), emp.get("shift_start", "09:30"), date)

    # Check for actual changes
    changes = {}
    old_status = rec.get("status")
    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    old_wh = rec.get("work_hours")
    old_lm = rec.get("late_minutes", 0)
    old_ot = rec.get("overtime_minutes", 0)
    old_hd = rec.get("half_day", False)

    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_in != target_punch_in:
        changes["punch_in"] = {"from": old_in, "to": target_punch_in}
    if old_out != target_punch_out:
        changes["punch_out"] = {"from": old_out, "to": target_punch_out}
    if old_wh != target_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": target_work_hours}
    if old_lm != target_late_minutes:
        changes["late_minutes"] = {"from": old_lm, "to": target_late_minutes}
    if old_ot != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": target_overtime_minutes}
    if old_hd != target_half_day:
        changes["half_day"] = {"from": old_hd, "to": target_half_day}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    if "history" not in rec or not rec.get("history"):
        history_match = {"$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}]}
    else:
        history_match = {"history": {"$size": len(rec["history"])}}

    updated = await db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], **history_match},
        {
            "$set": {
                "status": new_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent modification conflict")

    return format_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
async def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    emp = await db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    working_days = get_working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}}
    ]
    cursor = db.attendance_logs.aggregate(pipeline)
    logs = await cursor.to_list(length=None)

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for log in logs:
        d = date.fromisoformat(log["date"])
        is_weekday = d.weekday() < 5
        st = log.get("status")
        if is_weekday and st in ("PRESENT", "WFH", "ON_DUTY"):
            present_days += 0.5 if log.get("half_day") else 1.0
        if st == "LEAVE":
            leave_days += 1
        lm = log.get("late_minutes", 0)
        if lm > 0:
            late_count += 1
            total_late_minutes += lm
        total_overtime_minutes += log.get("overtime_minutes", 0)

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

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
async def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
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
                    }
                ],
                "as": "logs",
            }
        },
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "logs": {"$push": "$logs"}}},
        {"$sort": {"_id": 1}},
    ]

    cursor = db.employees.aggregate(pipeline)
    raw_dept_items = await cursor.to_list(length=None)

    items = []
    for dept_doc in raw_dept_items:
        dept_name = dept_doc["_id"]
        headcount = dept_doc["headcount"]
        if headcount == 0:
            continue

        # Flatten logs from nested array of employee logs
        all_logs = [log for emp_logs in dept_doc.get("logs", []) for log in emp_logs]

        present_days = 0.0
        leave_count = 0
        on_duty_count = 0
        late_count = 0
        total_late_minutes = 0
        work_hours_list = []

        for log in all_logs:
            d = date.fromisoformat(log["date"])
            is_weekday = d.weekday() < 5
            st = log.get("status")

            if is_weekday and st in ("PRESENT", "WFH", "ON_DUTY"):
                present_days += 0.5 if log.get("half_day") else 1.0

            if st == "LEAVE":
                leave_count += 1
            elif st == "ON_DUTY":
                on_duty_count += 1

            if st in ("PRESENT", "WFH", "ON_DUTY") and log.get("work_hours") is not None:
                work_hours_list.append(log["work_hours"])

            lm = log.get("late_minutes", 0)
            if lm > 0:
                late_count += 1
                total_late_minutes += lm

        avg_wh = round_half_up(sum(work_hours_list) / len(work_hours_list), 2) if work_hours_list else None

        items.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_wh,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
async def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    cursor = db.attendance_logs.aggregate(pipeline)
    raw_items = await cursor.to_list(length=None)

    items = []
    rank = 1
    for i, it in enumerate(raw_items):
        if i > 0 and it["total_late_minutes"] != raw_items[i - 1]["total_late_minutes"]:
            rank = i + 1
        if rank > limit:
            break
        items.append({"rank": rank, **it})

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
async def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from_date = date.fromisoformat(from_)
    to_date = date.fromisoformat(to)
    if to_date < from_date:
        raise HTTPException(422, "'to' date cannot be earlier than 'from' date")
    if (to_date - from_date).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_emp_count = await db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(404, "Department not found")

    dept_employees = await db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}).to_list(length=None)
    dept_emp_codes = [e["emp_code"] for e in dept_employees]

    logs = await db.attendance_logs.find(
        {"emp_code": {"$in": dept_emp_codes}, "date": {"$gte": from_, "$lte": to}}
    ).to_list(length=None)

    logs_by_date = defaultdict(list)
    for log in logs:
        logs_by_date[log["date"]].append(log)

    items = []
    curr = from_date
    while curr <= to_date:
        d_str = curr.isoformat()
        is_working_day = curr.weekday() < 5
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= d_str)
        day_logs = logs_by_date.get(d_str, [])

        present_count = 0.0
        for l in day_logs:
            if l.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if l.get("half_day") else 1.0

        late_count = sum(1 for l in day_logs if l.get("late_minutes", 0) > 0)

        if is_working_day and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
        else:
            att_rate = None

        row = {
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
        }

        # 7-day moving average over preceding non-null rates in range
        window_start_idx = max(0, len(items) - 6)
        window_rates = [it["attendance_rate"] for it in items[window_start_idx:] if it["attendance_rate"] is not None]
        if att_rate is not None:
            window_rates.append(att_rate)

        if window_rates:
            row["moving_avg_7d"] = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            row["moving_avg_7d"] = None

        items.append(row)
        curr += timedelta(days=1)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
async def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    emp_code = emp_code if isinstance(emp_code, str) else None
    month = month if isinstance(month, str) else None
    department = department if isinstance(department, str) else None
    date_from = date_from if isinstance(date_from, str) else None
    date_to = date_to if isinstance(date_to, str) else None
    status = status if isinstance(status, str) else None
    from_ = from_ if isinstance(from_, str) else None
    to = to if isinstance(to, str) else None
    page = page if isinstance(page, int) else 1
    page_size = page_size if isinstance(page_size, int) else 20
    limit = limit if isinstance(limit, int) else 10

    if endpoint == "attendance_list":
        q = {}
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
        cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
        explain_doc = await cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"}}}
        ]
        explain_doc = await db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        match_q = {"joined_on": {"$lte": f"{month}-{num_days:02d}"}}
        if department:
            match_q["department"] = department
        pipeline = [
            {"$match": match_q},
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
                                        {"$gte": ["$date", f"{month}-01"]},
                                        {"$lte": ["$date", f"{month}-{num_days:02d}"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "logs": {"$push": "$logs"}}},
            {"$sort": {"_id": 1}},
        ]
        explain_doc = await db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = [
            {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        explain_doc = await db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}}
        ]
        explain_doc = await db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
