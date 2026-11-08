"""Steam collector.

Sources (public, no key):
  - api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/   top 100 by CCU (+ last week's rank)
  - store.steampowered.com/api/featuredcategories                      top sellers / new releases
  - store.steampowered.com/search/results/?filter=popularnew          popular new releases (paged)
  - steamspy.com/api.php?request=top100in2weeks                        2-week top 100
  - api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1  live CCU, per app
  - store.steampowered.com/appreviews/<appid>?json=1                  total review count (sales proxy)
  - steamspy.com/api.php?request=appdetails&appid=                     tags (fetched once per game)
  - store.steampowered.com/api/appdetails?appids=                      release date (once per game)
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .net import try_get

MOST_PLAYED = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"
FEATURED = "https://store.steampowered.com/api/featuredcategories?cc=us&l=english"
SEARCH = ("https://store.steampowered.com/search/results/?query&start={start}&count=50"
          "&filter={filter}&infinite=1&cc=us&l=english&category1=998")  # 998 = games only
SPY_TOP = "https://steamspy.com/api.php?request=top100in2weeks"
SPY_APP = "https://steamspy.com/api.php?request=appdetails&appid={appid}"
CCU = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}"
REVIEWS = ("https://store.steampowered.com/appreviews/{appid}?json=1&language=all"
           "&purchase_type=all&num_per_page=0&filter=recent")
STORE_APP = ("https://store.steampowered.com/api/appdetails?appids={appid}"
             "&filters=basic,release_date,genres&cc=us&l=english")

APPID_RE = re.compile(r'data-ds-appid="(\d+)"')
TITLE_RE = re.compile(r'<span class="title">(.*?)</span>')


def _discover(con, ts):
    found = 0

    def hit(appid, name, source, pos):
        nonlocal found
        db.upsert_game(con, "steam", appid, ts, name=name,
                       url=f"https://store.steampowered.com/app/{appid}")
        db.add_hit(con, "steam", appid, ts, source, pos)
        found += 1

    mp = try_get(MOST_PLAYED)
    for r in ((mp or {}).get("response") or {}).get("ranks", []):
        hit(r["appid"], None, "steam:most-played", r.get("rank"))

    fc = try_get(FEATURED) or {}
    for key in ("top_sellers", "new_releases"):
        for pos, it in enumerate((fc.get(key) or {}).get("items", [])):
            hit(it["id"], it.get("name"), f"steam:{key}", pos)

    for flt in ("popularnew",):
        for page in range(3):
            res = try_get(SEARCH.format(start=page * 50, filter=flt))
            html = (res or {}).get("results_html", "")
            ids, titles = APPID_RE.findall(html), TITLE_RE.findall(html)
            for pos, appid in enumerate(ids):
                name = titles[pos] if pos < len(titles) else None
                hit(appid, name, f"steam:{flt}", page * 50 + pos)
            if len(ids) < 50:
                break

    spy = try_get(SPY_TOP) or {}
    for pos, (appid, g) in enumerate(spy.items()):
        hit(appid, g.get("name"), "steam:spy-2weeks", pos)
    return found


def _fetch_meta(con, cfg):
    """Tags + release date for games we haven't described yet (capped per run; SteamSpy ~1 req/s)."""
    rows = con.execute(
        "SELECT game_id FROM games WHERE platform='steam' AND meta_done=0 "
        "ORDER BY last_seen DESC LIMIT ?", (cfg["steam_meta_per_run"],)
    ).fetchall()
    for r in rows:
        appid = r["game_id"]
        spy = try_get(SPY_APP.format(appid=appid)) or {}
        tags = spy.get("tags") or {}
        tag_list = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])][:8] \
            if isinstance(tags, dict) else []
        store = (try_get(STORE_APP.format(appid=appid)) or {}).get(appid, {}).get("data") or {}
        if store and store.get("type") not in (None, "game"):
            # DLC / soundtrack / tool slipped in: stop tracking it.
            con.execute("UPDATE games SET meta_done=1, last_seen=0 WHERE platform='steam' "
                        "AND game_id=?", (appid,))
            continue
        db.upsert_game(
            con, "steam", appid, 0, seen=False,
            name=store.get("name") or spy.get("name"),
            tags=tag_list, genre=tag_list[0] if tag_list else None,
            created=(store.get("release_date") or {}).get("date"),
            meta_done=1,
        )
        time.sleep(1.0)


def _ccu(appid):
    d = try_get(CCU.format(appid=appid), retries=2, quiet=True) or {}  # 404 = no stats
    resp = d.get("response") or {}
    return appid, resp.get("player_count") if resp.get("result") == 1 else None


def _reviews(appid):
    d = try_get(REVIEWS.format(appid=appid), retries=2, quiet=True) or {}
    return appid, (d.get("query_summary") or {}).get("total_reviews")


def collect(con, ts, cfg):
    found = _discover(con, ts)
    for appid in cfg.get("watchlist", {}).get("steam", []):
        db.upsert_game(con, "steam", appid, ts)
    print(f"  steam: {found} chart/search hits")

    _fetch_meta(con, cfg)

    ids = db.tracked_ids(con, "steam", ts, cfg["keep_tracking_days"], cfg["steam_cap"])
    with ThreadPoolExecutor(max_workers=8) as ex:
        ccu = dict(ex.map(_ccu, ids))

    # Review counts move slowly. Each game is refreshed every N hours, staggered so
    # each hourly run only asks the store about 1/N of the games, one at a time.
    every = cfg["steam_reviews_every_h"]
    slot = (ts // 3600) % every
    due = [a for a in ids if int(a) % every == slot]
    reviews, rev_fail = {}, 0
    for appid in due:
        _, total = _reviews(appid)
        if total is None:
            rev_fail += 1
        else:
            reviews[appid] = total
        time.sleep(1.2)

    n = 0
    for appid in ids:
        if ccu.get(appid) is None and reviews.get(appid) is None:
            continue
        db.add_snapshot(con, "steam", appid, ts, ccu=ccu.get(appid), reviews=reviews.get(appid))
        n += 1
    print(f"  steam: snapshotted {n}/{len(ids)} tracked games; "
          f"reviews refreshed {len(reviews)}/{len(due)}"
          + (f" ({rev_fail} throttled/empty)" if rev_fail else ""))
    return n
