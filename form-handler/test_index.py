"""Offline tests for the form handler. Run: python3 form-handler/test_index.py"""

import json
import os
import sys
import unittest
from unittest import mock

os.environ.update(SMTP_USER='sender@yandex.ru', SMTP_PASSWORD='test-only', MAIL_TO='inbox@yandex.ru')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import index  # noqa: E402

ORIGIN = 'https://impression-team.github.io'


def event(payload=None, origin=ORIGIN, method='POST', raw=None):
    return {
        'httpMethod': method,
        'headers': {'Origin': origin, 'User-Agent': 'Mozilla/5.0 Test'},
        'body': raw if raw is not None else json.dumps(payload or {}),
        'isBase64Encoded': False,
        'requestContext': {'identity': {'sourceIp': '203.0.113.7'}},
    }


def valid(**overrides):
    data = {
        'name': 'Иван', 'email': 'ivan@example.com', 'phone': '@ivan',
        'project_type': 'Игра / Game', 'budget': '', 'message': 'Нужна игра',
        'consent': True, 'consent_at': '2026-10-01T18:00:00.000Z',
        'lang': 'ru', 'website': '', 'fill_ms': 15000,
    }
    data.update(overrides)
    return data


class HandlerTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(index, '_send')
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, ev):
        res = index.handler(ev, None)
        return res['statusCode'], json.loads(res['body']), res['headers']

    def test_valid_request_is_emailed(self):
        status, body, headers = self.call(event(valid()))
        self.assertEqual((status, body), (200, {'success': True}))
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
        msg = self.send.call_args[0][0]
        self.assertEqual(msg['To'], 'inbox@yandex.ru')
        self.assertIn('ivan@example.com', msg['Reply-To'])
        text = msg.get_content()
        for part in ('Иван', 'Нужна игра', 'ДАНО', '203.0.113.7', '2026-10-01T18:00:00.000Z', 'pd-consent.html'):
            self.assertIn(part, text)

    def test_without_consent_nothing_is_sent(self):
        for consent in (False, 'true', None):
            status, body, _ = self.call(event(valid(consent=consent)))
            self.assertEqual((status, body['error']), (400, 'consent'))
        self.send.assert_not_called()

    def test_foreign_origin_rejected(self):
        status, _, headers = self.call(event(valid(), origin='https://evil.example'))
        self.assertEqual(status, 403)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.send.assert_not_called()

    def test_bots_get_fake_success(self):
        for data in (valid(website='http://spam'), valid(fill_ms=500), valid(fill_ms=None)):
            status, body, _ = self.call(event(data))
            self.assertEqual((status, body), (200, {'success': True}))
        self.send.assert_not_called()

    def test_validation(self):
        cases = [
            valid(name=''), valid(message='  '), valid(email='not-an-email'),
            valid(email='a@b.co\nBcc: x@y.z'), valid(project_type='Hack'),
            valid(message='x' * 5001),
        ]
        for data in cases:
            status, _, _ = self.call(event(data))
            self.assertEqual(status, 400, data)
        self.send.assert_not_called()

    def test_bad_body_and_methods(self):
        self.assertEqual(self.call(event(raw='{oops'))[0], 400)
        self.assertEqual(self.call(event(raw='[1,2]'))[0], 400)
        self.assertEqual(self.call(event(raw='x' * 30000))[0], 413)
        self.assertEqual(self.call(event(method='GET'))[0], 405)
        status, _, headers = self.call(event(method='OPTIONS'))
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)

    def test_header_injection_in_name_is_flattened(self):
        self.call(event(valid(name='Ivan\r\nBcc: victim@example.com')))
        msg = self.send.call_args[0][0]
        self.assertNotIn('\n', msg['Reply-To'])
        self.assertIsNone(msg['Bcc'])

    def test_mail_failure_reported(self):
        self.send.side_effect = OSError('smtp down')
        status, body, _ = self.call(event(valid()))
        self.assertEqual((status, body['error']), (502, 'mail'))


if __name__ == '__main__':
    unittest.main()
