# Employee Attendance & Analytics API

A high-performance asynchronous REST API for employee attendance tracking, regularization, and workforce analytics built with **FastAPI** and **MongoDB (Motor)**, strictly adhering to OpenAPI 3.0 specifications and business rules (R1–R10).

---

## Features & Implementation Highlights

- **Employee Management:** CRUD operations with deterministic pagination and department filtering.
- **Attendance Punch-In & Punch-Out:**
  - Race-condition safe punch-in via MongoDB unique compound indexes (`emp_code`, `date`).
  - Atomic punch-out updates with 24-hour validity checks (Rule R3).
  - Shift date attribution and late arrival calculations (Rules R2, R4).
- **Attendance Regularization:** Safe edits with full audit history tracking (`history` array capturing old and new values per Rule R6).
- **Workforce Analytics:**
  - Monthly employee attendance breakdown (working days, present days, work hours, overtime, attendance percentage per Rule R7).
  - Department summaries ensuring complete headcount representation including employees with zero logs (Rule R9).
  - Late leaderboard with standard competition ranking ("1224" ties per Rule R8).
  - Department attendance trends with 7-day moving averages and zero-gap date filling (Rule R10).
- **Admin Query Plan Verification:** `/admin/explain/{endpoint}` confirming full index coverage (`IXSCAN`) and zero full collection scans (`COLLSCAN: false`).

---

## Quickstart

### 1. Requirements
- Python 3.10+
- MongoDB 6.0+ (running locally on port 27017 or via Atlas)

### 2. Setup Virtual Environment
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Configuration
Create a local `.env` file (not committed to version control):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hr_attendance
```

### 4. Seed Database
Seed the initial sample dataset (employees and sample attendance records):
```bash
python sample_seed.py
```

### 5. Run the API Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

---

## Submission Artifacts

- **[REVIEW.md](REVIEW.md):** Detailed analysis of starter code defects, symptoms, and corresponding fixes.
- **[DECISIONS.md](DECISIONS.md):** Technical justifications for database indexing, concurrency control, tie-breaking, zero-log headcount calculations, and 100x scalability.
