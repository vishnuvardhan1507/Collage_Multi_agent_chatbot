from typing import Any


LABELS = {
    "attendance_percentage": "Attendance",
    "attended_classes": "Attended classes",
    "booking_date": "Date",
    "building": "Building",
    "classroom_id": "Classroom",
    "course_id": "Course ID",
    "course_name": "Course",
    "credits": "Credits",
    "department": "Department",
    "designation": "Designation",
    "email": "Email",
    "end_time": "End time",
    "faculty": "Faculty",
    "faculty_name": "Faculty",
    "from_date": "From",
    "grade": "Grade",
    "leave_id": "Leave ID",
    "marks": "Marks",
    "name": "Name",
    "office_hours": "Office hours",
    "purpose": "Purpose",
    "reason": "Reason",
    "reviewed_by": "Reviewed by",
    "semester": "Semester",
    "start_time": "Start time",
    "status": "Status",
    "student_id": "Student ID",
    "student_name": "Student",
    "to_date": "To",
    "total_classes": "Total classes",
}


def format_sql_response(result: Any) -> str | None:
    if isinstance(result, list):
        if not result:
            return "No matching record was found within your allowed scope."
        if not all(isinstance(row, dict) for row in result):
            return None
        rows = [_clean_row(row) for row in result]
        return _format_rows(rows)

    if isinstance(result, dict):
        if "rows_affected" in result:
            rows_affected = result.get("rows_affected", 0)
            if rows_affected:
                return "Done. The requested update was saved successfully."
            return "No matching record was updated within your allowed scope."
        return _format_rows([_clean_row(result)])

    return None


def _format_rows(rows: list[dict[str, Any]]) -> str:
    keys = set().union(*(row.keys() for row in rows))
    if {"attended_classes", "total_classes"}.issubset(keys):
        return _format_attendance(rows)
    if "leave_id" in keys:
        return _format_leave_requests(rows)
    if {"grade", "marks"}.intersection(keys):
        return _format_results(rows)
    if {"course_id", "course_name"}.issubset(keys):
        return _format_courses(rows)
    return _format_generic(rows)


def _format_courses(rows: list[dict[str, Any]]) -> str:
    rows = [_normalize_course_row(row) for row in rows]
    labels = {'completed': 'Completed', 'enrolled': 'Currently enrolled', 'pending': 'To be completed'}
    grouped = all(str(row.get('status', '')).lower() in labels for row in rows)
    groups = [(label, [row for row in rows if str(row.get('status', '')).lower() == status]) for status,label in labels.items()] if grouped else [('Here are the courses I found', rows)]
    lines = []
    for heading, entries in groups:
        if not entries:
            continue
        lines.extend([f'{heading} ({len(entries)}):', ''])
        for row in entries:
            details = _details(row, exclude={'course_id','course_name'} | ({'status'} if grouped else set()), preferred=('department','credits','faculty','faculty_name','status'))
            lines.append(f'- {_course_title(row)}')
            lines.extend(f'  {detail}' for detail in details)
        lines.append('')
    return "\n".join(lines)


def _normalize_course_row(row: dict[str, Any]) -> dict[str, Any]:
    if "name" in row and "faculty" not in row and "faculty_name" not in row:
        normalized = dict(row)
        normalized["faculty"] = normalized.pop("name")
        return normalized
    return row


def _format_attendance(rows: list[dict[str, Any]]) -> str:
    lines = ["Here is your attendance:", ""]
    for row in rows:
        title = _course_title(row)
        attended = row.get("attended_classes")
        total = row.get("total_classes")
        percentage = row.get("attendance_percentage")
        if percentage is None and attended is not None and total:
            percentage = round((float(attended) * 100) / float(total), 2)
        suffix = f" - {percentage}%" if percentage is not None else ""
        lines.append(f"- {title}: {attended} out of {total} classes{suffix}")
    return "\n".join(lines)


def _format_leave_requests(rows: list[dict[str, Any]]) -> str:
    lines = ["Here are the leave requests I found:", ""]
    for row in rows:
        leave_id = row.get("leave_id")
        title = f"Leave request {leave_id}" if leave_id is not None else "Leave request"
        date_range = _date_range(row)
        if date_range:
            title = f"{title}: {date_range}"
        details = _details(
            row,
            exclude={"leave_id", "from_date", "to_date"},
            preferred=("student_id", "student_name", "reason", "status", "reviewed_by"),
        )
        lines.append(f"- {title}")
        lines.extend(f"  {detail}" for detail in details)
    return "\n".join(lines)


