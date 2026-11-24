"""Initialize a NEW database with current STU/FAC branch demo accounts."""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from datetime import date, timedelta
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config
from db.migrate import migrate

DB_PATH = Path(Config.DATABASE_PATH)

ACADEMIC_NOTICE_SOURCES = [
    {'id': 'aicte-curriculum', 'title': 'AICTE model curriculum announcements',
     'domains': ['aicte.gov.in'],
     'index_url': 'https://www.aicte.gov.in/education/model-syllabus'},
]

# Candidate venue identities. Collection enables a venue only after fetching its
# Crossref identity and official review policy; publisher membership alone is insufficient.
RESEARCH_SOURCES = [
    {'id': 'acm-tosem', 'title': 'ACM Transactions on Software Engineering and Methodology',
     'issns': ['1049-331X'], 'publisher': 'ACM', 'departments': ['CSE', 'IT'],
     'domains': ['acm.org'], 'policy_url': 'https://www.acm.org/publications/policies/peer-review',
     'recognition_urls': ['https://www.acm.org/media-center']},
    {'id': 'ieee-ted', 'title': 'IEEE Transactions on Electron Devices', 'issns': ['0018-9383'],
     'publisher': 'IEEE', 'departments': ['ECE', 'EEE'], 'domains': ['ieee.org'],
     'policy_url': 'https://journals.ieeeauthorcenter.ieee.org/submit-your-article-for-peer-review/about-the-peer-review-process/',
     'recognition_urls': ['https://eds.ieee.org/publications/transactions-on-electron-devices']},
    {'id': 'siam-jam', 'title': 'SIAM Journal on Applied Mathematics', 'issns': ['0036-1399'],
     'publisher': 'SIAM', 'departments': ['CSE', 'EEE', 'ME'], 'domains': ['siam.org'],
     'policy_url': 'https://www.siam.org/publications/siam-journals/',
     'recognition_urls': ['https://www.siam.org/publications/siam-news/']},
    {'id': 'nature-mi', 'title': 'Nature Machine Intelligence', 'issns': ['2522-5839'],
     'publisher': 'Springer Nature', 'departments': ['CSE', 'IT'], 'domains': ['nature.com'],
     'policy_url': 'https://www.nature.com/natmachintell/submission-guidelines/editorial-process',
     'recognition_urls': ['https://www.nature.com/natmachintell/research-highlights']},
    {'id': 'elsevier-rp', 'title': 'Research Policy', 'issns': ['0048-7333'],
     'publisher': 'Elsevier', 'departments': ['MBA'], 'domains': ['elsevier.com', 'sciencedirect.com'],
     'policy_url': 'https://www.sciencedirect.com/journal/research-policy/publish/guide-for-authors',
     'recognition_urls': ['https://www.elsevier.com/connect']},
    {'id': 'wiley-smj', 'title': 'Strategic Management Journal', 'issns': ['0143-2095'],
     'publisher': 'Wiley', 'departments': ['MBA'], 'domains': ['wiley.com'],
     'policy_url': 'https://onlinelibrary.wiley.com/page/journal/10970266/homepage/forauthors.html',
     'recognition_urls': ['https://newsroom.wiley.com/press-releases/']},
    {'id': 'taylor-ijpr', 'title': 'International Journal of Production Research', 'issns': ['0020-7543'],
     'publisher': 'Taylor & Francis', 'departments': ['ME', 'MBA'], 'domains': ['tandfonline.com', 'taylorandfrancis.com', 'taylorandfrancisgroup.com'],
     'policy_url': 'https://www.tandfonline.com/journals/tprs20/about-this-journal',
     'recognition_urls': ['https://newsroom.taylorandfrancisgroup.com/']},
    {'id': 'asme-jmd', 'title': 'Journal of Mechanical Design', 'issns': ['1050-0472'],
     'publisher': 'ASME', 'departments': ['ME'], 'domains': ['asme.org'],
     'policy_url': 'https://www.asme.org/publications-submissions/journals/information-for-authors',
     'recognition_urls': ['https://www.asme.org/topics-resources/content']},
    {'id': 'asce-jse', 'title': 'Journal of Structural Engineering', 'issns': ['0733-9445'],
     'publisher': 'ASCE', 'departments': ['CE'], 'domains': ['ascelibrary.org', 'ascelibrary.com', 'asce.org'],
     'policy_url': 'https://ascelibrary.com/author-center/submission-peer-review',
     'recognition_urls': ['https://www.asce.org/publications-and-news/civil-engineering-source']},
]


# Transport seed data: stops ordered by morning pickup; return order is reversed.
TRANSPORT_SERVICES = [('campus',
  'HITEC City near Cyber Towers, Hyderabad',
  'Asia/Kolkata',
  'Monday-Friday on teaching days; no scheduled service on weekends or institute holidays',
  '08:45',
  '16:30',
  20.0,
  'Approximate straight-line planning radius, not road distance or guaranteed coverage',
  5,
  'INR',
  'per student per calendar month, round trip on teaching days; route fare applies at every stop; no daily or '
  'one-way tariff defined',
  'Fictional demo transport dataset; approximate radii and scheduled times, not surveyed distances or live '
  'tracking')]
BUS_ROUTES = [('B01', 'campus', 'Northwest: Bachupally - Nizampet - KPHB', 2200),
 ('B02', 'campus', 'West: BHEL - Chandanagar - Hafeezpet', 2200),
 ('B03', 'campus', 'West-southwest: Tellapur - Nallagandla - Gachibowli', 2500),
 ('B04', 'campus', 'Southwest: Kokapet - Narsingi - Financial District', 2500),
 ('B05', 'campus', 'South: Golconda - Tolichowki - Manikonda', 2200),
 ('B06', 'campus', 'Southeast: Mehdipatnam - Banjara Hills - Jubilee Hills', 2200),
 ('B07', 'campus', 'East: Secunderabad - Begumpet - Ameerpet', 2500),
 ('B08', 'campus', 'Northeast: Jeedimetla - Balanagar - Moosapet', 2300),
 ('B09', 'campus', 'North: Pragathi Nagar - Miyapur - Kondapur', 1800),
 ('B10', 'campus', 'Local: Raidurg - Madhapur - Ayyappa Society', 1000)]
