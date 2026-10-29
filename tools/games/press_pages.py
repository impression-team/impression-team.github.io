#!/usr/bin/env python3
"""Builds a press page and a press kit for every game in tools/games/games.json.

For each game: <page>/index.html, <page>/media/ (icon, screenshots, optional trailer) and
<page>/presskit.zip (icon 1024, full-size screenshots, trailer key frame, fact sheet).

Sources:
  * texts       ~/Unity Projects/aso-tool/texts/<aso>/appstore|play/en-US.yaml (the store texts)
  * App Store   iTunes Lookup: name, icon, screenshots, release date, age rating, devices
  * Google Play via google_play.py (Play-only games): icon and phone screenshots of the listing
  * trailer     "press": {"video": "<path>"} in games.json — a local 16:9 video, compressed with ffmpeg
Games with "press_page": "custom" (Antheim) have a hand-made page and are skipped.

Files are rewritten only when their content changes, and zips use fixed timestamps, so running the
script again without store changes leaves git clean.

Usage:  python3 tools/games/press_pages.py [slug ...]
Needs ffmpeg (trailers) and macOS sips.
"""

import datetime as dt
import glob
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
ASO_TEXTS = os.path.expanduser('~/Unity Projects/aso-tool/texts')
CONTACT = 'vitalii.chardymov@gmail.com'
DEVELOPER = 'Impression Team'
MAX_SHOTS = 6
PAGE_SHOT_PX = 1434          # longest side on the page
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'
ZIP_TIME = (2026, 1, 1, 0, 0, 0)

try:
    import yaml
except ImportError:                                   # aso-tool keeps PyYAML in its own venv
    sys.path += glob.glob(os.path.expanduser('~/Unity Projects/aso-tool/.venv/lib/python3*/site-packages'))
    import yaml

sys.path.insert(0, HERE)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if attempt == 2:
                raise


def write_if_changed(path, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    if os.path.exists(path) and open(path, 'rb').read() == data:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    return True


def sips_jpeg(src_bytes, max_px=None, quality=85):
    """Converts any image to JPEG (optionally scaled down); returns (bytes, width, height)."""
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, 'src'), os.path.join(tmp, 'out.jpg')
        open(src, 'wb').write(src_bytes)
        cmd = ['sips', '-s', 'format', 'jpeg', '-s', 'formatOptions', str(quality)]
        if max_px:
            cmd += ['-Z', str(max_px)]
        subprocess.run(cmd + [src, '--out', out], check=True, capture_output=True)
        info = subprocess.run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', out], capture_output=True, text=True).stdout
        w, h = (int(x) for x in re.findall(r'pixel(?:Width|Height): (\d+)', info))
        return open(out, 'rb').read(), w, h


def sips_png(src_bytes, max_px):
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, 'src'), os.path.join(tmp, 'out.png')
        open(src, 'wb').write(src_bytes)
        subprocess.run(['sips', '-s', 'format', 'png', '-Z', str(max_px), src, '--out', out], check=True, capture_output=True)
        return open(out, 'rb').read()


# ── Texts ──────────────────────────────────────────────────────────────────────

def load_texts(aso):
    t = {}
    for store in ('play', 'appstore'):                 # App Store texts win where both exist
        path = os.path.join(ASO_TEXTS, aso, store, 'en-US.yaml')
        if os.path.exists(path):
            data = yaml.safe_load(open(path, encoding='utf-8')) or {}
            t.update({k: v for k, v in data.items() if v})
    return {
        'name': t.get('name') or t.get('title'),
        'subtitle': t.get('subtitle') or t.get('short_description'),
        'lead': t.get('promotional_text'),
        'description': t.get('description') or t.get('full_description') or '',
        'youtube': t.get('video'),
    }


