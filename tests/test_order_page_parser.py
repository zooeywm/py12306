"""Regression tests for 12306 initDc JavaScript form parsing."""
import unittest

from py12306.helpers.order_page_parser import parse_js_object_assignment


class ParseInitDcTests(unittest.TestCase):
    def parse(self, text, name='ticketInfoForPassengerForm'):
        return parse_js_object_assignment(text, name)

    def test_single_quotes_javascript_bools_and_null(self):
        page = r"""var ticketInfoForPassengerForm={'queryLeftTicketRequestDTO':
        {'train_no':'D1036','from_station':'YCB'},
        'leftTicketStr':'abc%2B123','purpose_codes':'00',
        'key_check_isChange':'SAFE_TEST','train_location':'P2',
        'nested':{'tickets':[{'reserved':false,'ready':true,'reason':null}]}};
        var orderRequestDTO={'adult_num':0,'passengerFlag':null};"""
        data = self.parse(page)
        self.assertEqual(data['queryLeftTicketRequestDTO']['train_no'], 'D1036')
        self.assertEqual(data['nested']['tickets'][0], {
            'reserved': False, 'ready': True, 'reason': None
        })
        self.assertEqual(self.parse(page, 'orderRequestDTO')['passengerFlag'], None)

    def test_quotes_and_braces_inside_strings(self):
        page = """var ticketInfoForPassengerForm = {
          "notice": "Traveller's ticket } is { ready",
          'quoted': 'it\\'s O\\'Reilly',
          'literal': 'true null false'
        };"""
        result = self.parse(page)
        self.assertEqual(result['notice'], "Traveller's ticket } is { ready")
        self.assertEqual(result['quoted'], "it's O'Reilly")
        self.assertEqual(result['literal'], 'true null false')

    def test_bare_js_keys_and_undefined(self):
        page = "const orderRequestDTO = {train_no:'D1036', enabled:true, alt:undefined, list:[1,2,3],};"
        result = self.parse(page, 'orderRequestDTO')
        self.assertEqual(result, {
            'train_no': 'D1036', 'enabled': True, 'alt': None,
            'list': [1, 2, 3]
        })

    def test_malicious_expression_rejected_without_executing(self):
        page = "var orderRequestDTO = {'attack': __import__('os').system('echo PWNED')};"
        with self.assertRaises(ValueError):
            self.parse(page, 'orderRequestDTO')

    def test_missing_and_invalid_object(self):
        for value in ("", "var ticketInfoForPassengerForm = []",
                      "var ticketInfoForPassengerForm = {'x': 1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(value)

    def test_whitelist_blocks_other_variable_names(self):
        with self.assertRaises(ValueError):
            self.parse("var passwords = {'password':'x'}", 'passwords')

    def test_nesting_limit(self):
        value = '{' * 66 + '}' * 66
        with self.assertRaises(ValueError):
            self.parse('var orderRequestDTO = ' + value, 'orderRequestDTO')

    def test_document_limit(self):
        with self.assertRaises(ValueError):
            self.parse(' ' * 4_000_001)


if __name__ == '__main__':
    unittest.main()
