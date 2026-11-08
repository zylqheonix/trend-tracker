# trend-tracker

Finds Roblox and Steam games that are **growing**, not the ones that are already big.
Takes one snapshot of public data a day, stores it in SQLite, ranks games and genres by daily and weekly growth, and publishes a dashboard website.
Python stdlib only — nothing to install.

## How it decides what's trending

| Signal | Roblox | Steam |
|---|---|---|
| Player growth | players now vs. the snapshot 1 day / 7 days earlier, same time of day | same |
| Inflow acceleration | visits/day now vs. 1 / 7 days earlier | new reviews/day (sales proxy) |
| Newness bonus | created ≤30d / ≤90d | released ≤30d / ≤90d |
| Entry bonus | newly appeared in Top Trending / Up-and-Coming | newly in Popular New Releases |

Growth is log-scaled with a pseudo-count, so a game going 5 → 40 players doesn't outrank 2k → 9k.
Games without enough history for the full window are scored but marked **partial** and discounted.
Genres (Roblox) and tags (Steam) get a momentum score: total CCU growth plus how many games in them are rising, weighted toward new ones.

Output lists: `breakout` (≤60 days old), `rising` (growing, not huge), `big_movers`, `falling`, `genres`.

**The daily view needs 2 days of snapshots; the weekly view needs 8.**

## Run it

```bash
python run.py all                 # collect a snapshot + write public/ (dashboard, json, digest)
python serve.py                   # dashboard + API on http://localhost:8787
python -m unittest tests/test_scoring.py
```

Local API:
- `GET /api/trends/roblox?window=24&list=breakout`
- `GET /api/genres/steam?window=168`
- `GET /api/game/roblox/<universeId>` (full history)
- `GET /api/digest`

## Run it daily for free (recommended): GitHub Actions

1. Push this folder to a new GitHub repo.
2. Settings → Pages → Source: **GitHub Actions**.
3. Optional: Settings → Secrets → Actions → add `DISCORD_WEBHOOK` to get the digest after each run.
4. Actions tab → trend-tracker → **Run workflow** once to start it. After that it runs daily at ~7–8am Pacific.

History is kept on a `data` branch (force-pushed each run, so the repo stays small).
Your dashboard is then at `https://<you>.github.io/<repo>/`, with the raw JSON beside it:
`roblox_24h.json`, `steam_24h.json`, `roblox_168h.json`, `steam_168h.json`, `digest_24h.md`.

A private repo works too (stays within the free Actions minutes), but Pages on a private repo needs a paid GitHub plan; use the local API instead.

## Data sources (all public, no keys)

- Roblox: `apis.roblox.com/explore-api/v1/get-sorts` (Discover sorts, **undocumented**, can change), `games.roblox.com/v1/games` and `/games/votes`.
- Steam: `ISteamChartsService/GetMostPlayedGames`, store `featuredcategories`, store search `filter=popularnew`, `ISteamUserStats/GetNumberOfCurrentPlayers`, store `appreviews`, store `appdetails`, SteamSpy `top100in2weeks` + `appdetails` (tags).

If a source breaks, that platform logs the error and the other keeps working.

## Tuning

`config.json`: how long to keep tracking a game after it leaves the charts, caps on tracked games, how often to refresh Steam reviews, and a `watchlist` of IDs to always track (put competitors and your own games here).
