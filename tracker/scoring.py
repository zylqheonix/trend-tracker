"""Trend scoring. Ranks games (and genres/tags) by *growth*, not by size.

Signals per game, over a window W (24h or 7d):
  ccu_growth   log2 change in concurrent players, now vs exactly W earlier.
               Comparing to the same time of day cancels the daily player cycle.
  flow_growth  acceleration of a cumulative counter:
               roblox -> visits/hour,  steam -> new reviews/hour (sales proxy).
  gain         absolute CCU added (small weight, so +5 -> +40 doesn't beat +2k -> +9k)
  newness      bonus for games created/released recently (catchable trends)
  entered      bonus for newly appearing in a trending/new sort during the window

All growth is log2 with a pseudo-count, so tiny games don't produce +900% noise.
"""
import json
import math
from datetime import datetime, timezone

H = 3600

PLATFORM = {
    "roblox": dict(k_ccu=50, k_flow=200.0, flow="visits", flow_span_h=24,
                   min_ccu=100, big_ccu=50_000, entry_sorts=("top-trending", "up-and-coming")),
    "steam": dict(k_ccu=20, k_flow=0.2, flow="reviews", flow_span_h=24,
                  min_ccu=50, big_ccu=20_000, entry_sorts=("steam:popularnew", "steam:new_releases")),
}

TOL = {24: 6, 168: 12}  # hours of slack when matching snapshots (cron drift)


def lg(a, b, k):
    return math.log2((a + k) / (b + k))


def parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def age_days(created, now_ts):
    d = parse_date(created)
    return None if d is None else max(0.0, (now_ts - d.timestamp()) / 86400)


def nearest(series, field, t, tol):
    best = None
    for r in series:
        if r[field] is None or abs(r["ts"] - t) > tol:
            continue
        if best is None or abs(r["ts"] - t) < abs(best["ts"] - t):
            best = r
    return best


def rate(series, field, t_end, span_h, tol):
    """Average per-hour increase of a cumulative counter over [t_end - span, t_end]."""
    b = nearest(series, field, t_end, tol)
    a = nearest(series, field, t_end - span_h * H, tol)
    if not a or not b or b["ts"] - a["ts"] < H:
        return None
    return max(0.0, (b[field] - a[field]) / ((b["ts"] - a["ts"]) / H))


