"""Yandex Cloud Function: sells TSPL Label Print licenses through YooKassa.

Three kinds of requests arrive at the same URL:

1. {"action": "create", "email": ..., "offer": true, ...} from tspl/index.html
   Creates a YooKassa payment and returns the URL of its payment page.
2. {"action": "status", "payment_id": ...} from tspl/thanks.html
   Asks YooKassa about the payment; once it has succeeded, returns the key
   so the buyer sees it right away.
3. {"type": "notification", "event": "payment.succeeded", ...} from YooKassa
   Emails the key to the buyer and a sale notice to the owner.

The notification body is never trusted: the payment is always re-read from
the YooKassa API with the shop's own credentials before a key is issued.

Keys are deterministic: the same payment always yields the same key
(key id is derived from the payment id, Ed25519 signatures are deterministic),
so repeated notifications or status checks never create extra licenses and no
database is needed.

Environment:
  YOOKASSA_SHOP_ID      shop id from the YooKassa dashboard
  YOOKASSA_SECRET_KEY   secret API key (Lockbox)
  TSPL_PRIVATE_KEY      base64 Ed25519 seed from Licensing/private_key.txt (Lockbox)
  SMTP_USER             Yandex Mail login used to send, e.g. vitaliy.chardymov@yandex.ru
  SMTP_PASSWORD         app password for SMTP_USER (Lockbox, shared with form-handler)
  MAIL_TO               where sale notices go (default: SMTP_USER)
  SUPPORT_EMAIL         Reply-To for buyers (default: vitaliy.chardymov@gmail.com)
  PRICE_RUB             price, e.g. 499.00
  RECEIPTS              1 = send a 54-FZ receipt through YooKassa (needs it enabled in the shop)
  RECEIPT_VAT_CODE      YooKassa vat_code, default 1 (no VAT)
  RECEIPT_SUBJECT       payment_subject, default "service"
  RECEIPT_TAX_SYSTEM    tax_system_code, optional (only if the shop has several)
  ALLOWED_ORIGINS       space-separated origins allowed to call create/status
"""

import base64
import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import ed25519

SITE = 'https://impression-team.github.io'
RETURN_URL = SITE + '/tspl/thanks.html'
DOWNLOAD_URL = SITE + '/tspl/#download'
OFFER_URL = SITE + '/tspl/oferta.html'
PRODUCT = 'tspl'
API = 'https://api.yookassa.ru/v3'

SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.yandex.ru')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
ALLOWED_ORIGINS = set(os.environ.get('ALLOWED_ORIGINS', SITE).split())
EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+$')
PAYMENT_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
KEY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
MIN_FILL_MS = 1500
MAX_BODY_BYTES = 20000

log = logging.getLogger()
log.setLevel(logging.INFO)


# ---------------------------------------------------------------- HTTP helpers

def _response(status, payload, origin=''):
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Vary': 'Origin'}
    if origin in ALLOWED_ORIGINS:
        headers['Access-Control-Allow-Origin'] = origin
        headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return {'statusCode': status, 'headers': headers, 'body': json.dumps(payload, ensure_ascii=False)}


def _fail(status, error, origin=''):
    return _response(status, {'success': False, 'error': error}, origin)


def price():
    return '{:.2f}'.format(float(os.environ.get('PRICE_RUB', '499')))


# ---------------------------------------------------------------- YooKassa

def _yookassa(method, path, payload=None, idempotence_key=None):
    auth = '{}:{}'.format(os.environ['YOOKASSA_SHOP_ID'], os.environ['YOOKASSA_SECRET_KEY'])
    headers = {
        'Authorization': 'Basic ' + base64.b64encode(auth.encode()).decode(),
        'Content-Type': 'application/json',
    }
    if idempotence_key:
        headers['Idempotence-Key'] = idempotence_key
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log.error('YooKassa %s %s -> %s %s', method, path, e.code, e.read()[:500])
        raise


def _receipt(email):
    item = {
        'description': 'Лицензия на программу TSPL Label Print для macOS',
        'quantity': '1.00',
        'amount': {'value': price(), 'currency': 'RUB'},
        'vat_code': int(os.environ.get('RECEIPT_VAT_CODE', '1')),
        'payment_mode': 'full_payment',
        'payment_subject': os.environ.get('RECEIPT_SUBJECT', 'service'),
    }
    receipt = {'customer': {'email': email}, 'items': [item]}
    if os.environ.get('RECEIPT_TAX_SYSTEM'):
        receipt['tax_system_code'] = int(os.environ['RECEIPT_TAX_SYSTEM'])
    return receipt


