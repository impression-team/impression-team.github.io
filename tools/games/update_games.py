#!/usr/bin/env python3
"""Builds the game cards on index.html from the stores.

Reads tools/games/games.json, then for every game:
  * App Store (iTunes Lookup API): name, icon, link and ratings summed over all storefronts;
  * Google Play (through Google APIs, see google_play.py; needs the service account key):
    rating and number of ratings from the Play Console reports, whether the game is live in production
    (no live release -> no Google Play button), name and icon for games that are only on Google Play;
    without the key or network the last known values from ratings.json are kept;
  * icon saved to assets/games/<slug>.jpg (App Store, then "icon_file" on disk, then the Play listing).
Then rewrites the block between <!-- GAMES:START --> and <!-- GAMES:END --> in index.html.

Usage:  python3 tools/games/update_games.py [--quick | --offline]
  --quick    only the US App Store (seconds instead of minutes; ratings then cover the US only)
  --offline  no network: rebuild the cards from ratings.json and the icons already in assets/games
             (use after editing games.json or the display thresholds)
Needs macOS `sips` for local icons. Standard library only.
"""

import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CONFIG = os.path.join(ROOT, 'tools', 'games', 'games.json')
RATINGS = os.path.join(ROOT, 'tools', 'games', 'ratings.json')
ICON_DIR = os.path.join(ROOT, 'assets', 'games')
INDEX = os.path.join(ROOT, 'index.html')

MIN_RATINGS = 5          # fewer ratings than this: the rating is not shown (too noisy)
MIN_RATING = 3.5         # ratings below this are not shown on the site (still saved in ratings.json)
NEW_BADGE_DAYS = 90      # "New" badge for games released within this many days
ICON_PX = 256
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'

