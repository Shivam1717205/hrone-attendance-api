# Implementation decisions

1. **Indexes.** Unique employee-code and attendance `(emp_code, date)` indexes protect natural keys. Department/code and department/join-date indexes support employee lists and headcount. Attendance date/status and employee/date/punch-in indexes support lists, monthly analytics and locating punch-ins. I rejected a `history` index: no query searches it, and writes would pay the cost.
2. **Punch-in race.** Both requests may check the employee, but the unique attendance index permits only one insert for that code and date. The losing insert raises `DuplicateKeyError`, mapped to 409; the winner receives 201.
3. **Ties.** `$rank` ranks by total late minutes, not row position. Ties share a rank and the next rank is skipped. The cutoff filters rank, so everyone tied at the limit is returned; tied rows are ordered by employee code.
4. **Headcount.** The summary starts with employees who joined by month-end and looks up each employee's logs. It groups per employee before department totals, so employees without logs still count once.
5. **100x data.** I would benchmark explain plans and latency at that scale, then consider precomputed monthly summaries for common periods. Source logs would remain authoritative, with summaries refreshed to reflect corrections.
