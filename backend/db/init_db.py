import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash


DB_PATH = Path(__file__).resolve().parent / "college.db"


SCHEMA = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS classroom_bookings;
DROP TABLE IF EXISTS classrooms;
DROP TABLE IF EXISTS leave_requests;
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS course_registrations;
DROP TABLE IF EXISTS prerequisites;
DROP TABLE IF EXISTS courses;
DROP TABLE IF EXISTS students;
DROP TABLE IF EXISTS faculty;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('student','faculty')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE faculty (
    faculty_id TEXT PRIMARY KEY REFERENCES users(user_id),
    name TEXT NOT NULL,
    department TEXT,
    designation TEXT,
    email TEXT,
    office_hours TEXT
);

CREATE TABLE students (
    student_id TEXT PRIMARY KEY REFERENCES users(user_id),
    name TEXT NOT NULL,
    department TEXT,
    semester INTEGER,
    advisor_faculty_id TEXT REFERENCES faculty(faculty_id)
);

CREATE TABLE courses (
    course_id TEXT PRIMARY KEY,
    course_name TEXT NOT NULL,
    department TEXT,
    credits INTEGER,
    faculty_id TEXT REFERENCES faculty(faculty_id)
);

CREATE TABLE prerequisites (
    course_id TEXT REFERENCES courses(course_id),
    prerequisite_course_id TEXT REFERENCES courses(course_id),
    PRIMARY KEY (course_id, prerequisite_course_id)
);

CREATE TABLE course_registrations (
    registration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT REFERENCES students(student_id),
    course_id TEXT REFERENCES courses(course_id),
    status TEXT CHECK(status IN ('enrolled','pending','completed')) DEFAULT 'enrolled',
    registered_on TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE attendance (
    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT REFERENCES students(student_id),
    course_id TEXT REFERENCES courses(course_id),
    total_classes INTEGER,
    attended_classes INTEGER
);

CREATE TABLE results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT REFERENCES students(student_id),
    course_id TEXT REFERENCES courses(course_id),
    grade TEXT,
    marks REAL
);