# App Store storefronts. Unknown or unsupported codes simply return no results.
STOREFRONTS = '''us gb ca au nz ie de at ch fr be nl lu es pt it gr mt cy se no dk fi is ee lv lt pl cz sk hu
si hr ba rs me mk al bg ro md ua by ru kz kg uz tj tm am az ge tr il ae sa qa kw bh om jo lb eg ma dz tn ly
iq ye in pk lk bd np bt mv cn hk mo tw jp kr mn sg my th vn ph id kh la mm bn mx gt sv hn ni cr pa br ar cl
co pe ec bo py uy ve do jm tt bb bs bz ag dm gd kn lc vc ai bm ky ms tc vg sr gy za ng ke gh tz ug rw zm zw
mw mz ao na bw sz cm ci sn ml ne bf bj cv gm gw lr sl mr td cg cd ga mg mu sc fj pg sb to vu fm pw nr xk'''.split()


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def itunes_lookup(ids, country):
    url = 'https://itunes.apple.com/lookup?id={}&country={}&entity=software'.format(','.join(map(str, ids)), country)
    for attempt in range(5):
        try:
            return json.loads(get(url)).get('results', [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5)
    print('  ! {}: gave up'.format(country), file=sys.stderr)
    return []


def appstore_data(ids, quick):
    """US metadata + ratings summed over storefronts: {id: {...}}."""
    meta = {r['trackId']: r for r in itunes_lookup(ids, 'us') if r.get('trackId') in ids}
    totals = {i: [0.0, 0] for i in ids}            # sum(avg*count), sum(count)
    countries = ['us'] if quick else STOREFRONTS
    for n, country in enumerate(countries, 1):
        results = meta.values() if country == 'us' else itunes_lookup(ids, country)
        for r in results:
            tid, cnt = r.get('trackId'), r.get('userRatingCount') or 0
            if tid in totals and cnt:
                totals[tid][0] += (r.get('averageUserRating') or 0) * cnt
                totals[tid][1] += cnt
        if not quick and country != 'us':
            if n % 25 == 0:
                print('  App Store: {}/{} storefronts'.format(n, len(countries)))
            time.sleep(3)                           # stay well under the API rate limit
    for tid, (weighted, cnt) in totals.items():
        if tid in meta:
            meta[tid]['_rating'] = round(weighted / cnt, 2) if cnt else None
            meta[tid]['_count'] = cnt
    return meta


def google_play_data(games, bucket, skip):
    """{package: {'name', 'rating', 'count', 'live'}} for the packages in games.json, or None if unavailable."""
    sys.path.insert(0, os.path.dirname(__file__))
    import google_play as gp
    try:
        apps = {a['package']: a['name'] for a in gp.apps()}
        packages = [g['google_play'] for g in games if g.get('google_play')]
        ratings = gp.ratings(bucket.rstrip('/') + '/stats/ratings/', packages) if bucket else {}
        counts = gp.rating_counts(bucket) if bucket else {}
        out = {}
        for pkg in packages:
            out[pkg] = {'name': apps.get(pkg), 'rating': (ratings.get(pkg) or {}).get('rating'),
                        'count': counts.get(pkg, 0), 'live': pkg in apps and gp.is_live(pkg)}
        missing = sorted(set(apps) - set(packages) - set(skip))
        if missing:
            print('  Google Play apps not on the site: ' + ', '.join('{} ({})'.format(apps[p], p) for p in missing))
        return out, gp
    except gp.GooglePlayError as err:
        print('  ! Google Play API unavailable ({}); keeping last known values'.format(err), file=sys.stderr)
        return None, gp


def save_icon(slug, src_url=None, src_file=None, play_url=None):
    os.makedirs(ICON_DIR, exist_ok=True)
    out = os.path.join(ICON_DIR, slug + '.jpg')
    if src_url:
        url = re.sub(r'/\d+x\d+bb\.(jpg|png)$', '/{0}x{0}bb.jpg'.format(ICON_PX), src_url)
        with open(out, 'wb') as f:
            f.write(get(url))
    elif play_url:
        tmp = out + '.download'
        with open(tmp, 'wb') as f:
            f.write(get('{}=s{}'.format(play_url, ICON_PX)))
        try:
            subprocess.run(['sips', '-Z', str(ICON_PX), '-s', 'format', 'jpeg', '-s', 'formatOptions', '85',
                            tmp, '--out', out], check=True, capture_output=True)
        finally:
            os.remove(tmp)
    elif src_file:
        subprocess.run(['sips', '-Z', str(ICON_PX), '-s', 'format', 'jpeg', '-s', 'formatOptions', '85',
                        os.path.expanduser(src_file), '--out', out], check=True, capture_output=True)
    return 'assets/games/{}.jpg'.format(slug)


def short_count(n):
    if n >= 1000000:
        return '{:.1f}M'.format(n / 1e6).replace('.0M', 'M')
    if n >= 1000:
        return '{:.1f}K'.format(n / 1e3).replace('.0K', 'K')
    return str(n)


def rating_html(store, rating, count):
    if not rating or count < MIN_RATINGS or rating < MIN_RATING:
        return ''
    word = 'rating' if count == 1 else 'ratings'
    return ('        <span class="game-rating" title="{store}: {r:.1f} out of 5, {c} {w}">'
            '<span class="star" aria-hidden="true">★</span> {r:.1f}'
            ' <small>{store} · {cs}</small></span>\n').format(store=store, r=rating, c=count, w=word, cs=short_count(count))


def card_html(g):
    e = html.escape
    badge = '<span class="game-badge">New</span>' if g.get('new') else ''
    links = ''
    if g.get('appstore_url'):
        links += '        <a href="{}" target="_blank" rel="noopener" class="store-btn">App Store</a>\n'.format(e(g['appstore_url']))
    if g.get('google_play'):
        links += ('        <a href="https://play.google.com/store/apps/details?id={}" target="_blank" rel="noopener" '
                  'class="store-btn">Google Play</a>\n').format(e(g['google_play']))
    ratings = rating_html('App Store', g.get('as_rating'), g.get('as_count', 0)) + \
        rating_html('Google Play', g.get('gp_rating'), g.get('gp_count', 0))
    # the whole card opens the game's press page (the title link is stretched over the card in CSS)
    return (
        '  <article class="game-card">\n'
        '    <div class="game-head">\n'
        '      <img class="game-icon" src="{icon}" alt="{name} icon" width="80" height="80" loading="lazy">\n'
        '      <div>\n'
        '        <h3><a class="game-link" href="{page}/">{name}</a></h3>\n'
        '        <span class="game-genre-tag tag-{tag}">{genre}</span>{badge}\n'
        '      </div>\n'
        '    </div>\n'
        '    <p class="game-desc">{desc}</p>\n'
        '{ratings_block}'
        '    <div class="game-links">\n{links}        <span class="game-more">Press kit ›</span>\n    </div>\n'
        '  </article>\n'
    ).format(icon=e(g['icon']), name=e(g['name']), tag=e(g['tag']), genre=e(g['genre']), badge=badge,
             page=e(g.get('page') or g['slug']),
             desc=e(g['description']), links=links,
             ratings_block='      <div class="game-ratings">\n{}      </div>\n'.format(ratings) if ratings else '')


def main():
    quick, offline = '--quick' in sys.argv, '--offline' in sys.argv
    config = json.load(open(CONFIG, encoding='utf-8'))
    games = config['games']
    known = json.load(open(RATINGS, encoding='utf-8')) if os.path.exists(RATINGS) else {}

    ids = [g['appstore_id'] for g in games if g.get('appstore_id')]
    if offline:
        print('Offline: using ratings.json and existing icons')
        store = {}
    else:
        print('App Store: {} apps, {}'.format(len(ids), 'US only' if quick else '{} storefronts'.format(len(STOREFRONTS))))
        store = appstore_data(ids, quick)

    play, gp = (None, None)
    if not offline:
        print('Google Play: reports, live status and names')
        play, gp = google_play_data(games, config.get('google_play_bucket'), config.get('google_play_skip', []))
    today = dt.date.today()
    for g in games:
        slug = g['slug']
        prev = known.get(slug, {})
        a = store.get(g.get('appstore_id'))
        if offline:
            for k in ('name', 'as_rating', 'as_count', 'appstore_url', 'new'):
                if prev.get(k) is not None and not (k == 'name' and g.get('name')):
                    g[k] = prev[k]
            g['icon'] = 'assets/games/{}.jpg'.format(slug)
            if not os.path.exists(os.path.join(ROOT, g['icon'])):
                g['icon'] = save_icon(slug, src_file=g['icon_file']) if g.get('icon_file') else g['icon']
        elif a:
            g['name'] = g.get('name') or a['trackName']
            g['appstore_url'] = a['trackViewUrl'].split('?')[0]
            g['as_rating'], g['as_count'] = a.get('_rating'), a.get('_count', 0)
            released = dt.date.fromisoformat(a['releaseDate'][:10])
            g['new'] = (today - released).days <= NEW_BADGE_DAYS
            g['icon'] = save_icon(slug, src_url=a['artworkUrl512'])
        else:
            if g.get('appstore_id'):
                print('  ! {}: not found in the App Store'.format(slug), file=sys.stderr)
            g['icon'] = None

        g['gp_rating'], g['gp_count'] = prev.get('gp_rating'), prev.get('gp_count', 0)
        g['gp_live'] = prev.get('gp_live', True)
        p = (play or {}).get(g.get('google_play'))
        if p:
            g['gp_rating'], g['gp_count'], g['gp_live'] = p['rating'], p['count'], p['live']
            g['name'] = g.get('name') or p['name']
        if g.get('google_play') and not g['gp_live']:
            print('  ! {}: no live production release on Google Play, button hidden'.format(slug), file=sys.stderr)
        if not g.get('gp_live'):
            g['google_play'] = None
        if not g.get('name'):
            g['name'] = prev.get('name') or slug

        if g['icon'] is None:                      # games that are not on the App Store
            if g.get('icon_file'):
                g['icon'] = save_icon(slug, src_file=g['icon_file'])
            elif gp and g.get('google_play') and not offline:
                try:
                    g['icon'] = save_icon(slug, play_url=gp.listing(g['google_play'])['icon'])
                except Exception as err:            # keep the previous icon if the download fails
                    print('  ! {}: Play icon not downloaded ({})'.format(slug, err), file=sys.stderr)
            if g['icon'] is None:
                g['icon'] = 'assets/games/{}.jpg'.format(slug)

        known[slug] = {k: g.get(k) for k in ('name', 'appstore_url', 'new', 'as_rating', 'as_count',
                                            'gp_rating', 'gp_count', 'gp_live')}
        known[slug]['checked'] = prev.get('checked') if offline else today.isoformat()
        print('  {:28} AS {:>4} ({:>4})  GP {:>4} ({:>4})'.format(
            slug, g.get('as_rating') or '-', g.get('as_count') or 0, g.get('gp_rating') or '-', g.get('gp_count') or 0))

    with open(RATINGS, 'w', encoding='utf-8') as f:
        json.dump(known, f, ensure_ascii=False, indent=2)
        f.write('\n')

    page = open(INDEX, encoding='utf-8').read()
    start, end = '<!-- GAMES:START -->', '<!-- GAMES:END -->'
    block = '{}\n<!-- Generated by tools/games/update_games.py on {} — edit tools/games/games.json, not this block -->\n{}{}'.format(
        start, today.isoformat(), ''.join(card_html(g) for g in games), end)
    page = re.sub(re.escape(start) + r'.*?' + re.escape(end), lambda m: block, page, count=1, flags=re.S)
    page = re.sub(r'(<div class="stat-num">)\d+\+(</div>\s*<div class="stat-label">Games Released)',
                  lambda m: '{}{}+{}'.format(m.group(1), len(games), m.group(2)), page, count=1)
    open(INDEX, 'w', encoding='utf-8').write(page)
    print('index.html updated: {} games'.format(len(games)))


if __name__ == '__main__':
    main()