def create_payment(email, offer_at):
    body = {
        'amount': {'value': price(), 'currency': 'RUB'},
        'capture': True,
        'confirmation': {'type': 'redirect', 'return_url': RETURN_URL},
        'description': 'TSPL Label Print — лицензия',
        'metadata': {'product': PRODUCT, 'email': email, 'offer_at': offer_at[:40]},
    }
    if os.environ.get('RECEIPTS') == '1':
        body['receipt'] = _receipt(email)
    return _yookassa('POST', '/payments', body, idempotence_key=str(uuid.uuid4()))


def get_payment(payment_id):
    return _yookassa('GET', '/payments/' + payment_id)


def is_paid_license(payment):
    meta = payment.get('metadata') or {}
    amount = payment.get('amount') or {}
    return (payment.get('status') == 'succeeded'
            and payment.get('paid') is True
            and meta.get('product') == PRODUCT
            and EMAIL_RE.match(meta.get('email') or '') is not None
            and amount.get('currency') == 'RUB'
            and float(amount.get('value') or 0) >= float(price()) - 0.001)


# ---------------------------------------------------------------- keys

def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def key_id_for(payment_id):
    """8 characters from the payment id, in the same alphabet keygen.py uses."""
    n = int.from_bytes(hashlib.sha256(('tspl:' + payment_id).encode()).digest()[:5], 'big')
    chars = []
    for _ in range(8):
        chars.append(KEY_ALPHABET[n & 31])
        n >>= 5
    s = ''.join(chars)
    return s[:4] + '-' + s[4:]


def license_for(payment):
    """Same layout as keygen.py: TSPL-<base64url(JSON)>.<base64url(signature)>."""
    seed = base64.b64decode(os.environ['TSPL_PRIVATE_KEY'].strip())
    meta = payment['metadata']
    when = (payment.get('captured_at') or payment.get('created_at') or '')[:10]
    payload = {'id': key_id_for(payment['id']), 'v': 1, 'd': when, 'n': meta['email']}
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    return payload['id'], 'TSPL-' + _b64url(data) + '.' + _b64url(ed25519.sign(seed, data))


# ---------------------------------------------------------------- email

BUYER_TEXT = """Здравствуйте!

Спасибо за покупку TSPL Label Print. Ваш лицензионный ключ:

{key}

Как активировать:
1. Откройте TSPL Label Print.
   Ещё не установили? Скачайте: {download}
2. Внизу окна нажмите «Ввести ключ…».
3. Вставьте ключ целиком (от TSPL- до конца) и нажмите «Активировать».

Ключ работает без интернета и не привязан к компьютеру — сохраните это письмо.
Номер лицензии: {kid}. Номер платежа: {payment_id}.

Если что-то не получается — просто ответьте на это письмо.
"""


def _send(msg):
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=10) as smtp:
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        smtp.send_message(msg)


def _message(to, subject, text, reply_to=None):
    sender = os.environ['SMTP_USER']
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr(('TSPL Label Print', sender))
    msg['To'] = to
    if reply_to:
        msg['Reply-To'] = reply_to
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain='impression-team.github.io')
    msg.set_content(text)
    return msg


def send_license(payment, kid, key):
    email = payment['metadata']['email']
    test = ' [ТЕСТ]' if payment.get('test') else ''
    support = os.environ.get('SUPPORT_EMAIL', 'vitaliy.chardymov@gmail.com')
    _send(_message(email, 'Ваш ключ TSPL Label Print' + test,
                   BUYER_TEXT.format(key=key, kid=kid, payment_id=payment['id'], download=DOWNLOAD_URL),
                   reply_to=support))
    owner = os.environ.get('MAIL_TO') or os.environ['SMTP_USER']
    amount = payment.get('amount') or {}
    _send(_message(owner, 'Продажа TSPL Label Print{}: {}'.format(test, email), '\n'.join([
        'Покупатель: ' + email,
        'Сумма: {} {}'.format(amount.get('value'), amount.get('currency')),
        'Лицензия: ' + kid,
        'Платёж ЮKassa: ' + payment['id'],
        'Оплачен: ' + str(payment.get('captured_at') or payment.get('created_at')),
        'Оферта принята: ' + str((payment.get('metadata') or {}).get('offer_at')),
        '',
        'Ключ уже отправлен покупателю. Если он потеряет письмо, ключ можно',
        'увидеть повторно на ' + RETURN_URL + '?p=' + payment['id'],
    ])))