def _format_results(rows: list[dict[str, Any]]) -> str:
    lines = ["Here are your results:", ""]
    for row in rows:
        title = _course_title(row)
        details = _details(row, exclude={"course_id", "course_name"}, preferred=("grade", "marks", "status"))
        lines.append(f"- {title}")
        lines.extend(f"  {detail}" for detail in details)
    return "\n".join(lines)


def _format_generic(rows: list[dict[str, Any]]) -> str:
    if len(rows) == 1:
        lines = ["Here is what I found:", ""]
        lines.extend(f"- {detail}" for detail in _details(rows[0]))
        return "\n".join(lines)

    lines = ["Here is what I found:", ""]
    for index, row in enumerate(rows, start=1):
        title = _best_title(row, index)
        details = _details(row)
        lines.append(title)
        lines.extend(f"  {detail}" for detail in details)
    return "\n".join(lines)


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def _course_title(row: dict[str, Any]) -> str:
    course_id = row.get("course_id")
    course_name = row.get("course_name")
    if course_id and course_name:
        return f"{course_id} - {course_name}"
    return str(course_name or course_id or "Course")


def _date_range(row: dict[str, Any]) -> str:
    from_date = row.get("from_date")
    to_date = row.get("to_date")
    if from_date and to_date and from_date != to_date:
        return f"{from_date} to {to_date}"
    return str(from_date or to_date or "")


def _details(
    row: dict[str, Any],
    exclude: set[str] | None = None,
    preferred: tuple[str, ...] = (),
) -> list[str]:
    exclude = exclude or set()
    keys = [key for key in preferred if key in row and key not in exclude]
    keys.extend(key for key in row.keys() if key not in exclude and key not in keys)
    return [f"{_label(key)}: {_value(key, row[key])}" for key in keys]


def _label(key: str) -> str:
    return LABELS.get(key, key.replace("_", " ").title())


def _value(key: str, value: Any) -> str:
    if key == "status" and isinstance(value, str):
        return value.capitalize()
    if key == "attendance_percentage":
        return f"{value}%"
    return str(value)


def _best_title(row: dict[str, Any], index: int) -> str:
    for key in ("name", "course_name", "student_name", "classroom_id", "email"):
        if row.get(key):
            return f"{index}. {row[key]}"
    return f"{index}. Record"
MENU_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def format_menu_response(rows, request, missing=None):
    if request.get('dish'):
        groups = {}
        for plan in request['plans']:
            matches = tuple((day, meal) for day in range(7) for meal in request['meals']
                            if any(row['plan_id']==plan and row['weekday']==day and row.get(meal) is not None for row in rows))
            groups.setdefault(matches, []).append('Veg' if plan=='veg' else 'Non-veg' if plan=='non_veg' else plan)
        lines = []
        for matches, plans in groups.items():
            label = ' and '.join(plans)
            if matches:
                times = ', '.join(f'{MENU_DAYS[day]} ({meal.replace("_", " ")})' for day, meal in matches)
                lines.append(f'{request["dish"]}: {times} — {label} mess.')
            else:
                lines.append(f'No recorded match for {request["dish"]} in the requested meals — {label} mess.')
        if missing:
            lines.append('Some requested menu information is unavailable; additional matches cannot be ruled out.')
        return '\n'.join(lines)
    def safe(value):
        return str(value).replace('|', '\\|').replace('\n', ' ') if value else 'Unavailable'
    sections = []
    for plan in request['plans']:
        title = 'Veg' if plan == 'veg' else 'Non-veg'
        lines = [f'{title} mess menu', '', '| Day | ' + ' | '.join(meal.replace('_',' ').capitalize() for meal in request['meals']) + ' |',
                 '| --- | ' + ' | '.join('---' for _ in request['meals']) + ' |']
        for day in request['weekdays']:
            row = next((r for r in rows if r['plan_id']==plan and r['weekday']==day), {})
            lines.append('| ' + MENU_DAYS[day] + ' | ' + ' | '.join(safe(row.get(meal)) for meal in request['meals']) + ' |')
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections)
