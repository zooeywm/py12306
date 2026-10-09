"""The 12306 queue date must not depend on host locale or timezone."""
import unittest

from py12306.helpers.queue_request import format_queue_train_date


class QueueDateTests(unittest.TestCase):
    def test_october_23_2026(self):
        self.assertEqual(format_queue_train_date('2026-10-23'),
                         'Fri Oct 23 2026 00:00:00 GMT+0800 (中国标准时间)')

    def test_january_1_2027(self):
        self.assertEqual(format_queue_train_date('2027-01-01'),
                         'Fri Jan 01 2027 00:00:00 GMT+0800 (中国标准时间)')

    def test_leap_day(self):
        self.assertEqual(format_queue_train_date('2028-02-29'),
                         'Tue Feb 29 2028 00:00:00 GMT+0800 (中国标准时间)')

    def test_february_2027(self):
        self.assertEqual(format_queue_train_date('2027-02-05'),
                         'Fri Feb 05 2027 00:00:00 GMT+0800 (中国标准时间)')

    def test_invalid_date(self):
        for value in ('2026-02-30', '20261023', None, '2026-10-23T00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_queue_train_date(value)


if __name__ == '__main__':
    unittest.main()