def description_html(text):
    """Store description -> HTML: CAPS lines become headings, bullet lines become lists."""
    out, items = [], []

    def flush():
        if items:
            out.append('      <ul>\n' + ''.join('        <li>{}</li>\n'.format(html.escape(i)) for i in items) + '      </ul>')
            items.clear()

    for block in re.split(r'\n\s*\n', text.strip()):
        for line in block.split('\n'):
            line = line.strip()
            if not line:
                continue
            bullet = re.match(r'^(?:[•\-–*✓✔►▶◆★]|\d{1,2}[.)])\s*(.+)$', line)
            letters = re.sub(r'[^A-Za-z]', '', line)
            if bullet:
                items.append(bullet.group(1))
            elif letters and letters.isupper() and len(line) <= 60:
                flush()
                out.append('      <h3>{}</h3>'.format(html.escape(line)))   # store headings are in caps; kept as is
            else:
                flush()
                out.append('      <p>{}</p>'.format(html.escape(line)))
        flush()
    return '\n'.join(out)


def plain_paragraphs(text, limit=3):
    paras = [p.strip() for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    prose = [p for p in paras if not re.match(r'^[•\-–*]', p) and not re.sub(r'[^A-Za-z]', '', p).isupper()]
    return '\n\n'.join(prose[:limit])


# ── Store data ─────────────────────────────────────────────────────────────────

def appstore_lookup(ids):
    if not ids:
        return {}
    data = json.loads(get('https://itunes.apple.com/lookup?id={}&country=us'.format(','.join(map(str, ids)))))
    return {r['trackId']: r for r in data.get('results', []) if r.get('trackId')}


def full_size(mzstatic_url):
    return re.sub(r'/\d+x\d+bb\.(png|jpg|webp)$', '/9999x9999bb.jpg', mzstatic_url)


def play_media(package):
    import google_play as gp
    def read(url):
        listing = gp._get(url + '/listings/en-US/icon').get('images', [])
        shots = gp._get(url + '/listings/en-US/phoneScreenshots').get('images', [])
        return (listing[0]['url'] if listing else None), [s['url'] for s in shots]
    return gp._with_edit(package, read)


# ── Trailer ────────────────────────────────────────────────────────────────────

def make_trailer(src, media_dir):
    os.makedirs(media_dir, exist_ok=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-an', '-vf', 'scale=1280:-2', '-c:v', 'libx264',
                    '-preset', 'slow', '-crf', '28', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    os.path.join(media_dir, 'trailer.mp4')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '3', '-i', src, '-frames:v', '1', '-vf', 'scale=1280:-2',
                    '-q:v', '4', os.path.join(media_dir, 'trailer.jpg')], check=True)


def make_key_frame(src):
    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, 'f.jpg')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', '4', '-i', src, '-frames:v', '1', '-q:v', '2', frame], check=True)
        return open(frame, 'rb').read()


# ── Page ───────────────────────────────────────────────────────────────────────

PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{name} — press page | Impression Team</title>
  <meta name="description" content="{meta_desc}" />
  <link rel="canonical" href="https://impression-team.github.io/{page}/" />
  <meta property="og:type" content="website" />
  <meta property="og:title" content="{name}" />
  <meta property="og:description" content="{subtitle}" />
  <meta property="og:url" content="https://impression-team.github.io/{page}/" />
  <meta property="og:image" content="https://impression-team.github.io/{page}/media/{og_image}" />
  <meta name="twitter:card" content="summary_large_image" />
  <link rel="icon" href="media/icon-512.png" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Exo+2:wght@300;400;600&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="../assets/press.css" />
</head>
<body>
<!-- Generated by tools/games/press_pages.py — edit tools/games/games.json or the aso-tool texts, not this file -->

<nav>
  <a href="../" class="nav-logo">Impression<span>&nbsp;Team</span></a>
  <ul>
    <li><a href="#about">About</a></li>
{nav_trailer}    <li><a href="#screenshots">Screenshots</a></li>
    <li><a href="#facts">Press kit</a></li>
  </ul>
</nav>

<section id="top">
  <div class="inner hero">
    <img class="hero-icon" src="media/icon-512.png" alt="{name} app icon" width="168" height="168" />
    <div>
      <span class="section-tag">// Press page</span>
      <h1>{name}</h1>
      <p class="subtitle">{subtitle}</p>
      <div class="badges">
{badges}      </div>
    </div>
  </div>
</section>

<section id="about">
  <div class="inner">
    <span class="section-tag">// The game</span>
    <h2>About the game</h2>
    <div class="separator"></div>
    <div class="desc">
{lead}{description}
    </div>
  </div>
