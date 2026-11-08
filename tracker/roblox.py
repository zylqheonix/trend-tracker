"""Roblox collector.

Sources (public, no key):
  - apis.roblox.com/explore-api/v1/get-sorts      Discover sorts (Top Trending, Up-and-Coming, ...)
      Undocumented: may change without notice. We only use it to *find* candidates.
  - games.roblox.com/v1/games?universeIds=...     live players, visits, favorites, created, genre
  (votes come from the sort entries; no separate votes call)
"""
import time
import urllib.parse
import uuid

from . import db
from .net import try_get

SORTS_URL = "https://apis.roblox.com/explore-api/v1/get-sorts"
GAMES_URL = "https://games.roblox.com/v1/games?universeIds={ids}"
BATCH = 50
PACE_S = 1.0  # pause between batched calls; games.roblox.com rate-limits bursts (429)


def fetch_sorts(max_pages=4):
    """Return {sort_id: [entry, ...]} for every Games sort on the Discover page."""
    session = str(uuid.uuid4())
    out, token, seen_tokens = {}, None, set()
    for _ in range(max_pages):
        params = {"sessionId": session, "device": "computer", "country": "all"}
        if token:
            params["sortsPageToken"] = token
        data = try_get(f"{SORTS_URL}?{urllib.parse.urlencode(params)}")
        if not data:
            break
        new = 0
        for sort in data.get("sorts", []):
            if sort.get("contentType") != "Games":
                continue
            sid = sort.get("sortId")
            if sid and sid not in out:
                out[sid] = sort.get("games", [])
                new += 1
        token = data.get("nextSortsPageToken")
        if not token or token in seen_tokens or new == 0:
            break
        seen_tokens.add(token)
    return out


def _batches(ids, n=BATCH):
    for i in range(0, len(ids), n):
        yield ids[i : i + n]


def collect(con, ts, cfg):
    sorts = fetch_sorts()
    print(f"  roblox: {len(sorts)} sorts -> {', '.join(sorts) or 'none'}")

    # 1) register everything that appears in a sort (this is the discovery step)
    for sid, entries in sorts.items():
        for pos, g in enumerate(entries):
            uid = g.get("universeId")
            if not uid:
                continue
            db.upsert_game(con, "roblox", uid, ts, name=g.get("name"))
            db.add_hit(con, "roblox", uid, ts, sid, pos)

    for uid in cfg.get("watchlist", {}).get("roblox", []):
        db.upsert_game(con, "roblox", uid, ts)

    # 2) snapshot every tracked game (recently seen), not just today's sort members,
    #    so we keep the trajectory after a game drops out of the sorts.
    ids = db.tracked_ids(con, "roblox", ts, cfg["keep_tracking_days"], cfg["roblox_cap"])
    # Votes come free with the sort entries; the separate votes endpoint isn't used by
    # scoring and doubled our request count (-> 429s), so it's skipped.
    votes = {str(g.get("universeId")): (g.get("totalUpVotes"), g.get("totalDownVotes"))
             for entries in sorts.values() for g in entries}

    n, failed = 0, []

    def fetch(chunk, quiet):
        data = try_get(GAMES_URL.format(ids=",".join(chunk)), quiet=quiet)
        time.sleep(PACE_S)
        return data

    pending = [(c, fetch(c, quiet=True)) for c in _batches(ids)]
    if any(d is None for _, d in pending):
        time.sleep(20)  # cool down, then retry rate-limited batches once
        pending = [(c, d if d is not None else fetch(c, quiet=False)) for c, d in pending]

    for chunk, data in pending:
        if data is None:
            failed.append(chunk)
        for g in (data or {}).get("data", []):
            uid = str(g["id"])
            db.upsert_game(
                con, "roblox", uid, ts, seen=False,
                name=g.get("name"),
                genre=g.get("genre_l1") or g.get("genre"),
                subgenre=g.get("genre_l2"),
                created=(g.get("created") or "")[:10] or None,
                url=f"https://www.roblox.com/games/{g.get('rootPlaceId')}",
            )
            up, down = votes.get(uid, (None, None))
            db.add_snapshot(
                con, "roblox", uid, ts,
                ccu=g.get("playing"), visits=g.get("visits"),
                favorites=g.get("favoritedCount"), upvotes=up, downvotes=down,
            )
            n += 1
    print(f"  roblox: snapshotted {n}/{len(ids)} tracked games"
          + (f" ({len(failed)} batch(es) still rate-limited)" if failed else ""))
    return n
