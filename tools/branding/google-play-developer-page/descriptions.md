# Google Play — developer page (Impression Team Studio)

Play Console → Store presence → Developer page. No API: fill in by hand.

## Files in this folder

| File | Field | Spec |
|---|---|---|
| `icon-512-variant-A-youtube-style.png` | Developer icon | 512 × 512, 32-bit PNG (with alpha) |
| `icon-512-variant-B-site-style.png` | Developer icon | 512 × 512, 32-bit PNG (with alpha) |
| `header-4096x2304-variant-A-youtube-style.jpg` | Header image | 4096 × 2304, JPEG, no alpha |
| `header-4096x2304-variant-B-site-style.jpg` | Header image | 4096 × 2304, JPEG, no alpha |

Pick one variant (A = the purple/grey style of the YouTube channel, B = the dark sci-fi style of the website) and
use the matching icon and header. Sources for re-rendering are in `source/` (headless Chrome, see below).

## Website

https://impression-team.github.io/

## Description (each ≤ 140 characters)

**English (en-US) — default**
Impression Team makes mobile games: cosy sims, puzzles, idle tycoons and arcade action. Free to play and crafted with care.

**Russian (ru-RU)**
Impression Team делает мобильные игры: уютные симуляторы, головоломки, idle-тайкуны и аркадный экшен. Бесплатно и с душой.

**Spanish, Latin America (es-419)**
Impression Team crea juegos móviles: simuladores acogedores, puzles, tycoons idle y acción arcade. Gratis y hechos con cariño.

**Spanish, Spain (es-ES)**
Impression Team crea juegos para móvil: simuladores acogedores, puzles, tycoons idle y acción arcade. Gratis y con mucho mimo.

**Portuguese, Brazil (pt-BR)**
Impression Team cria jogos para celular: simuladores aconchegantes, quebra-cabeças, tycoons idle e ação arcade. Grátis e feitos com carinho.

**German (de-DE)**
Impression Team macht Handyspiele: gemütliche Simulationen, Puzzles, Idle-Tycoons und Arcade-Action. Kostenlos und mit Liebe gemacht.

**French (fr-FR)**
Impression Team crée des jeux mobiles : simulations douillettes, puzzles, tycoons idle et action arcade. Gratuits et faits avec soin.

**Japanese (ja-JP)**
Impression Teamはモバイルゲームを作っています。癒やし系シミュレーション、パズル、放置系経営、アーケードアクション。無料で、心を込めて。

**Korean (ko-KR)**
Impression Team은 모바일 게임을 만듭니다: 아늑한 시뮬레이션, 퍼즐, 방치형 타이쿤, 아케이드 액션. 무료로, 정성을 담아.

**Chinese, Simplified (zh-CN)**
Impression Team 制作手机游戏：温馨模拟、益智解谜、放置经营和街机动作。免费畅玩，用心打造。

**Indonesian (id)**
Impression Team membuat game seluler: simulasi santai, puzzle, idle tycoon, dan aksi arkade. Gratis dan dibuat dengan sepenuh hati.

## Featured app (suggestion)

Music Band Tycoon: Idle Star (4.2 on Google Play, 56 ratings) — the best-rated game with many ratings.
Alternatives: Hero Hotel (4.6, 15 ratings), Antheim (newest, 1.3 coming in November).

## Re-rendering

```bash
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$C" --headless=new --hide-scrollbars --default-background-color=00000000 --force-device-scale-factor=1 \
  --virtual-time-budget=8000 --window-size=512,512 --screenshot=icon.png "file://$PWD/source/icon-a.html"
"$C" --headless=new --hide-scrollbars --allow-file-access-from-files --force-device-scale-factor=1 \
  --virtual-time-budget=8000 --window-size=4096,2304 --screenshot=header.png "file://$PWD/source/header-a.html"
sips -s format jpeg -s formatOptions 88 header.png --out header.jpg
```
The header sources point to the game icons in `<game>/media/icon-512.png` and to `source/brush.svg`
(the studio logo from `~/Youtube Channel/Impression Team/Logo/`).
