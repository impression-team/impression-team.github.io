"""Offline tests for the TSPL shop function. Run: python3 tspl-shop/test_index.py

YooKassa and SMTP are mocked; nothing leaves the machine. A throwaway test
key pair is used — never the real private key.
"""

import base64
import json
import os
import sys
import unittest
from unittest import mock

TEST_SEED = bytes(range(32))
os.environ.update(
    YOOKASSA_SHOP_ID='123456', YOOKASSA_SECRET_KEY='test_only',
    TSPL_PRIVATE_KEY=base64.b64encode(TEST_SEED).decode(),
    SMTP_USER='sender@yandex.ru', SMTP_PASSWORD='test-only', MAIL_TO='owner@yandex.ru',
    PRICE_RUB='499',
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import ed25519  # noqa: E402
import index  # noqa: E402

ORIGIN = 'https://impression-team.github.io'
PID = '2e8a5a3b-000f-5000-9000-1a2b3c4d5e6f'


def event(payload, origin=ORIGIN, method='POST'):
    return {'httpMethod': method, 'headers': {'Origin': origin} if origin else {},
            'body': json.dumps(payload), 'isBase64Encoded': False}


def paid(**overrides):
    p = {'id': PID, 'status': 'succeeded', 'paid': True, 'test': False,
         'amount': {'value': '499.00', 'currency': 'RUB'},
         'created_at': '2026-10-02T20:00:00.000Z', 'captured_at': '2026-10-03T09:15:00.000Z',
         'metadata': {'product': 'tspl', 'email': 'ivan@example.com', 'offer_at': '2026-10-02T19:59:00Z'}}
    p.update(overrides)
    return p


def verify_key(key, public):
    """Mirror of License.verify in the app (CryptoKit) using the cryptography package."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    body = key[len('TSPL-'):]
    data_s, sig_s = body.split('.')
    pad = lambda s: s + '=' * (-len(s) % 4)  # noqa: E731
    data = base64.urlsafe_b64decode(pad(data_s))
    Ed25519PublicKey.from_public_bytes(public).verify(base64.urlsafe_b64decode(pad(sig_s)), data)
    return json.loads(data)


class ShopTest(unittest.TestCase):
    def setUp(self):
        self.api = mock.patch.object(index, '_yookassa').start()
        self.send = mock.patch.object(index, '_send').start()
        self.addCleanup(mock.patch.stopall)

    def call(self, ev):
        res = index.handler(ev, None)
        return res['statusCode'], json.loads(res['body']) if res['body'] else {}

    # ---- create
    def test_create_returns_payment_page(self):
        self.api.return_value = {'id': PID, 'confirmation': {'confirmation_url': 'https://yoomoney.ru/checkout/x'}}
        status, body = self.call(event({'action': 'create', 'email': ' ivan@example.com ', 'offer': True,
                                        'offer_at': '2026-10-02T19:59:00Z', 'fill_ms': 5000, 'website': ''}))
        self.assertEqual(status, 200)
        self.assertEqual(body['url'], 'https://yoomoney.ru/checkout/x')
        method, path, payload = self.api.call_args[0]
        self.assertEqual((method, path), ('POST', '/payments'))
        self.assertEqual(payload['amount'], {'value': '499.00', 'currency': 'RUB'})
        self.assertTrue(payload['capture'])
        self.assertEqual(payload['metadata']['email'], 'ivan@example.com')
        self.assertEqual(payload['confirmation']['return_url'], 'https://impression-team.github.io/tspl/thanks.html')
        self.assertNotIn('receipt', payload)
        self.assertTrue(self.api.call_args[1]['idempotence_key'])

    def test_create_with_receipt(self):
        self.api.return_value = {'id': PID, 'confirmation': {'confirmation_url': 'u'}}
        with mock.patch.dict(os.environ, {'RECEIPTS': '1'}):
            self.call(event({'action': 'create', 'email': 'a@b.ru', 'offer': True, 'fill_ms': 5000}))
        receipt = self.api.call_args[0][2]['receipt']
        self.assertEqual(receipt['customer'], {'email': 'a@b.ru'})
        self.assertEqual(receipt['items'][0]['amount']['value'], '499.00')
        self.assertEqual(receipt['items'][0]['vat_code'], 1)

    def test_create_rejects_bad_input(self):
        cases = [
            ({'action': 'create', 'email': 'nope', 'offer': True, 'fill_ms': 5000}, 'email'),
            ({'action': 'create', 'email': 'a@b.ru', 'offer': False, 'fill_ms': 5000}, 'offer'),
            ({'action': 'create', 'email': 'a@b.ru', 'offer': True, 'fill_ms': 100}, 'bot'),
            ({'action': 'create', 'email': 'a@b.ru', 'offer': True, 'fill_ms': 5000, 'website': 'x'}, 'bot'),
        ]
        for payload, err in cases:
            status, body = self.call(event(payload))
            self.assertEqual((status, body['error']), (400, err))
        self.api.assert_not_called()

    def test_foreign_origin_is_refused(self):
        status, body = self.call(event({'action': 'create', 'email': 'a@b.ru', 'offer': True, 'fill_ms': 5000},
                                       origin='https://evil.example'))
        self.assertEqual(status, 403)
        self.api.assert_not_called()

    # ---- status
    def test_status_returns_valid_key_once_paid(self):
        self.api.return_value = paid()
        status, body = self.call(event({'action': 'status', 'payment_id': PID}))
        self.assertEqual((status, body['status']), (200, 'succeeded'))
        info = verify_key(body['key'], ed25519.public_from_seed(TEST_SEED))
        self.assertEqual(info, {'id': body['license_id'], 'v': 1, 'd': '2026-10-03', 'n': 'ivan@example.com'})
        self.assertRegex(body['license_id'], r'^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$')
        self.send.assert_not_called()

    def test_same_payment_same_key(self):
        self.api.return_value = paid()
        k1 = self.call(event({'action': 'status', 'payment_id': PID}))[1]['key']
        k2 = self.call(event({'action': 'status', 'payment_id': PID}))[1]['key']
        self.assertEqual(k1, k2)
        self.api.return_value = paid(id='2e8a5a3b-000f-5000-9000-ffffffffffff')
        k3 = self.call(event({'action': 'status', 'payment_id': '2e8a5a3b-000f-5000-9000-ffffffffffff'}))[1]['key']
        self.assertNotEqual(k1, k3)

    def test_status_without_payment_gives_no_key(self):
        for p in (paid(status='pending', paid=False), paid(status='canceled', paid=False),
                  paid(amount={'value': '1.00', 'currency': 'RUB'}),
                  paid(metadata={'product': 'other', 'email': 'a@b.ru'})):
            self.api.return_value = p
            status, body = self.call(event({'action': 'status', 'payment_id': PID}))
            self.assertNotIn('key', body)

    def test_status_rejects_garbage_id(self):
        status, body = self.call(event({'action': 'status', 'payment_id': '../payments'}))
        self.assertEqual(status, 400)
        self.api.assert_not_called()

    # ---- notification
    def test_notification_rereads_payment_and_mails_key(self):
        self.api.return_value = paid()
        fake_body = {'type': 'notification', 'event': 'payment.succeeded',
                     'object': {'id': PID, 'status': 'succeeded', 'metadata': {'email': 'attacker@x.ru'}}}
        status, _ = self.call(event(fake_body, origin=None))
        self.assertEqual(status, 200)
        self.api.assert_called_once_with('GET', '/payments/' + PID)
        buyer, owner = [c[0][0] for c in self.send.call_args_list]
        self.assertEqual(buyer['To'], 'ivan@example.com')        # from the API, not from the body
        self.assertIn('TSPL-', buyer.get_content())
        self.assertEqual(owner['To'], 'owner@yandex.ru')
        self.assertIn('ivan@example.com', owner['Subject'])

    def test_notification_for_unpaid_payment_sends_nothing(self):
        self.api.return_value = paid(status='canceled', paid=False)
        status, _ = self.call(event({'type': 'notification', 'event': 'payment.succeeded',
                                     'object': {'id': PID}}, origin=None))
        self.assertEqual(status, 200)
        self.send.assert_not_called()

    def test_notification_asks_for_retry_when_mail_fails(self):
        self.api.return_value = paid()
        self.send.side_effect = OSError('smtp down')
        status, _ = self.call(event({'type': 'notification', 'event': 'payment.succeeded',
                                     'object': {'id': PID}}, origin=None))
        self.assertEqual(status, 500)

    def test_other_events_are_ignored(self):
        status, _ = self.call(event({'type': 'notification', 'event': 'payment.canceled',
                                     'object': {'id': PID}}, origin=None))
        self.assertEqual(status, 200)
        self.api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