</section>
{trailer}
<section id="screenshots">
  <div class="inner">
    <span class="section-tag">// Screenshots</span>
    <h2>Screenshots</h2>
    <div class="separator"></div>
    <div class="shots{shots_class}">
{shots}    </div>
    <p class="note">Full-size screenshots are in the press kit below.</p>
  </div>
</section>

<section id="facts">
  <div class="inner">
    <span class="section-tag">// For press</span>
    <h2>Fact sheet</h2>
    <div class="separator"></div>
    <div class="facts">
      <table>
{facts}      </table>
      <div class="kit">
        <h3>Press kit</h3>
        <p>Everything in one archive, free to use in coverage of {name}:</p>
        <ul>
{kit_items}        </ul>
        <a class="btn" href="presskit.zip" download>Download press kit <small>(ZIP, {kit_size})</small></a>
      </div>
    </div>
  </div>
</section>

<footer>
  <p>© 2024–2026 Impression Team Studio</p>
  <p><a href="../">All games</a> · <a href="../privacy.html">Privacy Policy</a> · <a href="../terms.html">Terms</a></p>
</footer>

<script>
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {{
    document.querySelectorAll('video[autoplay]').forEach(function (v) {{
      v.removeAttribute('autoplay'); v.pause(); v.controls = true;
    }});
  }}
</script>

