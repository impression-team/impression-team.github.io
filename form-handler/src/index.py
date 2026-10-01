"""Yandex Cloud Function: accepts the request form from services.html and emails it.

The browser posts JSON as text/plain (a "simple" CORS request, so no preflight).
The function validates the data, checks that personal data consent was given,
and forwards the request to MAIL_TO through Yandex Mail SMTP.

Environment:
  SMTP_USER       Yandex Mail login used to send, e.g. chardymov@yandex.ru
  SMTP_PASSWORD   app password for SMTP_USER (comes from Lockbox, never in code)
  MAIL_TO         where requests are delivered (default: SMTP_USER)
  ALLOWED_ORIGINS space-separated origins allowed to post the form
"""

import base64
import json
import logging
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.yandex.ru')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
ALLOWED_ORIGINS = set(os.environ.get('ALLOWED_ORIGINS', 'https://impression-team.github.io').split())
CONSENT_URL = 'https://impression-team.github.io/pd-consent.html'
CONSENT_EDITION = '01.10.2026'

PROJECT_TYPES = {'Игра / Game', 'Приложение / App', 'Сайт / Website', 'Другое / Other'}
LIMITS = {'name': 100, 'email': 150, 'phone': 100, 'budget': 200, 'message': 5000}
EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+$')
MIN_FILL_MS = 3000          # bots submit instantly; people need a few seconds
MAX_BODY_BYTES = 20000

log = logging.getLogger()
log.setLevel(logging.INFO)


def _response(status, payload, origin):
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Vary': 'Origin'}
    if origin in ALLOWED_ORIGINS:
        headers['Access-Control-Allow-Origin'] = origin
        headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return {'statusCode': status, 'headers': headers, 'body': json.dumps(payload, ensure_ascii=False)}


def _ok(origin):
    return _response(200, {'success': True}, origin)


def _fail(status, error, origin):
    return _response(status, {'success': False, 'error': error}, origin)


def _single_line(value):
    return ' '.join(value.split())


def _build_email(fields, consent_at, ip, user_agent, lang):
    sender = os.environ['SMTP_USER']
    received_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    msg = EmailMessage()
    msg['Subject'] = 'Заявка с сайта: ' + fields['project_type']
    msg['From'] = formataddr(('Impression Team — заявки', sender))
    msg['To'] = os.environ.get('MAIL_TO') or sender
    msg['Reply-To'] = formataddr((_single_line(fields['name']), fields['email']))
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain='impression-team.github.io')

    msg.set_content('\n'.join([
        'Новая заявка с impression-team.github.io/services.html',
        '',
        'Имя: ' + fields['name'],
        'Email: ' + fields['email'],
        'Телефон / Telegram: ' + (fields['phone'] or '—'),
        'Что нужно сделать: ' + fields['project_type'],
        'Бюджет и сроки: ' + (fields['budget'] or '—'),
        'Язык страницы: ' + lang,
        '',
        'Описание задачи:',
        fields['message'],
        '',
        '— — —',
        'Согласие на обработку персональных данных: ДАНО',
        'Время по данным браузера: ' + consent_at,
        'Время получения сервером: ' + received_at,
        'IP-адрес: ' + (ip or '—'),
        'Браузер: ' + (user_agent or '—'),
        'Текст согласия: {} (редакция от {})'.format(CONSENT_URL, CONSENT_EDITION),
        '',
        'Чтобы ответить клиенту, просто нажмите «Ответить».',
    ]))
    return msg


def _send(msg):
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=10) as smtp:
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
        smtp.send_message(msg)


def handler(event, context):
    headers = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
    origin = headers.get('origin', '')
    method = (event.get('httpMethod') or 'POST').upper()

    if method == 'OPTIONS':
        return _response(204, {}, origin)
    if method != 'POST':
        return _fail(405, 'method', origin)
    if origin not in ALLOWED_ORIGINS:
        return _fail(403, 'origin', origin)

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

    # Bot traps: answer "success" so bots don't learn anything, but send nothing.
    if str(data.get('website') or '').strip():
        log.info('honeypot filled, dropped')
        return _ok(origin)
    fill_ms = data.get('fill_ms')
    if not isinstance(fill_ms, (int, float)) or fill_ms < MIN_FILL_MS:
        log.info('filled too fast (%s ms), dropped', fill_ms)
        return _ok(origin)

    fields = {key: str(data.get(key) or '').strip() for key in LIMITS}
    fields['project_type'] = str(data.get('project_type') or '').strip()

    if any(len(fields[key]) > limit for key, limit in LIMITS.items()):
        return _fail(400, 'too_long', origin)
    if not fields['name'] or not fields['message'] or not EMAIL_RE.match(fields['email']):
        return _fail(400, 'required', origin)
    if fields['project_type'] not in PROJECT_TYPES:
        return _fail(400, 'project_type', origin)

    # No consent — no processing: reject before anything is sent or stored.
    if data.get('consent') is not True:
        return _fail(400, 'consent', origin)
    consent_at = _single_line(str(data.get('consent_at') or ''))[:40] or '—'
    lang = 'en' if data.get('lang') == 'en' else 'ru'

    identity = (event.get('requestContext') or {}).get('identity') or {}
    ip = identity.get('sourceIp') or headers.get('x-forwarded-for', '').split(',')[0].strip()
    user_agent = _single_line(headers.get('user-agent', ''))[:300]

    try:
        _send(_build_email(fields, consent_at, ip, user_agent, lang))
    except Exception:
        log.exception('failed to send request email')
        return _fail(502, 'mail', origin)

    log.info('request delivered')
    return _ok(origin)
