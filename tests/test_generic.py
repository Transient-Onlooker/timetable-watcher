import unittest
import tempfile
from pathlib import Path
from datetime import date
import timetable_watcher as w

class GenericTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 26)
        self.lesson = lambda p, sub, teach, change=False: {"period": p, "subject": sub, "teacher": teach, "site_changed": change, "original_subject": "Before" if change else None, "original_teacher": "Teacher" if change else None}

    def test_code_validation(self):
        self.assertEqual(w.validate_school_code('12345'), '12345')
        for value in ('abc', '123&x=1', '１２３'):
            with self.assertRaises(w.TimetableError): w.validate_school_code(value)

    def test_missing_code(self):
        with self.assertRaisesRegex(w.TimetableError, 'TIMETABLE_SCHOOL_CODE'):
            w.main(['inspect', '--school-code', '0'])

    def test_generic_name(self):
        t=w.format_message(self.day,1,1,[self.lesson(1,'Subject','Teacher')],None,'2026-10-24 12:00:00','morning', '12345')
        self.assertIn('학교 12345',t)
        self.assertIn('?sc=12345',t)

    def test_change_threshold(self):
        for count in (1,2,3,4,7):
            lessons=[self.lesson(i,'New', 'Teacher',True) for i in range(1,count+1)]
            text=w.format_message(self.day,1,1,lessons,None,'2026-10-24 12:00:00','morning','12345')
            self.assertEqual('→' in text,count<=3)
            self.assertIn('1교시 New · Teacher (변동됨)',text)

    def test_no_room_signal(self):
        before=[{**self.lesson(1,'Subject','Teacher'), 'room': '101'}]
        after=[{**self.lesson(1,'Subject','Teacher'), 'room': '102'}]
        self.assertEqual([],w.changed_between(before,after))

    def test_db_partitioning(self):
        with tempfile.TemporaryDirectory() as temp:
            with w.connect_db(Path(temp)/'s.sqlite3') as db:
                a=[self.lesson(1,'A','B')]
                w.capture(db,self.day,1,1,a,'t1','12345')
                self.assertIsNotNone(w.latest_snapshot(db,self.day,1,1,'12345'))
                self.assertIsNone(w.latest_snapshot(db,self.day,1,1,'67890'))

if __name__=='__main__': unittest.main()
