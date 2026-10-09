"""The 12306 queue date must not depend on host locale or timezone."""
import datetime
import unittest
from unittest.mock import patch

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

    def test_no_locale_dependent_strftime(self):
        with patch.object(datetime.date, 'fromisoformat', wraps=datetime.date.fromisoformat) as date_mock:
            result = format_queue_train_date('2026-10-23')
        self.assertEqual(result[:3], 'Fri')
        date_mock.assert_called_once_with('2026-10-23')

    def test_invalid_date(self):
        for value in ('2026-02-30', '20261023', None, '2026-10-23T00:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_queue_train_date(value)


if __name__ == '__main__':
    unittest.main()
