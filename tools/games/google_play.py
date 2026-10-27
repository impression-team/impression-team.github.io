"""Google Play data through Google APIs (play.google.com itself is unreachable from this Mac).

Auth: a service account JSON key, by default ~/.config/impression-team/play-service-account.json
(override with PLAY_SERVICE_ACCOUNT). The key never goes into the repo; the OAuth token is signed
with the system `openssl`, so no third-party Python packages are needed.

  apps()                -> [{'package': ..., 'name': ...}] for every app the service account can see
                           (Google Play Developer Reporting API)
  ratings(bucket, pkgs) -> {package: {'rating': float, 'count': int|None, 'month': 'YYYYMM'}}
                           from the Play Console bulk ratings reports in Cloud Storage

Command line:  python3 tools/games/google_play.py [gs://pubsite_prod_rev_.../stats/ratings/]
"""

import base64
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

KEY_FILE = os.path.expanduser(os.environ.get('PLAY_SERVICE_ACCOUNT', '~/.config/impression-team/play-service-account.json'))
SCOPES = ['https://www.googleapis.com/auth/playdeveloperreporting',
          'https://www.googleapis.com/auth/androidpublisher',
          'https://www.googleapis.com/auth/devstorage.read_only']
_token = None


class GooglePlayError(Exception):
    pass


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def _sign_rs256(message, private_key_pem):
    with tempfile.NamedTemporaryFile() as data:
        data.write(message)
        data.flush()
        res = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', '/dev/stdin', '-binary', data.name],
                             input=private_key_pem.encode(), capture_output=True)
    if res.returncode != 0:
        raise GooglePlayError('openssl could not sign the token')
    return res.stdout