BUS_STOPS = [('B01', 1, 'Bachupally', 'Bachupally junction', 12.0, '07:25', '17:50'),
 ('B01', 2, 'Nizampet', 'Nizampet village bus stop', 9.0, '07:45', '17:30'),
 ('B01', 3, 'JNTU', 'JNTU main-road bus stop', 7.0, '08:00', '17:15'),
 ('B01', 4, 'KPHB', 'KPHB Phase 4 main-road stop', 5.0, '08:15', '17:00'),
 ('B02', 1, 'BHEL', 'BHEL township main gate', 12.0, '07:25', '17:50'),
 ('B02', 2, 'Chandanagar', 'Chandanagar main-road bus stop', 9.0, '07:45', '17:30'),
 ('B02', 3, 'Madinaguda', 'Madinaguda main-road stop', 7.0, '08:00', '17:15'),
 ('B02', 4, 'Hafeezpet', 'Hafeezpet railway station approach', 5.0, '08:15', '17:00'),
 ('B03', 1, 'Tellapur', 'Tellapur junction', 12.0, '07:15', '18:00'),
 ('B03', 2, 'Nallagandla', 'Nallagandla main-road junction', 9.0, '07:35', '17:40'),
 ('B03', 3, 'Lingampally', 'Lingampally railway station approach', 10.0, '07:50', '17:25'),
 ('B03', 4, 'Gachibowli', 'Gachibowli junction', 4.0, '08:15', '17:00'),
 ('B04', 1, 'Kokapet', 'Kokapet village junction', 10.0, '07:20', '17:55'),
 ('B04', 2, 'Narsingi', 'Narsingi main-road junction', 10.0, '07:40', '17:35'),
 ('B04', 3, 'Financial District', 'Wipro Circle public-road stop', 7.0, '08:00', '17:15'),
 ('B04', 4, 'Nanakramguda', 'Nanakramguda junction', 6.0, '08:15', '17:00'),
 ('B05', 1, 'Golconda', 'Golconda main-road bus stop', 10.0, '07:20', '17:55'),
 ('B05', 2, 'Tolichowki', 'Tolichowki main-road junction', 8.0, '07:40', '17:35'),
 ('B05', 3, 'Shaikpet', 'Shaikpet junction', 6.0, '07:55', '17:20'),
 ('B05', 4, 'Manikonda', 'Manikonda main-road junction', 6.0, '08:10', '17:05'),
 ('B05', 5, 'Khajaguda', 'Khajaguda junction', 4.0, '08:25', '16:50'),
 ('B06', 1, 'Mehdipatnam', 'Mehdipatnam bus-stop approach', 11.0, '07:20', '17:55'),
 ('B06', 2, 'Masab Tank', 'Masab Tank main-road stop', 11.0, '07:35', '17:40'),
 ('B06', 3, 'Banjara Hills', 'Road No. 12 main-road junction', 8.0, '07:55', '17:20'),
 ('B06', 4, 'Jubilee Hills', 'Jubilee Hills Check Post', 4.0, '08:15', '17:00'),
 ('B07', 1, 'Secunderabad', 'Jubilee Bus Station public approach', 13.0, '07:10', '18:05'),
 ('B07', 2, 'Paradise', 'Paradise junction', 12.0, '07:25', '17:50'),
 ('B07', 3, 'Begumpet', 'Begumpet metro station approach', 9.0, '07:45', '17:30'),
 ('B07', 4, 'Ameerpet', 'Ameerpet metro station approach', 7.0, '08:00', '17:15'),
 ('B07', 5, 'Yousufguda', 'Yousufguda metro station approach', 5.0, '08:15', '17:00'),
 ('B08', 1, 'Jeedimetla', 'Jeedimetla main-road bus stop', 12.0, '07:20', '17:55'),
 ('B08', 2, 'Chintal', 'Chintal main-road stop', 10.0, '07:40', '17:35'),
 ('B08', 3, 'Balanagar', 'Balanagar junction', 8.0, '07:55', '17:20'),
 ('B08', 4, 'Moosapet', 'Moosapet metro station approach', 6.0, '08:10', '17:05'),
 ('B08', 5, 'Kukatpally', 'Kukatpally metro station approach', 6.0, '08:20', '16:55'),
 ('B09', 1, 'Pragathi Nagar', 'Pragathi Nagar main-road junction', 10.0, '07:30', '17:45'),
 ('B09', 2, 'Miyapur', 'Miyapur metro station approach', 8.0, '07:50', '17:25'),
 ('B09', 3, 'Allwyn Crossroads', 'Allwyn Crossroads stop', 6.0, '08:05', '17:10'),
 ('B09', 4, 'Kondapur', 'Kondapur junction', 3.0, '08:20', '16:55'),
 ('B10', 1, 'Raidurg', 'Raidurg metro station approach', 2.0, '08:00', '17:15'),
 ('B10', 2, 'Durgam Cheruvu', 'Durgam Cheruvu metro station approach', 2.0, '08:10', '17:05'),
 ('B10', 3, 'Madhapur', 'Madhapur main-road bus stop', 2.0, '08:20', '16:55'),
 ('B10', 4, 'Ayyappa Society', 'Ayyappa Society main-road junction', 2.0, '08:30', '16:45')]
BUS_LOCALITY_ALIASES = [('B01', 'bachupally', 1),
 ('B01', 'nizampet', 2),
 ('B01', 'jntu', 3),
 ('B01', 'jntuh', 3),
 ('B01', 'kphb', 4),
 ('B01', 'kphb phase 4', 4),
 ('B01', 'kukatpally housing board', 4),
 ('B02', 'bhel', 1),
 ('B02', 'chandanagar', 2),
 ('B02', 'chanda nagar', 2),
 ('B02', 'madinaguda', 3),
 ('B02', 'hafeezpet', 4),
 ('B03', 'tellapur', 1),
 ('B03', 'nallagandla', 2),
 ('B03', 'lingampally', 3),
 ('B03', 'lingampalli', 3),
 ('B03', 'gachibowli', 4),
 ('B04', 'kokapet', 1),
 ('B04', 'narsingi', 2),
 ('B04', 'financial district', 3),
 ('B04', 'wipro circle', 3),
 ('B04', 'nanakramguda', 4),
 ('B05', 'golconda', 1),
 ('B05', 'golkonda', 1),
 ('B05', 'tolichowki', 2),
 ('B05', 'toli chowki', 2),
 ('B05', 'shaikpet', 3),
 ('B05', 'manikonda', 4),
 ('B05', 'khajaguda', 5),
 ('B06', 'mehdipatnam', 1),
 ('B06', 'masab tank', 2),
 ('B06', 'masabtank', 2),
 ('B06', 'banjara hills', 3),
 ('B06', 'jubilee hills', 4),
 ('B06', 'jubilee hills check post', 4),
 ('B07', 'secunderabad', 1),
 ('B07', 'jbs', 1),
 ('B07', 'jubilee bus station', 1),
 ('B07', 'paradise', 2),
 ('B07', 'begumpet', 3),
 ('B07', 'ameerpet', 4),
 ('B07', 'yousufguda', 5),
 ('B07', 'yusufguda', 5),
 ('B08', 'jeedimetla', 1),
 ('B08', 'chintal', 2),
 ('B08', 'balanagar', 3),
 ('B08', 'moosapet', 4),
 ('B08', 'kukatpally', 5),
 ('B09', 'pragathi nagar', 1),
 ('B09', 'pragatinagar', 1),
 ('B09', 'miyapur', 2),
 ('B09', 'allwyn crossroads', 3),
 ('B09', 'allwyn x roads', 3),
 ('B09', 'kondapur', 4),
 ('B10', 'raidurg', 1),
 ('B10', 'raidurgam', 1),
 ('B10', 'durgam cheruvu', 2),
 ('B10', 'madhapur', 3),
 ('B10', 'ayyappa society', 4)]

