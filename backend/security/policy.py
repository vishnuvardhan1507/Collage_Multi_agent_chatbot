ROLE_CAPABILITY_MATRIX = """
Students:
- SELECT attendance, course registrations, results, prerequisites, faculty public info, and own academic data only for their own student_id.
- INSERT leave_requests only for own student_id with status forced to 'pending'.
- INSERT course_registrations only for own student_id, after considering prerequisites.

Faculty:
- SELECT own assigned courses.
- SELECT students only when they are advised by this faculty or enrolled in a course taught by this faculty.
- SELECT student results and course details only for their own courses/students.
- SELECT classroom availability.
- INSERT classroom_bookings only for own faculty_id.
- UPDATE leave_requests only by setting status/reviewed_by, scoped to advised students or students in faculty-taught courses.

System-wide:
- attendance is read-only for every role. No UPDATE or DELETE on attendance.
- No DROP or ALTER.
- All writes except student leave request insert, student course booking insert, faculty classroom booking insert, and scoped faculty leave status update are forbidden.
"""