# ---------------------------------------------------------------- handlers

def handle_create(data, origin):
    if str(data.get('website') or '').strip():          # honeypot
        return _fail(400, 'bot', origin)
    fill_ms = data.get('fill_ms')
    if not isinstance(fill_ms, (int, float)) or fill_ms < MIN_FILL_MS:
        return _fail(400, 'bot', origin)
    email = str(data.get('email') or '').strip()
    if len(email) > 150 or not EMAIL_RE.match(email):
        return _fail(400, 'email', origin)
    if data.get('offer') is not True:
        return _fail(400, 'offer', origin)
    offer_at = ' '.join(str(data.get('offer_at') or '').split()) or '—'
    try:
        payment = create_payment(email, offer_at)
    except Exception:
        log.exception('create payment failed')
        return _fail(502, 'payment', origin)
    url = ((payment.get('confirmation') or {}).get('confirmation_url'))
    if not url:
        return _fail(502, 'payment', origin)
    log.info('payment %s created', payment.get('id'))
    return _response(200, {'success': True, 'payment_id': payment['id'], 'url': url}, origin)


def handle_status(data, origin):
    payment_id = str(data.get('payment_id') or '').strip().lower()
    if not PAYMENT_ID_RE.match(payment_id):
        return _fail(400, 'payment_id', origin)
    try:
        payment = get_payment(payment_id)
    except urllib.error.HTTPError as e:
        return _fail(404 if e.code == 404 else 502, 'not_found' if e.code == 404 else 'payment', origin)
    except Exception:
        log.exception('status failed')
        return _fail(502, 'payment', origin)
    if (payment.get('metadata') or {}).get('product') != PRODUCT:
        return _fail(404, 'not_found', origin)
    if is_paid_license(payment):
        kid, key = license_for(payment)
        return _response(200, {'success': True, 'status': 'succeeded', 'key': key, 'license_id': kid,
                               'email': payment['metadata']['email']}, origin)
    return _response(200, {'success': True, 'status': payment.get('status')}, origin)


def handle_notification(data):
    obj = data.get('object') or {}
    payment_id = str(obj.get('id') or '')
    if data.get('event') != 'payment.succeeded' or not PAYMENT_ID_RE.match(payment_id):
        return _response(200, {'ignored': True})
    try:
        payment = get_payment(payment_id)        # never trust the notification body
    except Exception:
        log.exception('notification: cannot read payment %s', payment_id)
        return _fail(500, 'payment')                # YooKassa will retry
    if not is_paid_license(payment):
        log.info('notification for %s ignored (status %s)', payment_id, payment.get('status'))
        return _response(200, {'ignored': True})
    kid, key = license_for(payment)
    try:
        send_license(payment, kid, key)
    except Exception:
        log.exception('mail for %s failed', payment_id)
        return _fail(500, 'mail')                   # retry later
    log.info('license %s sent for payment %s', kid, payment_id)
    return _response(200, {'success': True})


def handler(event, context):
    headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
    origin = headers.get('origin', '')
    method = (event.get('httpMethod') or 'POST').upper()
    if method == 'OPTIONS':
        return _response(204, {}, origin)
    if method != 'POST':
        return _fail(405, 'method', origin)

    body = event.get('body') or ''
    if event.get('isBase64Encoded'):
        body = base64.b64decode(body).decode('utf-8', 'replace')
    if len(body.encode('utf-8')) > MAX_BODY_BYTES:
        return _fail(413, 'too_large', origin)
    try:
        data = json.loads(body)
    except ValueError:
        return _fail(400, 'bad_json', origin)
    if not isinstance(data, dict):
        return _fail(400, 'bad_json', origin)

    if data.get('type') == 'notification':
        return handle_notification(data)

    if origin not in ALLOWED_ORIGINS:
        return _fail(403, 'origin', origin)
    action = data.get('action')
    if action == 'create':
        return handle_create(data, origin)
    if action == 'status':
        return handle_status(data, origin)
    return _fail(400, 'action', origin)
