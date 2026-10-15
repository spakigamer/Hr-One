# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` guarantees employee uniqueness and enables O(1) single-record lookups. An index on `department` accelerates department-filtered employee queries and aggregation joins.
   - `attendance_logs`: A unique compound index on `("emp_code", 1), ("date", 1)` atomically enforces Rule R1 (one attendance record per employee per day) and speeds up monthly queries. A compound index on `("date", 1), ("emp_code", 1)` supports date-range departmental trend queries and late leaderboard scans.
   - *Considered & rejected:* An index on `status` alone was rejected due to low cardinality (only 3 distinct values: PRESENT, WFH, ON_DUTY), offering minimal selectivity while adding write overhead.

2. **Punch-in race.**
   If two identical punch-in requests arrive at the same instant, both may pass the initial application-level check. When both issue `insert_one()`, MongoDB's unique compound index on `("emp_code", 1), ("date", 1)` guarantees that one insertion succeeds while the other fails atomically with a `DuplicateKeyError` (code 11000). The API catches this error and returns `HTTP 409 Conflict: "already punched in for this date"`, ensuring zero data corruption.

3. **Ties.**
   Our leaderboard implements standard competition ranking ("1224" ranking) with secondary deterministic sorting by `emp_code` ascending. If two employees tie on late minutes at the cutoff of `limit`, both share the identical `rank` value and are returned up to the requested `limit` in deterministic `emp_code` order.

4. **Headcount.**
   The department summary does not aggregate `attendance_logs` in isolation. Instead, it queries `db.employees` for the department to establish the true total headcount, and cross-references attendance records for the month. Employees with zero attendance logs are explicitly retained with 0 present days, ensuring attendance percentages are mathematically accurate.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily and monthly department and employee statistics into dedicated rollup collections (`daily_department_summary`, `monthly_employee_summary`) updated asynchronously via MongoDB Change Streams or background worker tasks. Real-time aggregation pipelines across tens of millions of raw attendance logs would cause heavy CPU/IO bottlenecks during read spikes.