def score_game(series, platform, now, window_h, created=None, entered=False):
    """Return a metrics dict for one game, or None if there isn't enough data.

    Works with daily or hourly snapshots: "now" is the latest snapshot, and the
    comparison point is whichever snapshot is nearest to exactly W hours before it
    (within a tolerance that absorbs scheduler drift). Same time of day on both
    sides, so the daily player cycle cancels out.
    """
    p = PLATFORM[platform]
    tol = TOL.get(window_h, max(6, window_h // 8)) * H
    pts = [r for r in series if r["ccu"] is not None]
    if not pts:
        return None
    latest = max(pts, key=lambda r: r["ts"])
    if latest["ts"] < now - tol or latest["ccu"] < p["min_ccu"]:
        return None  # stale (dropped out of tracking) or too small
    ref, ccu_now = latest["ts"], latest["ccu"]

    prev = nearest(pts, "ccu", ref - window_h * H, tol)
    confidence = "full"
    if prev is not None:
        ccu_prev, span_h = prev["ccu"], (ref - prev["ts"]) / H
    else:
        # Not enough history for a full window yet (e.g. weekly view in week one):
        # compare against the oldest point we have, flagged and discounted.
        old = [r for r in pts if r["ts"] <= ref - 6 * H]
        if not old:
            return None
        first = min(old, key=lambda r: r["ts"])
        ccu_prev, span_h = first["ccu"], (ref - first["ts"]) / H
        confidence = "partial"

    ccu_g = lg(ccu_now, ccu_prev, p["k_ccu"])
    if confidence == "partial":
        # Extrapolating a short span is noisy: scale up gently (sqrt, max 2x) and
        # discount, so a partial read never outranks a confirmed riser by default.
        ccu_g *= min(math.sqrt(window_h / max(span_h, 1)), 2.0) * 0.8

    # Inflow per hour over the last day vs the day ending W earlier.
    f, fspan = p["flow"], p["flow_span_h"]
    ftol = max(2, fspan // 3) * H
    flow_now = rate(series, f, ref, fspan, ftol)
    flow_prev = rate(series, f, ref - window_h * H, fspan, ftol)
    flow_g = (lg(flow_now, flow_prev, p["k_flow"])
              if flow_now is not None and flow_prev is not None else None)

    gain = ccu_now - ccu_prev
    age = age_days(created, now)
    newness = 0.6 if age is not None and age <= 30 else 0.3 if age is not None and age <= 90 else 0.0

    score = ccu_g + (0.7 * flow_g if flow_g is not None else 0.0)
    score += 0.25 * math.log10(max(gain, 1.0))
    score += newness + (0.3 if entered else 0.0)

    return {
        "score": round(score, 3),
        "ccu_now": round(ccu_now),
        "ccu_prev": round(ccu_prev),
        "ccu_growth_pct": round(((ccu_now + p["k_ccu"]) / (ccu_prev + p["k_ccu"]) - 1) * 100, 1),
        "flow_now_per_day": None if flow_now is None else round(flow_now * 24),
        "flow_growth_pct": None if flow_g is None else round((2 ** flow_g - 1) * 100, 1),
        "age_days": None if age is None else round(age),
        "entered_trending": entered,
        "confidence": confidence,
        "span_h": round(span_h, 1),
    }


def _load_series(con, platform, since):
    rows = con.execute(
        "SELECT game_id, ts, ccu, visits, reviews FROM snapshots "
        "WHERE platform=? AND ts>=? ORDER BY ts", (platform, since)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["game_id"], []).append(dict(r))
    return out


def _entered(con, platform, now, window_h, sorts):
    """Games whose first appearance in an entry sort falls inside the window."""
    q = ("SELECT game_id, MIN(ts) AS first FROM sort_hits WHERE platform=? AND source IN (%s) "
         "GROUP BY game_id" % ",".join("?" * len(sorts)))
    rows = con.execute(q, (platform, *sorts)).fetchall()
    return {r["game_id"] for r in rows if r["first"] > now - window_h * H}


def rank(con, platform, now, window_h=24):
    p = PLATFORM[platform]
    series = _load_series(con, platform, now - (window_h + 72) * H)
    meta = {r["game_id"]: dict(r) for r in con.execute(
        "SELECT * FROM games WHERE platform=?", (platform,)).fetchall()}
    entered = _entered(con, platform, now, window_h, p["entry_sorts"])

    games = []
    for gid, s in series.items():
        m = meta.get(gid, {})
        res = score_game(s, platform, now, window_h, m.get("created"), gid in entered)
        if res is None:
            continue
        tags = json.loads(m["tags"]) if m.get("tags") else []
        res.update(id=gid, name=m.get("name"), url=m.get("url"),
                   genre=m.get("subgenre") or m.get("genre"), tags=tags[:5])
        games.append(res)
    games.sort(key=lambda g: g["score"], reverse=True)

    big = p["big_ccu"]
    growing = [g for g in games if g["ccu_growth_pct"] >= 20 or (g["flow_growth_pct"] or 0) >= 50]
    return {
        "breakout": [g for g in growing if g["age_days"] is not None and g["age_days"] <= 60][:25],
        "rising": [g for g in growing if g["ccu_now"] < big][:40],
        "big_movers": [g for g in games if g["ccu_now"] >= big and g["ccu_growth_pct"] >= 10][:15],
        "falling": sorted([g for g in games if g["ccu_growth_pct"] <= -30],
                          key=lambda g: g["ccu_growth_pct"])[:15],
        "genres": genre_trends(games, platform),
        "n_scored": len(games),
    }


def genre_trends(games, platform):
    """Aggregate momentum per genre (roblox) or per tag (steam, top-3 tags)."""
    k = PLATFORM[platform]["k_ccu"]
    groups = {}
    for g in games:
        if g["confidence"] != "full":
            continue
        keys = g["tags"][:3] if platform == "steam" else [g["genre"]]
        for key in filter(None, keys):
            groups.setdefault(key, []).append(g)

    out = []
    for key, gs in groups.items():
        if len(gs) < 3:
            continue
        now_sum = sum(g["ccu_now"] for g in gs)
        prev_sum = sum(g["ccu_prev"] for g in gs)
        rising = [g for g in gs if g["ccu_growth_pct"] >= 40]
        young_rising = [g for g in rising if g["age_days"] is not None and g["age_days"] <= 60]
        growth = lg(now_sum, prev_sum, k * len(gs))
        momentum = growth + 0.35 * math.log2(1 + len(rising)) + 0.25 * len(young_rising)
        out.append({
            "genre": key,
            "momentum": round(momentum, 3),
            "ccu_growth_pct": round((2 ** growth - 1) * 100, 1),
            "games": len(gs),
            "rising": len(rising),
            "young_rising": len(young_rising),
            "top": [g["name"] for g in sorted(gs, key=lambda x: -x["score"])[:3]],
        })
    out.sort(key=lambda x: x["momentum"], reverse=True)
    return out[:20]


def histories(con, platform, ids, since):
    """{game_id: [[ts, ccu], ...]} for the given games, for dashboard sparklines."""
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("SELECT game_id, ts, ccu FROM snapshots WHERE platform=? AND ts>=? AND ccu IS NOT NULL "
             "AND game_id IN (%s) ORDER BY ts" % ",".join("?" * len(chunk)))
        for r in con.execute(q, (platform, since, *chunk)).fetchall():
            out.setdefault(r["game_id"], []).append([r["ts"], r["ccu"]])
    return out