def token():
    global _token
    if _token and _token[1] > time.time() + 60:
        return _token[0]
    if not os.path.exists(KEY_FILE):
        raise GooglePlayError('no service account key at ' + KEY_FILE)
    key = json.load(open(KEY_FILE))
    now = int(time.time())
    header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    claims = _b64(json.dumps({'iss': key['client_email'], 'scope': ' '.join(SCOPES),
                              'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600}).encode())
    unsigned = header + b'.' + claims
    assertion = (unsigned + b'.' + _b64(_sign_rs256(unsigned, key['private_key']))).decode()
    body = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                                   'assertion': assertion}).encode()
    try:
        data = json.load(urllib.request.urlopen('https://oauth2.googleapis.com/token', body, timeout=20))
    except urllib.error.HTTPError as e:
        raise GooglePlayError('token request failed: {} {}'.format(e.code, e.read()[:300].decode('utf-8', 'replace')))
    _token = (data['access_token'], now + int(data.get('expires_in', 3600)))
    return _token[0]


def _request(method, url, body=None, raw=False):
    """Authorized call with retries on network hiccups (this connection drops TLS handshakes now and then)."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = r.read()
            return out if raw else (json.loads(out) if out else {})
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise GooglePlayError('{} {}: {}'.format(e.code, url.split('?')[0], e.read()[:300].decode('utf-8', 'replace')))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 3:
                raise GooglePlayError('network error for {}: {}'.format(url.split('?')[0], e))
            time.sleep(3 * (attempt + 1))


def _get(url, raw=False):
    return _request('GET', url, raw=raw)


def apps():
    out, page = [], ''
    while True:
        url = 'https://playdeveloperreporting.googleapis.com/v1beta1/apps:search?pageSize=1000'
        if page:
            url += '&pageToken=' + urllib.parse.quote(page)
        data = _get(url)
        out += [{'package': a['packageName'], 'name': a.get('displayName', '')} for a in data.get('apps', [])]
        page = data.get('nextPageToken')
        if not page:
            return sorted(out, key=lambda a: a['name'].casefold())


def _parse_bucket(uri):
    uri = uri.replace('gs://', '').strip('/')
    bucket, _, prefix = uri.partition('/')
    return bucket, (prefix.rstrip('/') + '/') if prefix else 'stats/ratings/'


def ratings(bucket_uri, packages):
    """Latest monthly ratings overview per package: the last row's 'Total Average Rating'."""
    bucket, prefix = _parse_bucket(bucket_uri)
    names = _list_objects(bucket, prefix)
    result = {}
    for pkg in packages:
        files = sorted(n for n in names if n.startswith('{}ratings_{}_'.format(prefix, pkg)) and n.endswith('_overview.csv'))
        if not files:
            continue
        name = files[-1]
        rows = [r for r in _read_csv(bucket, name) if (r.get('Total Average Rating') or '').strip() not in ('', 'NA')]
        if rows:
            last = rows[-1]
            result[pkg] = {'rating': round(float(last['Total Average Rating']), 2), 'count': None,
                           'month': name.rsplit('_', 2)[-2]}
    return result



def _list_objects(bucket, prefix):
    names, page = [], ''
    while True:
        url = 'https://storage.googleapis.com/storage/v1/b/{}/o?prefix={}'.format(bucket, urllib.parse.quote(prefix))
        if page:
            url += '&pageToken=' + urllib.parse.quote(page)
        data = _get(url)
        names += [o['name'] for o in data.get('items', [])]
        page = data.get('nextPageToken')
        if not page:
            return names


def _read_csv(bucket, name):
    raw = _get('https://storage.googleapis.com/storage/v1/b/{}/o/{}?alt=media'.format(
        bucket, urllib.parse.quote(name, safe='')), raw=True)
    text = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8-sig')
    return list(csv.DictReader(io.StringIO(text)))


def rating_counts(bucket_uri):
    """{package: number of ratings} from the reviews export (it includes ratings without text).
    A review edited later can appear in several monthly files, so each is counted once."""
    bucket, _ = _parse_bucket(bucket_uri)
    seen = {}
    for name in _list_objects(bucket, 'reviews/'):
        if not name.endswith('.csv'):
            continue
        for row in _read_csv(bucket, name):
            key = row.get('Review Link') or (row.get('Device'), row.get('Review Submit Millis Since Epoch'))
            seen.setdefault(row['Package Name'], set()).add(key)
    return {pkg: len(keys) for pkg, keys in seen.items()}


def _with_edit(package, fn):
    """Runs fn(edit_url) inside a temporary edit that is always deleted, never committed."""
    base = 'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{}/edits'.format(package)
    edit = _request('POST', base, {})
    url = '{}/{}'.format(base, edit['id'])
    try:
        return fn(url)
    finally:
        _request('DELETE', url)


def is_live(package):
    """True if the production track has a release that is rolled out (completed or in progress)."""
    tracks = _with_edit(package, lambda url: _get(url + '/tracks').get('tracks', []))
    return any(t['track'] == 'production' and any(r.get('status') in ('completed', 'inProgress')
                                                  for r in t.get('releases', [])) for t in tracks)


def listing(package, language=None):
    """Store listing: {'title', 'short', 'full', 'icon'} in the app's default language (or `language`)."""
    def read(url):
        lang = language or _get(url + '/details').get('defaultLanguage', 'en-US')
        data = _get('{}/listings/{}'.format(url, lang))
        icons = _get('{}/listings/{}/icon'.format(url, lang)).get('images', [])
        return {'title': data.get('title'), 'short': data.get('shortDescription'),
                'full': data.get('fullDescription'), 'icon': icons[0]['url'] if icons else None, 'language': lang}
    return _with_edit(package, read)

if __name__ == '__main__':
    try:
        key = json.load(open(KEY_FILE))
        print('Service account:', key['client_email'])
        token()
        print('Token: OK')
        found = apps()
        print('Apps visible in Play Console: {}'.format(len(found)))
        for a in found:
            print('  {:40} {}'.format(a['package'], a['name']))
        if len(sys.argv) > 1:
            r = ratings(sys.argv[1], [a['package'] for a in found])
            print('Ratings from bulk reports: {}'.format(len(r)))
            for pkg, v in sorted(r.items()):
                print('  {:40} {} ({})'.format(pkg, v['rating'], v['month']))
    except GooglePlayError as e:
        print('ERROR:', e)
        sys.exit(1)
