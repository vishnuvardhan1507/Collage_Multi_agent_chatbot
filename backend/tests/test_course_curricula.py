from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from db.init_db import init_db
from db.replace_demo_accounts import replace_accounts
from db.assign_branch_courses import assign_courses, CURRICULA, COMMON
from agents.response_formatter import format_sql_response


class CurriculumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.database=Path(cls.temp.name)/'courses.db'
        init_db(cls.database)
        replace_accounts(cls.database,Path(cls.temp.name)/'memory')
        with closing(sqlite3.connect(cls.database)) as conn:
            cls.users=conn.execute('SELECT * FROM users ORDER BY user_id').fetchall()
            cls.leaves=conn.execute('SELECT * FROM leave_requests ORDER BY leave_id').fetchall()
        cls.report=assign_courses(cls.database)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_ten_courses_and_status_split_for_every_student(self):
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM courses').fetchone()[0],70)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM course_registrations').fetchone()[0],140)
            counts=conn.execute('SELECT student_id,status,COUNT(*) FROM course_registrations GROUP BY student_id,status').fetchall()
            self.assertEqual(len(counts),42)
            for _,status,count in counts:
                self.assertEqual(count,{'completed':4,'enrolled':4,'pending':2}[status])
            self.assertEqual(conn.execute('SELECT * FROM users ORDER BY user_id').fetchall(),self.users)
            self.assertEqual(conn.execute('SELECT * FROM leave_requests ORDER BY leave_id').fetchall(),self.leaves)
        self.assertTrue(Path(self.report['backup']).is_file())

    def test_branch_subjects_and_teaching_assignments(self):
        with closing(sqlite3.connect(self.database)) as conn:
            names={dept:{r[0] for r in conn.execute('SELECT course_name FROM courses WHERE department=?',(dept,))} for dept in CURRICULA}
            for a in names:
                for b in names:
                    if a!=b:
                        self.assertEqual(names[a]&names[b],set(COMMON))
            self.assertEqual(conn.execute('''SELECT cr.student_id FROM course_registrations cr
                JOIN students s ON s.student_id=cr.student_id JOIN courses c ON c.course_id=cr.course_id
                JOIN faculty f ON f.faculty_id=c.faculty_id
                WHERE s.department!=c.department OR s.advisor_faculty_id!=c.faculty_id OR f.department!=s.department''').fetchall(),[])

    def test_prerequisites_are_foreign_keys_and_statuses_are_consistent(self):
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual({r[3] for r in conn.execute('PRAGMA foreign_key_list(prerequisites)')},{'course_id','prerequisite_course_id'})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM prerequisites').fetchone()[0],56)
            unmet=conn.execute('''SELECT cr.status FROM course_registrations cr JOIN prerequisites p ON p.course_id=cr.course_id
                WHERE NOT EXISTS (SELECT 1 FROM course_registrations pre WHERE pre.student_id=cr.student_id
                AND pre.course_id=p.prerequisite_course_id AND pre.status='completed')''').fetchall()
            self.assertEqual({r[0] for r in unmet},{'pending'})
            self.assertEqual(conn.execute('''SELECT r.result_id FROM results r JOIN course_registrations cr
                ON cr.student_id=r.student_id AND cr.course_id=r.course_id WHERE cr.status!='completed' ''').fetchall(),[])
            self.assertEqual(conn.execute('''SELECT a.attendance_id FROM attendance a JOIN course_registrations cr
                ON cr.student_id=a.student_id AND cr.course_id=a.course_id WHERE cr.status='pending' ''').fetchall(),[])
            self.assertEqual(conn.execute('''SELECT faculty_id,weekday,start_time,COUNT(*) FROM course_timetable t
                JOIN courses c ON c.course_id=t.course_id GROUP BY faculty_id,weekday,start_time HAVING COUNT(*)>1''').fetchall(),[])

    def test_course_statuses_have_readable_group_labels(self):
        result=format_sql_response([{'course_id':str(i),'course_name':'Course '+str(i),'status':s} for i,s in enumerate(['completed','enrolled','pending'])])
        for label in ['Completed (1)','Currently enrolled (1)','To be completed (1)']:
            self.assertIn(label,result)