CREATE TABLE leave_requests (
    leave_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT REFERENCES students(student_id),
    from_date TEXT,
    to_date TEXT,
    reason TEXT,
    status TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
    reviewed_by TEXT REFERENCES faculty(faculty_id),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE classrooms (
    classroom_id TEXT PRIMARY KEY,
    building TEXT,
    capacity INTEGER
);

CREATE TABLE classroom_bookings (
    booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
    classroom_id TEXT REFERENCES classrooms(classroom_id),
    faculty_id TEXT REFERENCES faculty(faculty_id),
    booking_date TEXT,
    start_time TEXT,
    end_time TEXT,
    purpose TEXT
);
"""


FACULTY = [
    ("FAC001", "Dr. Meera Nair", "Computer Science", "Professor", "meera.nair@college.edu", "Mon/Wed 2-4 PM"),
    ("FAC002", "Dr. Arjun Rao", "Computer Science", "Associate Professor", "arjun.rao@college.edu", "Tue/Thu 10-12 PM"),
    ("FAC003", "Prof. Kavita Menon", "Electronics", "Assistant Professor", "kavita.menon@college.edu", "Fri 1-3 PM"),
    ("FAC004", "Dr. Ibrahim Khan", "Mathematics", "Professor", "ibrahim.khan@college.edu", "Mon 11-1 PM"),
    ("FAC005", "Prof. Priya Shah", "Management", "Assistant Professor", "priya.shah@college.edu", "Wed 9-11 AM"),
]

STUDENTS = [
    ("192125022", "vishnu", "Computer Science", 6, "FAC001"),
    ("192125023", "Aarav Sharma", "Computer Science", 6, "FAC001"),
    ("192125024", "Diya Patel", "Computer Science", 5, "FAC002"),
    ("192125025", "Nikhil Verma", "Computer Science", 5, "FAC002"),
    ("192125026", "Sara Thomas", "Electronics", 4, "FAC003"),
    ("192125027", "Rohan Das", "Electronics", 4, "FAC003"),
    ("192125028", "Ananya Iyer", "Mathematics", 3, "FAC004"),
    ("192125029", "Kabir Singh", "Management", 2, "FAC005"),
    ("192125030", "Maya George", "Computer Science", 6, "FAC001"),
    ("192125031", "Ishan Kulkarni", "Computer Science", 5, "FAC002"),
]

COURSES = [
    ("CS101", "Programming Fundamentals", "Computer Science", 4, "FAC001"),
    ("CS201", "Data Structures", "Computer Science", 4, "FAC002"),
    ("CS301", "Machine Learning", "Computer Science", 4, "FAC001"),
    ("CS302", "Database Systems", "Computer Science", 3, "FAC002"),
    ("CS401", "Cloud Computing", "Computer Science", 3, "FAC001"),
    ("EC201", "Digital Circuits", "Electronics", 4, "FAC003"),
    ("MA201", "Linear Algebra", "Mathematics", 3, "FAC004"),
    ("MG101", "Principles of Management", "Management", 3, "FAC005"),
]


def _insert_many(cur, sql, rows):
    cur.executemany(sql, rows)


def seed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    password = generate_password_hash("password123")

    users = [(fid, name, password, "faculty") for fid, name, *_ in FACULTY]
    users.extend((sid, name, password, "student") for sid, name, *_ in STUDENTS)
    _insert_many(cur, "INSERT INTO users(user_id, username, password_hash, role) VALUES (?, ?, ?, ?)", users)
    _insert_many(cur, "INSERT INTO faculty VALUES (?, ?, ?, ?, ?, ?)", FACULTY)
    _insert_many(cur, "INSERT INTO students VALUES (?, ?, ?, ?, ?)", STUDENTS)
    _insert_many(cur, "INSERT INTO courses VALUES (?, ?, ?, ?, ?)", COURSES)

    _insert_many(
        cur,
        "INSERT INTO prerequisites(course_id, prerequisite_course_id) VALUES (?, ?)",
        [("CS201", "CS101"), ("CS301", "CS201"), ("CS302", "CS201"), ("CS401", "CS302")],
    )

    registrations = [
        ("192125022", "CS101", "completed"), ("192125022", "CS201", "completed"),
        ("192125022", "CS301", "enrolled"), ("192125022", "CS302", "enrolled"),
        ("192125023", "CS301", "enrolled"), ("192125024", "CS201", "enrolled"),
        ("192125025", "CS302", "enrolled"), ("192125026", "EC201", "enrolled"),
        ("192125027", "EC201", "enrolled"), ("192125028", "MA201", "enrolled"),
        ("192125029", "MG101", "enrolled"), ("192125030", "CS301", "enrolled"),
        ("192125031", "CS201", "enrolled"),
    ]
    _insert_many(cur, "INSERT INTO course_registrations(student_id, course_id, status) VALUES (?, ?, ?)", registrations)

    attendance = [
        ("192125022", "CS301", 45, 41), ("192125022", "CS302", 38, 32),
        ("192125023", "CS301", 45, 39), ("192125024", "CS201", 42, 36),
        ("192125025", "CS302", 38, 28), ("192125026", "EC201", 40, 35),
        ("192125027", "EC201", 40, 31), ("192125028", "MA201", 36, 34),
        ("192125029", "MG101", 30, 27), ("192125030", "CS301", 45, 43),
        ("192125031", "CS201", 42, 33),
    ]
    _insert_many(cur, "INSERT INTO attendance(student_id, course_id, total_classes, attended_classes) VALUES (?, ?, ?, ?)", attendance)

    results = [
        ("192125022", "CS101", "A", 91), ("192125022", "CS201", "A-", 86),
        ("192125023", "CS101", "B+", 82), ("192125024", "CS101", "A", 90),
        ("192125025", "CS201", "B", 76), ("192125026", "EC201", "A-", 87),
        ("192125028", "MA201", "A", 93), ("192125029", "MG101", "B+", 80),
        ("192125030", "CS201", "A", 92), ("192125031", "CS101", "B", 74),
    ]
    _insert_many(cur, "INSERT INTO results(student_id, course_id, grade, marks) VALUES (?, ?, ?, ?)", results)

    leave_requests = [
        ("192125022", "2026-08-20", "2026-08-22", "Medical appointment", "pending", None),
        ("192125024", "2026-08-18", "2026-08-19", "Family function", "pending", None),
        ("192125026", "2026-08-25", "2026-08-25", "Competition travel", "approved", "FAC003"),
    ]
    _insert_many(
        cur,
        "INSERT INTO leave_requests(student_id, from_date, to_date, reason, status, reviewed_by) VALUES (?, ?, ?, ?, ?, ?)",
        leave_requests,
    )

    classrooms = [("Lab-302", "Innovation Block", 36), ("A-101", "Main Block", 60), ("Seminar-1", "Admin Block", 120)]
    _insert_many(cur, "INSERT INTO classrooms VALUES (?, ?, ?)", classrooms)
    bookings = [
        ("Lab-302", "FAC001", "2026-08-21", "10:00", "12:00", "ML lab"),
        ("A-101", "FAC002", "2026-08-21", "14:00", "15:00", "Database tutorial"),
    ]
    _insert_many(
        cur,
        "INSERT INTO classroom_bookings(classroom_id, faculty_id, booking_date, start_time, end_time, purpose) VALUES (?, ?, ?, ?, ?, ?)",
        bookings,
    )
    conn.commit()


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        seed(conn)


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