# Initial institutional dataset; prices are INR per student per month.
HOSTEL_ROOMS = [
    (f'H{sharing}{"A" if ac else "N"}-{index}', sharing, ac, occupied,
     {2: 6000, 3: 5000, 4: 4000}[sharing] + 2000 * ac)
    for sharing in (2, 3, 4) for ac in (0, 1)
    for index, occupied in enumerate((sharing, sharing - 1, 0), 1)
]
MESS_PLANS = [('veg', 'Veg', 3000), ('non_veg', 'Non-veg', 4000)]
# Monday=0 through Sunday=6. Each record explicitly names all four meals.
MESS_MENUS = [
    {
        'plan_id': 'veg',
        'weekday': 0,
        'breakfast': 'Idli, sambar, coconut chutney, milk',
        'lunch': 'Rice, dal, beans curry, curd',
        'evening_snacks': 'Samosa, mint chutney, tea or coffee',
        'dinner': 'Chapati, chana masala, rice, salad',
    },
    {
        'plan_id': 'veg',
        'weekday': 1,
        'breakfast': 'Poha, banana, tea',
        'lunch': 'Rice, rajma, cabbage curry, curd',
        'evening_snacks': 'Vegetable sandwich, tea or coffee',
        'dinner': 'Chapati, dal tadka, mixed vegetable curry, rice, and curd',
    },
    {
        'plan_id': 'veg',
        'weekday': 2,
        'breakfast': 'Dosa, sambar, chutney, milk',
        'lunch': 'Vegetable pulao, dal, raita',
        'evening_snacks': 'Onion pakora, tomato chutney, tea or coffee',
        'dinner': 'Chapati, palak paneer, rice, salad',
    },
    {
        'plan_id': 'veg',
        'weekday': 3,
        'breakfast': 'Upma, fruit, tea',
        'lunch': 'Rice, sambar, potato fry, curd',
        'evening_snacks': 'Sweet corn chaat, tea or coffee',
        'dinner': 'Chapati, green gram dal, vegetable kurma, rice',
    },
    {
        'plan_id': 'veg',
        'weekday': 4,
        'breakfast': 'Puri, potato curry, milk',
        'lunch': 'Rice, dal, bhindi fry, curd',
        'evening_snacks': 'Vegetable cutlet, tomato sauce, tea or coffee',
        'dinner': 'Chapati, kadai paneer, vegetable fried rice',
    },
    {
        'plan_id': 'veg',
        'weekday': 5,
        'breakfast': 'Pongal, chutney, banana',
        'lunch': 'Lemon rice, dal, carrot curry, curd',
        'evening_snacks': 'Pav bhaji, tea or coffee',
        'dinner': 'Chapati, chickpea curry, rice, salad',
    },
    {
        'plan_id': 'veg',
        'weekday': 6,
        'breakfast': 'Aloo paratha, curd, tea',
        'lunch': 'Vegetable biryani, raita, fruit',
        'evening_snacks': 'Bhel puri, fresh lime juice',
        'dinner': 'Chapati, dal makhani, rice, mixed vegetables',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 0,
        'breakfast': 'Idli, sambar, boiled egg, milk',
        'lunch': 'Rice, chicken curry, dal, curd',
        'evening_snacks': 'Samosa, mint chutney, tea or coffee',
        'dinner': 'Chapati, egg masala, rice, salad',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 1,
        'breakfast': 'Poha, boiled egg, tea',
        'lunch': 'Rice, fish curry, cabbage curry, curd',
        'evening_snacks': 'Vegetable sandwich, tea or coffee',
        'dinner': 'Chapati, chicken curry, rice, dal, curd',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 2,
        'breakfast': 'Dosa, chutney, omelette, milk',
        'lunch': 'Chicken pulao, dal, raita',
        'evening_snacks': 'Onion pakora, tomato chutney, tea or coffee',
        'dinner': 'Chapati, egg curry, rice, salad',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 3,
        'breakfast': 'Upma, boiled egg, tea',
        'lunch': 'Rice, chicken curry, potato fry, curd',
        'evening_snacks': 'Sweet corn chaat, tea or coffee',
        'dinner': 'Chapati, fish curry, rice, dal',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 4,
        'breakfast': 'Puri, potato curry, boiled egg, milk',
        'lunch': 'Rice, fish curry, dal, curd',
        'evening_snacks': 'Vegetable cutlet, tomato sauce, tea or coffee',
        'dinner': 'Chapati, egg masala, vegetable fried rice',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 5,
        'breakfast': 'Pongal, chutney, omelette',
        'lunch': 'Rice, chicken curry, carrot curry, curd',
        'evening_snacks': 'Pav bhaji, tea or coffee',
        'dinner': 'Chapati, egg curry, rice, salad',
    },
    {
        'plan_id': 'non_veg',
        'weekday': 6,
        'breakfast': 'Aloo paratha, boiled egg, tea',
        'lunch': 'Chicken biryani, raita, fruit',
        'evening_snacks': 'Bhel puri, fresh lime juice',
        'dinner': 'Chapati, fish curry, rice, dal',
    },
]

