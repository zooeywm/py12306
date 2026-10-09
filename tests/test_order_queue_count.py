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

    def post(self, *args, **kwargs):
        return self.response


class QueueSupplyTests(unittest.TestCase):
    def make_order(self, response, member_count=1):
        instance = object.__new__(Order)
        instance.failure_reason = ''
        instance.session = FakeSession(response)
        instance.query_ins = SimpleNamespace(
            left_date='2026-10-23', current_order_seat='O',
            current_seat=30, member_num_take=member_count)
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

    def test_queue_valid_abundant(self):
        self.check({'status': True, 'data': {
            'ticket': '充足', 'countT': '0', 'op_2': False}}, True)

    def test_queue_no_seats(self):
        self.check({'status': True, 'data': {'ticket': '0,12'}}, False)

    def test_queue_missing_ticket(self):
        self.check({'status': True, 'data': {'countT': 0}}, False)

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