</body>
</html>
'''


def human_size(n):
    return '{:.1f} MB'.format(n / 1e6) if n >= 1e6 else '{} KB'.format(max(1, round(n / 1e3)))


def build(game, apps, gp_live):
    e = html.escape
    slug = game['slug']
    page = game.get('page') or slug
    aso = game.get('aso') or page
    texts = load_texts(aso)
    a = apps.get(game.get('appstore_id'))
    name = (a or {}).get('trackName') or texts['name'] or game.get('name') or slug
    subtitle = texts['subtitle'] or ''
    gp = game.get('google_play') if gp_live.get(slug, True) else None
    out_dir = os.path.join(ROOT, page)
    media = os.path.join(out_dir, 'media')
    changed = []

    # sources of media: URLs carry a content hash, so the same URL means the same image
    if a:
        icon_url = re.sub(r'/\d+x\d+bb\.(png|jpg)$', '/1024x1024bb.png', a['artworkUrl512'])
        shot_urls = [full_size(u) for u in a.get('screenshotUrls', [])][:MAX_SHOTS]
    else:
        play_icon, play_shots = play_media(game['google_play'])
        icon_url = play_icon + '=s1024'
        shot_urls = [u + '=s0' for u in play_shots][:MAX_SHOTS]
    video = (game.get('press') or {}).get('video')
    video_path = os.path.expanduser(video) if video else None
    has_video = bool(video_path and os.path.exists(video_path))

    # trailer: local video, else a YouTube link from the Play texts
    trailer_html, nav_trailer = '', ''
    if has_video:
        if not os.path.exists(os.path.join(media, 'trailer.mp4')) or \
                os.path.getmtime(os.path.join(media, 'trailer.mp4')) < os.path.getmtime(video_path):
            make_trailer(video_path, media)
            changed.append('trailer')
        trailer_html = """
<section id="trailer">
  <div class="inner">
    <span class="section-tag">// In motion</span>
    <h2>Trailer</h2>
    <div class="separator"></div>
    <div class="trailer"><video src="media/trailer.mp4" poster="media/trailer.jpg" autoplay muted loop playsinline preload="metadata"></video></div>{yt}
  </div>
</section>
"""
        nav_trailer = '    <li><a href="#trailer">Trailer</a></li>\n'
    elif texts['youtube']:
        trailer_html = """
<section id="trailer">
  <div class="inner">
    <span class="section-tag">// In motion</span>
    <h2>Trailer</h2>
    <div class="separator"></div>
    <a class="btn-outline" href="{}" target="_blank" rel="noopener">Watch the trailer on YouTube</a>
  </div>
</section>
""".format(e(texts['youtube']))
        nav_trailer = '    <li><a href="#trailer">Trailer</a></li>\n'
    trailer_html = trailer_html.replace('{yt}', '\n    <p class="note"><a href="{}" target="_blank" rel="noopener">Watch it on YouTube</a></p>'.format(
        e(texts['youtube'])) if texts['youtube'] else '')

    # facts
    platforms = []
    if a:
        devices = ' '.join(a.get('supportedDevices', []))
        platforms.append('iPhone and iPad (iOS {}+)'.format(a.get('minimumOsVersion', '').split('.')[0]) if 'iPad' in devices else 'iPhone')
        if 'MacDesktop' in devices:
            platforms.append('Mac')
    if gp:
        platforms.append('Android')
    facts = [('Title', e(name)), ('Developer', DEVELOPER), ('Genre', e(game.get('genre', ''))),
             ('Platforms', ', '.join(platforms))]
    if a:
        facts.append(('Released', dt.date.fromisoformat(a['releaseDate'][:10]).strftime('%B %Y')))
        facts.append(('Price', 'Free to download' if a.get('price', 0) == 0 else e(a.get('formattedPrice', ''))))
        facts.append(('Age rating', e(a.get('contentAdvisoryRating', ''))))
    else:
        facts.append(('Price', 'Free to download'))
    facts.append(('Contact', '<a href="mailto:{0}">{0}</a>'.format(CONTACT)))
    facts.append(('Privacy', '<a href="../privacy.html">Privacy policy</a>'))
    facts_html = ''.join('        <tr><th>{}</th><td>{}</td></tr>\n'.format(k, v) for k, v in facts if v)

    # store badges
    badges = ''
    if a:
        badges += ('        <a class="badge" href="https://apps.apple.com/app/id{}" target="_blank" rel="noopener">\n'
                   '          <img src="../assets/badges/app-store.svg" alt="Download on the App Store" />\n'
                   '          <small>{}</small>\n        </a>\n').format(a['trackId'], ' · '.join(
                       p.split(' (')[0].replace(' and ', ' · ') for p in platforms if p != 'Android'))
    if gp:
        badges += ('        <a class="badge" href="https://play.google.com/store/apps/details?id={}" target="_blank" rel="noopener">\n'
                   '          <img src="../assets/badges/google-play.svg" alt="Get it on Google Play" />\n'
                   '          <small>Android</small>\n        </a>\n').format(e(gp))

    # fact sheet text for the press kit
    links = ['Press page:  https://impression-team.github.io/{}/'.format(page)]
    if a:
        links.append('App Store:   https://apps.apple.com/app/id{}'.format(a['trackId']))
    if gp:
        links.append('Google Play: https://play.google.com/store/apps/details?id={}'.format(gp))
    if texts['youtube']:
        links.append('Trailer:     {}'.format(texts['youtube']))
    links += ['Privacy:     https://impression-team.github.io/privacy.html', 'Contact:     ' + CONTACT]
    sheet = '\n'.join([name.upper(), subtitle, '',
                       '\n'.join('{}: {}'.format(k, re.sub(r'<[^>]+>', '', v)) for k, v in facts if v and k not in ('Contact', 'Privacy')),
                       '', 'ABOUT', plain_paragraphs(texts['description']), '', 'LINKS', '\n'.join(links), '',
                       'IN THIS KIT', 'icon-1024.png          app icon, 1024 x 1024',
                       'screenshots/           {} screenshots, full size'.format(len(shot_urls))]
                      + (['key-frame.jpg          a frame from the trailer'] if has_video else [])) + '\n'

    # media and press kit: rebuilt only when a source or the fact sheet changed. Store CDNs re-encode
    # images on every download, so comparing bytes alone would rewrite the files on every run.
    manifest_path = os.path.join(media, '.sources.json')
    sources = {'icon': icon_url, 'shots': shot_urls,
               'video': '{}@{}'.format(video, int(os.path.getmtime(video_path))) if has_video else None,
               'sheet': hashlib.sha256(sheet.encode()).hexdigest()}
    old = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    kit_path = os.path.join(out_dir, 'presskit.zip')
    files_ok = os.path.exists(kit_path) and os.path.exists(os.path.join(media, 'icon-512.png')) and all(
        os.path.exists(os.path.join(media, 'screen-{}.jpg'.format(i))) for i in range(1, len(shot_urls) + 1))
    if old.get('sources') == sources and files_ok:
        dims = old['dims']
    else:
        icon_src = get(icon_url)
        if write_if_changed(os.path.join(media, 'icon-512.png'), sips_png(icon_src, 512)):
            changed.append('icon')
        full_shots, dims = [], []
        for i, url in enumerate(shot_urls, 1):
            original = get(url)
            full, w, h = sips_jpeg(original, quality=90)
            small, sw, sh = sips_jpeg(original, max_px=PAGE_SHOT_PX, quality=80)
            full_shots.append(('screenshot-{:02d}.jpg'.format(i), full))
            dims.append([w, h, sw, sh])
            if write_if_changed(os.path.join(media, 'screen-{}.jpg'.format(i)), small):
                changed.append('screen-{}'.format(i))
        for stale in glob.glob(os.path.join(media, 'screen-*.jpg')):
            if int(re.search(r'screen-(\d+)', stale).group(1)) > len(shot_urls):
                os.remove(stale)
        key_frame = make_key_frame(video_path) if has_video else None
        folder = re.sub(r'[^A-Za-z0-9]+', '-', name.split(':')[0]).strip('-') + '-press-kit'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            def add(arc, data):
                info = zipfile.ZipInfo('{}/{}'.format(folder, arc), ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, data)
            add('icon-1024.png', sips_png(icon_src, 1024))
            for fname, data in full_shots:
                add('screenshots/' + fname, data)
            if key_frame:
                add('key-frame.jpg', key_frame)
            add('fact-sheet.txt', sheet)
        if write_if_changed(kit_path, buf.getvalue()):
            changed.append('presskit')
        write_if_changed(manifest_path, json.dumps({'sources': sources, 'dims': dims}, indent=1) + '\n')

    landscape = bool(dims) and dims[0][2] > dims[0][3]
    kit_items = ['App icon, 1024 × 1024',
                 '{} screenshots{}'.format(len(dims), ', {} × {}'.format(dims[0][0], dims[0][1]) if dims else '')]
    if has_video:
        kit_items.append('A key frame from the trailer')
    kit_items.append('Fact sheet and description (text)')

    shots_html = ''.join(
        '      <a href="media/screen-{0}.jpg" target="_blank"><img src="media/screen-{0}.jpg" alt="{1} screenshot {0}" '
        'width="{2}" height="{3}" loading="lazy" /></a>\n'.format(i, e(name), d[2], d[3]) for i, d in enumerate(dims, 1))
    lead = '      <p class="lead">{}</p>\n'.format(e(texts['lead'])) if texts['lead'] else ''
    page_html = PAGE.format(
        name=e(name), subtitle=e(subtitle), page=page, meta_desc=e(plain_paragraphs(texts['description'], 1)[:300]),
        og_image='trailer.jpg' if has_video else 'screen-1.jpg', nav_trailer=nav_trailer, badges=badges, lead=lead,
        description=description_html(texts['description']), trailer=trailer_html,
        shots_class=' landscape' if landscape else '', shots=shots_html, facts=facts_html,
        kit_items=''.join('          <li>{}</li>\n'.format(i) for i in kit_items),
        kit_size=human_size(os.path.getsize(kit_path)))
    if write_if_changed(os.path.join(out_dir, 'index.html'), page_html):
        changed.append('page')
    return page, changed


def main():
    config = json.load(open(os.path.join(HERE, 'games.json'), encoding='utf-8'))
    ratings = json.load(open(os.path.join(HERE, 'ratings.json'), encoding='utf-8'))
    gp_live = {k: v.get('gp_live', True) for k, v in ratings.items()}
    only = set(sys.argv[1:])
    games = [g for g in config['games'] if g.get('press_page') != 'custom' and (not only or g['slug'] in only)]
    apps = appstore_lookup([g['appstore_id'] for g in games if g.get('appstore_id')])
    for g in games:
        try:
            page, changed = build(g, apps, gp_live)
            print('  {:28} /{}/  {}'.format(g['slug'], page, ', '.join(changed) or 'unchanged'))
        except Exception as err:
            print('  ! {}: {}'.format(g['slug'], err), file=sys.stderr)


if __name__ == '__main__':
    main()