SCHEMA = """
PRAGMA foreign_keys = ON;


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

# Room ID, building, seats, AC (1=yes, 0=no), projectors, computers.
CLASSROOMS = [
    ('Lab-302', 'Innovation Block', 36, 1, 1, 36),
    ('A-101', 'Main Block', 60, 0, 1, 0),
    ('Seminar-1', 'Admin Block', 120, 1, 2, 0),
    ('Lab-303', 'Innovation Block', 40, 1, 1, 40),
    ('Lab-201', 'Main Block', 30, 0, 1, 30),
    ('A-102', 'Main Block', 60, 1, 1, 0),
    ('A-103', 'Main Block', 40, 0, 0, 0),
    ('B-201', 'Academic Block', 80, 1, 1, 0),
    ('B-202', 'Academic Block', 50, 0, 1, 0),
    ('Seminar-2', 'Admin Block', 100, 1, 2, 10),
]

# ID, name, branch, registered email
FACULTY = [
    ('FAC001', 'Dr. Vishnuvardhan', 'Computer Science', 'faculty@example.com'),
    ('FAC002', 'Dr. Kavita Rao', 'Electronics', None),
    ('FAC003', 'Dr. Anand Iyer', 'Mathematics', None),
    ('FAC004', 'Dr. Priya Shah', 'Management', None),
    ('FAC005', 'Dr. Rohan Nair', 'Information Technology', None),
    ('FAC006', 'Dr. Meera Reddy', 'Mechanical Engineering', None),
    ('FAC007', 'Dr. Arjun Kumar', 'Civil Engineering', None),
]

# ID, name, branch, semester, assigned faculty, registered email
STUDENTS = [
    ('STU001', 'Vishnu', 'Computer Science', 4, 'FAC001', 'student@example.com'),
    ('STU002', 'Ananya Rao', 'Computer Science', 4, 'FAC001', None),
    ('STU003', 'Aarav Shah', 'Electronics', 4, 'FAC002', None),
    ('STU004', 'Diya Nair', 'Electronics', 4, 'FAC002', None),
    ('STU005', 'Kabir Das', 'Mathematics', 4, 'FAC003', None),
    ('STU006', 'Meera Iyer', 'Mathematics', 4, 'FAC003', None),
    ('STU007', 'Rohan Patel', 'Management', 4, 'FAC004', None),
    ('STU008', 'Sara Khan', 'Management', 4, 'FAC004', None),
    ('STU009', 'Ishan Gupta', 'Information Technology', 4, 'FAC005', None),
    ('STU010', 'Maya Thomas', 'Information Technology', 4, 'FAC005', None),
    ('STU011', 'Arjun Reddy', 'Mechanical Engineering', 4, 'FAC006', None),
    ('STU012', 'Priya Menon', 'Mechanical Engineering', 4, 'FAC006', None),
    ('STU013', 'Nikhil Kumar', 'Civil Engineering', 4, 'FAC007', None),
    ('STU014', 'Kavya Singh', 'Civil Engineering', 4, 'FAC007', None),
]

# Eight branch subjects, with two common subjects added as branch-owned offerings.
CURRICULA = {
    'Computer Science': ('CS', ['Programming Fundamentals', 'Data Structures', 'Machine Learning', 'Database Systems', 'Operating Systems', 'Computer Architecture', 'Artificial Intelligence', 'Cloud Computing']),
    'Electronics': ('EC', ['Circuit Theory', 'Electronic Devices', 'Digital Electronics', 'Analog Electronics', 'Signals and Systems', 'Microprocessors', 'Embedded Systems', 'VLSI Design']),
    'Mathematics': ('MA', ['Calculus', 'Linear Algebra', 'Differential Equations', 'Probability Theory', 'Abstract Algebra', 'Numerical Methods', 'Real Analysis', 'Mathematical Modelling']),
    'Management': ('MG', ['Principles of Management', 'Financial Accounting', 'Marketing Management', 'Organizational Behaviour', 'Business Economics', 'Human Resource Management', 'Strategic Management', 'Corporate Finance']),
    'Information Technology': ('IT', ['Information Systems Fundamentals', 'Web Programming', 'Computer Networks', 'Software Engineering', 'Information Security', 'Systems Administration', 'Network Security', 'Enterprise Application Development']),
    'Mechanical Engineering': ('ME', ['Engineering Mechanics', 'Engineering Drawing', 'Thermodynamics', 'Fluid Mechanics', 'Manufacturing Processes', 'Strength of Materials', 'Heat Transfer', 'Machine Design']),
    'Civil Engineering': ('CE', ['Engineering Geology', 'Construction Materials', 'Surveying', 'Structural Analysis', 'Geotechnical Engineering', 'Transportation Engineering', 'Reinforced Concrete Design', 'Construction Project Management']),
}
SUFFIXES = ['001', '002', '101', '201', '301', '302', '303', '304', '401', '402']
COMMON = ['Professional Communication', 'Environmental Studies']
STATUSES = ['completed'] * 4 + ['enrolled'] * 4 + ['pending'] * 2


# Maintained teaching outlines, not a claim of an officially approved syllabus.
# Each named unit records its topics and an assessable learning outcome.
SYLLABUS_OUTLINES = {
    'Professional Communication': [('Communication', 'audience analysis; active listening', 'Adapt communication to an audience'), ('Professional writing', 'reports; email etiquette; resumes', 'Write clear professional documents'), ('Presentation', 'public speaking; interviews; group discussion', 'Present and defend an idea')],
    'Environmental Studies': [('Ecosystems', 'biodiversity; food chains; conservation', 'Explain ecosystem relationships'), ('Resources', 'water management; energy; sustainability', 'Compare sustainable resource choices'), ('Pollution', 'air pollution; waste management; climate change', 'Assess environmental impacts')],
    'Programming Fundamentals': [('Programming basics', 'variables; types; control flow', 'Trace program execution'), ('Functions and collections', 'functions; arrays; strings', 'Decompose a problem into functions'), ('Problem solving', 'algorithms; debugging; testing', 'Implement and test a small program')],
    'Data Structures': [('Linear structures', 'arrays; linked lists; stacks; queues', 'Select a linear data structure'), ('Trees and graphs', 'binary search trees; graph traversal; heaps', 'Implement traversal algorithms'), ('Algorithm analysis', 'sorting; searching; time complexity', 'Compare algorithm efficiency')],
    'Machine Learning': [('Learning foundations', 'features; train-test splits; bias and variance', 'Design a leakage-free learning experiment'), ('Supervised learning', 'linear regression; logistic regression; decision trees', 'Compare supervised learning models'), ('Unsupervised learning and evaluation', 'clustering; principal component analysis; precision; recall; cross-validation', 'Evaluate and interpret model performance')],
    'Database Systems': [('Relational modelling', 'entities; relationships; keys; normalization', 'Design a normalized relational schema'), ('Querying', 'SQL joins; aggregation; subqueries', 'Retrieve related data using SQL'), ('Database reliability', 'transactions; indexes; concurrency; recovery', 'Explain consistency and query performance')],
    'Operating Systems': [('Processes', 'processes; threads; CPU scheduling', 'Compare scheduling strategies'), ('Memory', 'paging; virtual memory; page replacement', 'Explain virtual memory management'), ('Resources', 'synchronization; deadlocks; file systems', 'Reason about concurrent resource access')],
    'Computer Architecture': [('Processor design', 'instruction sets; datapaths; control units', 'Trace instruction execution'), ('Performance', 'pipelining; hazards; cache memory', 'Analyze processor performance'), ('Memory and I/O', 'memory hierarchy; interrupts; input-output interfaces', 'Explain hardware data movement')],
    'Artificial Intelligence': [('Search', 'state spaces; heuristic search; A-star', 'Formulate a search problem'), ('Reasoning', 'propositional logic; knowledge representation; probabilistic reasoning', 'Represent and infer knowledge'), ('Intelligent systems', 'planning; neural networks; responsible AI', 'Compare intelligent-system approaches')],
    'Cloud Computing': [('Cloud foundations', 'service models; deployment models; virtualization', 'Choose a cloud service model'), ('Distributed applications', 'containers; distributed storage; scalability', 'Design a scalable application'), ('Operations', 'monitoring; cloud security; cost management', 'Evaluate cloud operating tradeoffs')],
    'Circuit Theory': [('DC networks', 'Kirchhoff laws; network theorems', 'Solve circuit networks'), ('AC networks', 'phasors; impedance; resonance', 'Analyze sinusoidal circuits'), ('Transient response', 'RC circuits; RL circuits; Laplace transforms', 'Predict transient circuit behavior')],
    'Electronic Devices': [('Semiconductors', 'PN junctions; diodes; rectifiers', 'Explain semiconductor device behavior'), ('Transistors', 'BJT; MOSFET; biasing', 'Analyze transistor operating points'), ('Applications', 'amplifiers; switching; device characteristics', 'Select devices for basic circuits')],
    'Digital Electronics': [('Logic', 'Boolean algebra; logic gates; minimization', 'Simplify logic expressions'), ('Combinational circuits', 'multiplexers; decoders; adders', 'Design combinational logic'), ('Sequential circuits', 'flip-flops; counters; registers; finite state machines', 'Design sequential systems')],
    'Analog Electronics': [('Amplifiers', 'small-signal models; transistor amplifiers; frequency response', 'Analyze amplifier performance'), ('Feedback', 'negative feedback; oscillators; stability', 'Explain feedback effects'), ('Operational amplifiers', 'op-amps; active filters; comparators', 'Design op-amp applications')],
    'Signals and Systems': [('Signals', 'continuous-time signals; discrete-time signals; system properties', 'Classify signals and systems'), ('Transforms', 'Fourier transforms; Laplace transforms; Z transforms', 'Analyze signals in transform domains'), ('System analysis', 'convolution; sampling; frequency response', 'Predict linear-system responses')],
    'Microprocessors': [('Architecture', 'registers; instruction sets; addressing modes', 'Explain processor organization'), ('Programming', 'assembly language; interrupts; subroutines', 'Write simple assembly programs'), ('Interfacing', 'memory interfacing; timers; peripheral interfaces', 'Connect processor peripherals')],
    'Embedded Systems': [('Embedded design', 'microcontrollers; sensors; actuators', 'Specify an embedded application'), ('Real-time software', 'real-time scheduling; interrupts; embedded C', 'Implement timing-aware software'), ('Connectivity', 'UART; SPI; I2C; testing', 'Integrate and test connected devices')],
    'VLSI Design': [('CMOS', 'MOS transistors; CMOS logic; inverter characteristics', 'Analyze CMOS gates'), ('Digital design', 'HDL; simulation; synthesis', 'Describe and verify digital hardware'), ('Physical design', 'layout; timing; power; verification', 'Evaluate chip design constraints')],
    'Calculus': [('Differentiation', 'limits; derivatives; optimization', 'Solve derivative-based problems'), ('Integration', 'definite integrals; techniques of integration', 'Evaluate integrals'), ('Multivariable calculus', 'partial derivatives; gradients; multiple integrals', 'Analyze multivariable functions')],
    'Linear Algebra': [('Linear systems', 'matrices; row reduction; rank', 'Solve linear systems'), ('Vector spaces', 'basis; dimension; linear transformations', 'Describe vector-space structure'), ('Spectral methods', 'eigenvalues; eigenvectors; orthogonality; singular value decomposition', 'Apply matrix decompositions')],
    'Differential Equations': [('First-order equations', 'separable equations; linear equations; initial values', 'Solve first-order models'), ('Higher-order equations', 'linear ODEs; systems of equations', 'Solve higher-order differential equations'), ('Applications', 'boundary values; partial differential equations; modelling', 'Translate physical models into equations')],
    'Probability Theory': [('Probability foundations', 'conditional probability; Bayes theorem; independence', 'Calculate conditional probabilities'), ('Random variables', 'distributions; expectation; variance', 'Model random quantities'), ('Limit results', 'law of large numbers; central limit theorem; estimation', 'Reason about sampling uncertainty')],
    'Abstract Algebra': [('Groups', 'subgroups; cyclic groups; permutations', 'Analyze group structure'), ('Homomorphisms', 'cosets; quotient groups; isomorphisms', 'Use structure-preserving maps'), ('Rings and fields', 'ideals; polynomial rings; fields', 'Reason about algebraic structures')],
    'Numerical Methods': [('Numerical error', 'rounding; root finding; convergence', 'Estimate numerical accuracy'), ('Linear and nonlinear problems', 'numerical linear algebra; interpolation; approximation', 'Implement numerical solution methods'), ('Numerical calculus', 'quadrature; numerical differentiation; numerical ODEs', 'Approximate calculus problems')],
    'Real Analysis': [('Real numbers and sequences', 'completeness; sequences; convergence', 'Construct convergence arguments'), ('Functions', 'limits; continuity; differentiation', 'Prove properties of real functions'), ('Integration and series', 'Riemann integration; function sequences; uniform convergence', 'Analyze integration and convergence')],
    'Mathematical Modelling': [('Model formulation', 'assumptions; dimensional analysis; parameter estimation', 'Formulate a mathematical model'), ('Dynamic models', 'difference equations; differential models; stability', 'Analyze model dynamics'), ('Decision models', 'optimization; simulation; model validation', 'Evaluate model predictions')],
    'Principles of Management': [('Management foundations', 'planning; organizing; management theories', 'Explain management functions'), ('People and execution', 'leadership; motivation; coordination', 'Compare leadership approaches'), ('Control', 'decision making; performance control; ethics', 'Evaluate management decisions')],
    'Financial Accounting': [('Accounting foundations', 'double-entry bookkeeping; journal; ledger', 'Record business transactions'), ('Statements', 'trial balance; income statement; balance sheet', 'Prepare financial statements'), ('Interpretation', 'cash flow; depreciation; financial ratios', 'Interpret financial performance')],
    'Marketing Management': [('Market understanding', 'consumer behavior; market research; segmentation', 'Identify target segments'), ('Marketing mix', 'product; pricing; distribution; promotion', 'Develop a marketing mix'), ('Strategy', 'branding; digital marketing; marketing metrics', 'Evaluate a marketing strategy')],
    'Organizational Behaviour': [('Individuals', 'perception; personality; motivation', 'Explain individual workplace behavior'), ('Groups', 'teams; communication; conflict', 'Analyze team dynamics'), ('Organizations', 'culture; organizational change; leadership', 'Assess organizational change')],
    'Business Economics': [('Demand and supply', 'elasticity; consumer choice; demand forecasting', 'Analyze market demand'), ('Firm decisions', 'production; costs; market structures', 'Explain pricing and output decisions'), ('Business environment', 'inflation; monetary policy; business cycles', 'Assess macroeconomic business impacts')],
    'Human Resource Management': [('Staffing', 'job analysis; recruitment; selection', 'Design a staffing process'), ('Development', 'training; performance appraisal; career development', 'Evaluate employee development'), ('Employment relations', 'compensation; employee relations; HR ethics', 'Assess HR policies')],
    'Strategic Management': [('Strategic analysis', 'industry analysis; competitive advantage; SWOT', 'Analyze strategic position'), ('Strategy design', 'business strategy; corporate strategy; growth', 'Compare strategic alternatives'), ('Implementation', 'organizational alignment; strategy execution; control', 'Evaluate strategy implementation')],
    'Corporate Finance': [('Financial decisions', 'time value of money; risk and return', 'Value financial cash flows'), ('Investment', 'capital budgeting; cost of capital; valuation', 'Evaluate investment projects'), ('Financing', 'capital structure; working capital; dividends', 'Assess financing choices')],
    'Information Systems Fundamentals': [('Information systems', 'business processes; systems components; data', 'Explain information-system roles'), ('System design', 'requirements; systems lifecycle; databases', 'Analyze business system requirements'), ('Governance', 'IT governance; security; digital transformation', 'Assess information-system risks')],
    'Web Programming': [('Web foundations', 'HTML; CSS; accessibility', 'Build accessible web pages'), ('Client programming', 'JavaScript; DOM; events; forms', 'Implement browser interactions'), ('Web applications', 'HTTP; APIs; server-side programming; authentication', 'Build a client-server application')],
    'Computer Networks': [('Network layers', 'OSI model; TCP/IP; Ethernet', 'Explain layered networking'), ('Internetworking', 'IP addressing; routing; switching', 'Analyze network connectivity'), ('Transport and applications', 'TCP; UDP; DNS; HTTP', 'Trace application network traffic')],
    'Software Engineering': [('Requirements', 'requirements analysis; use cases; specifications', 'Specify software requirements'), ('Design and delivery', 'software architecture; design patterns; agile development', 'Plan maintainable software'), ('Quality', 'testing; version control; maintenance', 'Evaluate software quality')],
    'Information Security': [('Security foundations', 'threat modelling; confidentiality; integrity; availability', 'Assess security risks'), ('Controls', 'cryptography; authentication; access control', 'Choose protective controls'), ('Security operations', 'incident response; vulnerability management; security policy', 'Plan security responses')],
    'Systems Administration': [('System management', 'operating system installation; users; permissions', 'Administer a multiuser system'), ('Services', 'network services; automation; shell scripting', 'Automate system operations'), ('Reliability', 'backups; monitoring; troubleshooting', 'Maintain service reliability')],
    'Network Security': [('Network threats', 'traffic analysis; network attacks; threat modelling', 'Identify network threats'), ('Protection', 'firewalls; VPNs; intrusion detection', 'Design network defenses'), ('Operations', 'security monitoring; incident handling; secure protocols', 'Investigate network incidents')],
    'Enterprise Application Development': [('Architecture', 'layered architecture; services; APIs', 'Design enterprise services'), ('Persistence', 'data access; transactions; integration', 'Implement reliable data integration'), ('Delivery', 'deployment; automated testing; scalability', 'Deliver maintainable applications')],
    'Engineering Mechanics': [('Statics', 'forces; equilibrium; free-body diagrams', 'Solve equilibrium problems'), ('Structures', 'trusses; friction; centroids', 'Analyze structural force systems'), ('Dynamics', 'kinematics; work and energy; momentum', 'Analyze moving bodies')],
    'Engineering Drawing': [('Drawing conventions', 'scales; dimensions; geometric construction', 'Produce standard technical drawings'), ('Projection', 'orthographic projection; sections; isometric views', 'Interpret engineering views'), ('Computer-aided drawing', 'CAD; assembly drawings; tolerances', 'Create dimensioned CAD drawings')],
    'Thermodynamics': [('Energy', 'properties; first law; thermodynamic systems', 'Apply energy balances'), ('Entropy', 'second law; entropy; exergy', 'Assess thermodynamic efficiency'), ('Cycles', 'power cycles; refrigeration; gas mixtures', 'Analyze thermal cycles')],
    'Fluid Mechanics': [('Fluid statics', 'pressure; buoyancy; fluid properties', 'Calculate static fluid forces'), ('Fluid motion', 'continuity; Bernoulli equation; momentum equation', 'Analyze fluid flows'), ('Flow systems', 'pipe flow; boundary layers; dimensional analysis', 'Estimate flow losses')],
    'Manufacturing Processes': [('Primary processes', 'casting; forming; welding', 'Compare manufacturing methods'), ('Machining', 'cutting mechanics; machine tools; CNC', 'Select machining processes'), ('Quality and automation', 'metrology; process planning; manufacturing automation', 'Plan a quality-controlled process')],
    'Strength of Materials': [('Stress and strain', 'elasticity; axial loading; thermal stress', 'Calculate material deformation'), ('Beams and shafts', 'bending; shear; torsion', 'Analyze loaded structural members'), ('Failure', 'buckling; combined stress; failure theories', 'Assess component safety')],
    'Heat Transfer': [('Conduction', 'steady conduction; transient conduction; fins', 'Calculate conductive heat flow'), ('Convection', 'forced convection; natural convection; correlations', 'Estimate convection coefficients'), ('Thermal systems', 'radiation; heat exchangers; thermal design', 'Analyze thermal equipment')],
    'Machine Design': [('Design foundations', 'design factors; material selection; fatigue', 'Select safe design parameters'), ('Machine elements', 'shafts; bearings; gears; fasteners', 'Size machine elements'), ('Assemblies', 'springs; brakes; couplings; design validation', 'Evaluate mechanical assemblies')],
    'Engineering Geology': [('Earth materials', 'minerals; rocks; weathering', 'Identify engineering rock properties'), ('Geological structures', 'faults; folds; earthquakes', 'Assess geological hazards'), ('Site assessment', 'groundwater; geological mapping; site investigation', 'Interpret site geology')],
    'Construction Materials': [('Material properties', 'cement; aggregates; concrete', 'Evaluate construction materials'), ('Material selection', 'steel; timber; masonry', 'Select materials for applications'), ('Testing and durability', 'material testing; durability; sustainable materials', 'Assess material quality')],
    'Surveying': [('Measurement', 'distance measurement; leveling; errors', 'Perform survey measurements'), ('Survey methods', 'traversing; theodolites; total stations', 'Plan a site survey'), ('Mapping', 'contours; GPS; setting out', 'Interpret and produce survey maps')],
    'Structural Analysis': [('Structural systems', 'loads; supports; determinate structures', 'Analyze structural equilibrium'), ('Deformation', 'deflection; energy methods; influence lines', 'Calculate structural response'), ('Indeterminate structures', 'moment distribution; stiffness method; matrix analysis', 'Analyze redundant structures')],
    'Geotechnical Engineering': [('Soil properties', 'soil classification; compaction; permeability', 'Characterize engineering soils'), ('Soil behavior', 'consolidation; shear strength; earth pressure', 'Analyze soil response'), ('Foundations', 'bearing capacity; settlement; slope stability', 'Assess foundation performance')],
    'Transportation Engineering': [('Transport planning', 'traffic studies; transport demand; road alignment', 'Analyze transportation requirements'), ('Highways', 'geometric design; pavement materials; pavement design', 'Design highway components'), ('Operations', 'traffic signals; road safety; maintenance', 'Evaluate transport operations')],
    'Reinforced Concrete Design': [('Design principles', 'limit state design; concrete behavior; reinforcement', 'Apply reinforced concrete design principles'), ('Members', 'beams; slabs; shear; bond', 'Design concrete members'), ('Systems', 'columns; footings; detailing', 'Detail concrete structural systems')],
    'Construction Project Management': [('Project planning', 'work breakdown; scheduling; critical path', 'Develop a construction schedule'), ('Project control', 'estimation; budgeting; resource allocation', 'Monitor project costs and resources'), ('Delivery', 'contracts; quality; safety; risk management', 'Manage construction delivery risks')],
}
SYLLABUS_ALIASES = {'Machine Learning': ['ML'], 'Artificial Intelligence': ['AI'],
    'Database Systems': ['DBMS', 'databases'], 'Operating Systems': ['OS'],
    'Data Structures': ['DSA'], 'Web Programming': ['web development'],
    'Probability Theory': ['probability'], 'Linear Algebra': ['matrix algebra']}

# Role -> (aliases, curated course relevance). Topics are selected from the outline,
# and actual prerequisite edges determine learning order at read time.
CAREER_GUIDANCE = {
    'AI Developer': (['AI engineer', 'artificial intelligence developer'], {'Artificial Intelligence': 'Build search and reasoning systems', 'Machine Learning': 'Train and evaluate predictive models', 'Data Structures': 'Implement efficient algorithms', 'Programming Fundamentals': 'Build reliable programs', 'Linear Algebra': 'Understand vector and matrix computations', 'Probability Theory': 'Reason about uncertainty', 'Embedded Systems': 'Deploy intelligence in connected devices'}),
    'Data Scientist': (['data science', 'data analyst'], {'Machine Learning': 'Build and evaluate predictive models', 'Database Systems': 'Prepare and query analytical data', 'Probability Theory': 'Quantify uncertainty', 'Linear Algebra': 'Understand feature representations', 'Numerical Methods': 'Implement reliable numerical analysis', 'Mathematical Modelling': 'Turn domain questions into models', 'Business Economics': 'Interpret business demand and indicators', 'Signals and Systems': 'Analyze sampled and transformed data'}),
    'Full Stack Developer': (['full stack', 'full-stack developer', 'web developer'], {'Web Programming': 'Build browser interfaces and web services', 'Enterprise Application Development': 'Integrate application services and persistence', 'Database Systems': 'Design application data storage', 'Software Engineering': 'Develop and test maintainable applications', 'Computer Networks': 'Understand HTTP and network communication', 'Information Security': 'Protect application access', 'Programming Fundamentals': 'Develop programming foundations', 'Data Structures': 'Select efficient application structures', 'Cloud Computing': 'Deploy and operate applications'}),
    'Embedded Systems Engineer': (['embedded developer', 'firmware engineer'], {'Embedded Systems': 'Integrate hardware and real-time software', 'Microprocessors': 'Program and interface processors', 'Digital Electronics': 'Understand digital hardware', 'Electronic Devices': 'Select circuit components', 'Circuit Theory': 'Analyze connected circuits'}),
    'VLSI Engineer': (['chip designer'], {'VLSI Design': 'Design and verify digital chips', 'Digital Electronics': 'Design sequential and combinational logic', 'Electronic Devices': 'Understand transistor behavior', 'Analog Electronics': 'Analyze analog interfaces'}),
    'Quantitative Analyst': (['quant analyst', 'mathematical modeller'], {'Probability Theory': 'Model uncertain outcomes', 'Linear Algebra': 'Solve multivariable systems', 'Numerical Methods': 'Compute approximate solutions', 'Mathematical Modelling': 'Validate decision models', 'Calculus': 'Analyze change and optimization', 'Differential Equations': 'Model dynamic systems'}),
    'Business Analyst': (['business analytics', 'management analyst'], {'Business Economics': 'Understand business drivers', 'Financial Accounting': 'Interpret financial statements', 'Marketing Management': 'Analyze customers and markets', 'Strategic Management': 'Evaluate strategic alternatives', 'Principles of Management': 'Understand organizational decisions'}),
    'Finance Analyst': (['financial analyst'], {'Corporate Finance': 'Evaluate investment and financing decisions', 'Financial Accounting': 'Interpret financial performance', 'Business Economics': 'Assess economic conditions'}),
    'Cybersecurity Analyst': (['security analyst', 'cyber security'], {'Information Security': 'Assess and mitigate threats', 'Network Security': 'Monitor and defend networks', 'Computer Networks': 'Understand network behavior', 'Systems Administration': 'Secure and maintain systems'}),
    'Mechanical Design Engineer': (['mechanical designer'], {'Machine Design': 'Design safe machine components', 'Strength of Materials': 'Calculate stresses and failure risks', 'Engineering Drawing': 'Communicate component geometry', 'Engineering Mechanics': 'Analyze forces and motion', 'Manufacturing Processes': 'Choose feasible manufacturing methods'}),
    'Thermal Engineer': (['energy engineer'], {'Thermodynamics': 'Analyze energy conversion', 'Heat Transfer': 'Design thermal equipment', 'Fluid Mechanics': 'Analyze thermal-system flows'}),
    'Civil Structural Engineer': (['structural engineer', 'civil engineer'], {'Structural Analysis': 'Calculate load response', 'Reinforced Concrete Design': 'Design concrete structural members', 'Construction Materials': 'Select durable materials', 'Geotechnical Engineering': 'Assess foundations', 'Surveying': 'Understand site geometry'}),
    'Construction Project Manager': (['construction manager'], {'Construction Project Management': 'Plan costs, schedules and delivery', 'Construction Materials': 'Assess material choices', 'Surveying': 'Understand site layout', 'Transportation Engineering': 'Understand infrastructure delivery'}),
}

def seed_academic_guidance(conn):
    import json
    for course_id, name in conn.execute('SELECT course_id,course_name FROM courses').fetchall():
        if name not in SYLLABUS_OUTLINES:
            continue
        conn.execute('INSERT OR IGNORE INTO course_syllabi VALUES (?,?,?,?)',
            (course_id, json.dumps(SYLLABUS_ALIASES.get(name, [])), json.dumps([
                {'unit': i, 'title': title, 'topics': topics.split('; '), 'learning_outcome': outcome}
                for i, (title, topics, outcome) in enumerate(SYLLABUS_OUTLINES[name], 1)]),
             'Maintained demonstration teaching outline; not an officially verified syllabus'))
    for role, (aliases, relevance) in CAREER_GUIDANCE.items():
        conn.execute('INSERT OR IGNORE INTO career_roles VALUES (?,?,?)', (role, json.dumps(aliases), 'Curated advisory curriculum relevance; no placement requirement'))
        for priority, (name, reason) in enumerate(relevance.items(), 1):
            for (course_id,) in conn.execute('SELECT course_id FROM courses WHERE course_name=?', (name,)).fetchall():
                conn.execute('INSERT OR IGNORE INTO career_course_relevance VALUES (?,?,?,?)', (role, course_id, priority, reason))


def seed_accounts(conn, token_version=0):
    password_hash = generate_password_hash('password123')
    for fid, name, department, email in FACULTY:
        conn.execute('INSERT INTO users(user_id,username,password_hash,role,email,token_version) VALUES(?,?,?,?,?,?)', (fid,name,password_hash,'faculty',email,token_version))
        conn.execute('INSERT INTO faculty VALUES(?,?,?,?,?,?)', (fid,name,department,'Professor',email,'Mon-Fri 14:00-16:00'))
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for sid, name, department, semester, advisor, email in STUDENTS:
        conn.execute('INSERT INTO users(user_id,username,password_hash,role,email,token_version) VALUES(?,?,?,?,?,?)', (sid,name,password_hash,'student',email,token_version))
        conn.execute('INSERT INTO students VALUES(?,?,?,?,?)', (sid,name,department,semester,advisor))
        conn.execute('INSERT INTO leave_requests(student_id,from_date,to_date,reason,status) VALUES(?,?,?,?,?)', (sid,tomorrow,tomorrow,'Sample personal leave request','pending'))


def seed_courses(conn):
    students = conn.execute('SELECT student_id,department,advisor_faculty_id FROM students ORDER BY student_id').fetchall()
    owners = {}
    for department in CURRICULA:
        faculty = conn.execute('SELECT faculty_id FROM faculty WHERE department=?', (department,)).fetchall()
        if len(faculty) != 1:
            raise ValueError(f'{department} must have exactly one assigned faculty member.')
        owners[department] = faculty[0][0]
    for sid, department, advisor in students:
        if department not in owners or advisor != owners[department]:
            raise ValueError(f'{sid} has an unknown branch or mismatched assigned faculty.')
    for dept, (prefix, subjects) in CURRICULA.items():
        for suffix, name in zip(SUFFIXES, COMMON + subjects):
            cid = prefix + suffix
            credits = 2 if suffix in {'001','002'} else 4
            conn.execute('INSERT INTO courses VALUES(?,?,?,?,?)', (cid,name,dept,credits,owners[dept]))
            conn.execute("INSERT INTO demo_records VALUES('courses',?)", (cid,))
        for course, prerequisite in [('201','101'),('301','201'),('302','101'),('303','201'),('304','101'),('401','301'),('402','302'),('402','304')]:
            conn.execute('INSERT INTO prerequisites VALUES(?,?)', (prefix+course,prefix+prerequisite))
        # Current-course schedules only: no historical/future course is counted as active.
        for slot, suffix in enumerate(SUFFIXES[4:8]):
            for weekday in [slot, (slot+3) % 5]:
                conn.execute('INSERT INTO course_timetable(course_id,weekday,start_time,end_time,effective_from,effective_to,is_demo) VALUES(?,?,?,?,?,?,1)',
                             (prefix+suffix,weekday,f'{9+slot:02d}:00',f'{10+slot:02d}:00',f'{date.today().year}-01-01',f'{date.today().year}-12-31'))
    for student_index, (sid,dept,_) in enumerate(students):
        prefix = CURRICULA[dept][0]
        for index, (suffix,status) in enumerate(zip(SUFFIXES,STATUSES)):
            cid = prefix + suffix
            conn.execute('INSERT INTO course_registrations(student_id,course_id,status) VALUES(?,?,?)', (sid,cid,status))
            if status == 'completed':
                marks = 78 + (student_index+index) % 16
                conn.execute('INSERT INTO results(student_id,course_id,grade,marks) VALUES(?,?,?,?)', (sid,cid,'A' if marks>=85 else 'B+',marks))
                conn.execute('INSERT INTO attendance(student_id,course_id,total_classes,attended_classes) VALUES(?,?,60,?)', (sid,cid,54+(student_index+index)%5))
            elif status == 'enrolled':
                conn.execute('INSERT INTO attendance(student_id,course_id,total_classes,attended_classes) VALUES(?,?,40,?)', (sid,cid,33+(student_index+index)%7))
    if conn.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Invalid foreign keys; course replacement rolled back.')
    invalid = conn.execute("""SELECT cr.student_id FROM course_registrations cr JOIN prerequisites p ON p.course_id=cr.course_id
        WHERE cr.status IN ('completed','enrolled') AND NOT EXISTS (
            SELECT 1 FROM course_registrations pre WHERE pre.student_id=cr.student_id
            AND pre.course_id=p.prerequisite_course_id AND pre.status='completed')""").fetchall()
    if invalid:
        raise RuntimeError('Unmet prerequisites for completed/enrolled courses; replacement rolled back.')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_syllabi'").fetchone():
        seed_academic_guidance(conn)

def init_db(db_path: Path = DB_PATH) -> None:
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally resetting an existing installation.
    with db_path.open('xb'):
        pass
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    migrate(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        seed_accounts(conn)
        seed_courses(conn)
        from tools.attendance import migrate_attendance
        from db.migrate import migrate_alerts
        migrate_attendance(conn)
        migrate_alerts(conn)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DB_PATH)
    args = parser.parse_args()
    init_db(args.database)
    print(f'Initialized current branch demo database at {args.database}')
