"""Regression cases for queue supply checks; no live 12306 requests."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from py12306.order.order import Order


class FakeResponse:
    status_code = 200

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.last_post_data = None

    def post(self, *args, **kwargs):
        self.last_post_data = args[1] if len(args) > 1 else kwargs.get('data')
        return self.response


class QueueSupplyTests(unittest.TestCase):
    def make_order(self, response, member_count=1):
        instance = object.__new__(Order)
        instance.failure_reason = ''
        instance.session = FakeSession(response)
        instance.query_ins = SimpleNamespace(
            left_date='2026-10-23', current_order_seat='O',
            current_seat=30, member_num_take=member_count,
            ticket_info=['secret', '', 'TEST', 'D1036'])
        instance.user_ins = SimpleNamespace(
            global_repeat_submit_token='test-only',
            ticket_info_for_passenger_form={
                'queryLeftTicketRequestDTO': {
                    'train_no': 'TEST', 'station_train_code': 'D1036',
                    'from_station': 'AAA', 'to_station': 'BBB',
                },
                'leftTicketStr': 'test-only',
                'purpose_codes': '00',
                'train_location': 'TEST',
            })
        return instance

    def check(self, payload, expected, count=1):
        order = self.make_order(FakeResponse(payload), member_count=count)
        with patch('py12306.order.order.OrderLog.add_quick_log') as mocked:
            mocked.return_value.flush.return_value = None
            self.assertEqual(order.get_queue_count(), expected)
        if not expected:
            self.assertTrue(order.failure_reason)

    def test_queue_valid_number(self):
        self.check({'status': True, 'data': {
            'ticket': '8,12', 'countT': '1', 'op_2': 'false'}}, True)

    def test_queue_request_date_and_train_values(self):
        order = self.make_order(FakeResponse({'status': True, 'data': {
            'ticket': '8,12', 'countT': '0', 'op_2': 'false'}}))
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertTrue(order.get_queue_count())
        posted = order.session.last_post_data
        self.assertEqual(posted['train_date'],
                         'Fri Oct 23 2026 00:00:00 GMT+0800 (中国标准时间)')
        self.assertEqual(posted['seatType'], 'O')
        self.assertEqual(posted['stationTrainCode'], 'D1036')
        self.assertEqual(posted['train_no'], 'TEST')

    def test_queue_request_rejects_wrong_train(self):
        order = self.make_order(FakeResponse({'status': True, 'data': {
            'ticket': '8,12'}}))
        order.user_ins.ticket_info_for_passenger_form['queryLeftTicketRequestDTO']['train_no'] = 'WRONG'
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIsNone(order.session.last_post_data)
        self.assertIn('车次与余票查询结果不一致', order.failure_reason)

    def test_queue_valid_abundant(self):
        self.check({'status': True, 'data': {
            'ticket': '充足', 'countT': '0', 'op_2': False}}, True)

    def test_queue_no_seats(self):
        self.check({'status': True, 'data': {'ticket': '0,12'}}, False)

    def test_queue_missing_ticket(self):
        self.check({'status': True, 'data': {'countT': 0}}, False)

    def test_queue_missing_ticket_diagnostic_is_redacted(self):
        order = self.make_order(FakeResponse({'status': True, 'data': {
            'countT': '1', 'op_2': 'false',
            'REPEAT_SUBMIT_TOKEN': 'SECRET_DO_NOT_LOG',
            'passenger': 'PRIVATE_DO_NOT_LOG'
        }}))
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIn('countT', order.failure_reason)
        self.assertIn('op_2', order.failure_reason)
        self.assertNotIn('SECRET_DO_NOT_LOG', order.failure_reason)
        self.assertNotIn('PRIVATE_DO_NOT_LOG', order.failure_reason)

    def test_queue_numeric_ticket(self):
        self.check({'status': True, 'data': {'ticket': 8, 'countT': 0}}, True)

    def test_ticket_null_with_present_key_reports_null(self):
        order = self.make_order(FakeResponse({'status': True, 'data': {
            'ticket': None, 'count': '7', 'countT': '0',
            'op_1': 'false', 'op_2': 'false',
        }}))
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIn('ticket 字段不可解析：null', order.failure_reason)
        self.assertIn('countT 字段存在', order.failure_reason)
        self.assertIn('op_2=false', order.failure_reason)

    def test_empty_and_whitespace_ticket_are_distinguished(self):
        for ticket, expected in [('', '空字符串'), ('   ', '仅空白字符')]:
            with self.subTest(ticket=ticket):
                order = self.make_order(FakeResponse({
                    'status': True, 'data': {'ticket': ticket, 'op_1': False}
                }))
                with patch('py12306.order.order.OrderLog.add_quick_log'):
                    self.assertFalse(order.get_queue_count())
                self.assertIn(expected, order.failure_reason)

    def test_ticket_wrong_type_is_safely_reported(self):
        for ticket, expected in [(True, '布尔值'), ([], '数组'), ({}, '对象')]:
            with self.subTest(ticket_type=expected):
                order = self.make_order(FakeResponse({
                    'status': True, 'data': {'ticket': ticket}
                }))
                with patch('py12306.order.order.OrderLog.add_quick_log'):
                    self.assertFalse(order.get_queue_count())
                self.assertIn(expected, order.failure_reason)

    def test_malformed_ticket_not_logged_even_with_private_value(self):
        sensitive = 'PRIVATE_DO_NOT_LOG'
        order = self.make_order(FakeResponse({
            'status': True,
            'data': {'ticket': {'passenger': sensitive}, 'op_1': sensitive}
        }))
        with patch('py12306.order.order.OrderLog.add_quick_log') as mocked:
            self.assertFalse(order.get_queue_count())
        self.assertNotIn(sensitive, order.failure_reason)
        log_lines = ' '.join(str(call) for call in mocked.call_args_list)
        self.assertNotIn(sensitive, log_lines)


    def test_queue_full_queue_without_ticket(self):
        order = self.make_order(FakeResponse({'status': True, 'data': {'op_2': 'true'}}))
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIn('排队人数超过余票数量', order.failure_reason)

    def test_queue_invalid_ticket(self):
        self.check({'status': True, 'data': {'ticket': '*'}}, False)

    def test_queue_insufficient_party(self):
        self.check({'status': True, 'data': {'ticket': '1'}}, False, count=2)

    def test_queue_too_many_waiting(self):
        self.check({'status': True, 'data': {
            'ticket': '8', 'op_2': 'true'}}, False)

    def test_queue_rejected(self):
        self.check({'status': False, 'messages': ['invalid']}, False)

    def test_queue_bad_http_status(self):
        order = self.make_order(FakeResponse({}, status=503))
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIn('503', order.failure_reason)

    def test_queue_missing_form_key(self):
        order = self.make_order(FakeResponse({}))
        del order.user_ins.ticket_info_for_passenger_form['leftTicketStr']
        with patch('py12306.order.order.OrderLog.add_quick_log'):
            self.assertFalse(order.get_queue_count())
        self.assertIn('必需字段', order.failure_reason)


if __name__ == '__main__':
    unittest.main()
