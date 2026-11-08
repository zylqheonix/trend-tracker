"""SQLite storage: one row per game, one row per (game, hourly snapshot)."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    platform   TEXT NOT NULL,          -- 'roblox' | 'steam'
    game_id    TEXT NOT NULL,          -- universeId / appid
    name       TEXT,
    genre      TEXT,                   -- roblox genre_l1 / steam primary tag
    subgenre   TEXT,                   -- roblox genre_l2
    tags       TEXT,                   -- JSON list (steam tags)
    created    TEXT,                   -- ISO date (roblox created / steam release)
    url        TEXT,
    first_seen INTEGER,
    last_seen  INTEGER,                -- last time it appeared in a chart/sort/pool source
    meta_done  INTEGER DEFAULT 0,      -- steam: tags/release date fetched
    PRIMARY KEY (platform, game_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    platform  TEXT NOT NULL,
    game_id   TEXT NOT NULL,
    ts        INTEGER NOT NULL,        -- unix seconds, rounded to the hour
    ccu       INTEGER,
    visits    INTEGER,                 -- roblox cumulative visits
    favorites INTEGER,
    reviews   INTEGER,                 -- steam cumulative review count
    upvotes   INTEGER,
    downvotes INTEGER,
    PRIMARY KEY (platform, game_id, ts)
);
CREATE TABLE IF NOT EXISTS sort_hits (
    platform TEXT NOT NULL,
    game_id  TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    source   TEXT NOT NULL,            -- e.g. 'up-and-coming', 'steam:popularnew'
    position INTEGER,
    PRIMARY KEY (platform, game_id, ts, source)
);
CREATE INDEX IF NOT EXISTS snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS hits_ts ON sort_hits(ts);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def hour_now():
    return int(time.time()) // 3600 * 3600


def upsert_game(con, platform, game_id, ts, **fields):
    """Insert a game or update the given fields. Always bumps last_seen when seen=True."""
    seen = fields.pop("seen", True)
    game_id = str(game_id)
    row = con.execute(
        "SELECT 1 FROM games WHERE platform=? AND game_id=?", (platform, game_id)
    ).fetchone()
    if "tags" in fields and not isinstance(fields["tags"], (str, type(None))):
        fields["tags"] = json.dumps(fields["tags"])
    if row is None:
        cols = ["platform", "game_id", "first_seen", "last_seen"] + list(fields)
        vals = [platform, game_id, ts, ts if seen else None] + list(fields.values())
        con.execute(
            f"INSERT INTO games ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
    else:
        sets = {k: v for k, v in fields.items() if v is not None}
        if seen:
            sets["last_seen"] = ts
        if sets:
            con.execute(
                f"UPDATE games SET {', '.join(f'{k}=?' for k in sets)} "
                "WHERE platform=? AND game_id=?",
                list(sets.values()) + [platform, game_id],
            )


def add_snapshot(con, platform, game_id, ts, **metrics):
    cols = ["platform", "game_id", "ts"] + list(metrics)
    con.execute(
        f"INSERT OR REPLACE INTO snapshots ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [platform, str(game_id), ts] + list(metrics.values()),
    )


def add_hit(con, platform, game_id, ts, source, position):
    con.execute(
        "INSERT OR REPLACE INTO sort_hits VALUES (?,?,?,?,?)",
        (platform, str(game_id), ts, source, position),
    )


def tracked_ids(con, platform, now, keep_days, cap):
    """Games seen in any source within keep_days, newest first, capped."""
    rows = con.execute(
        "SELECT game_id FROM games WHERE platform=? AND last_seen >= ? "
        "ORDER BY last_seen DESC LIMIT ?",
        (platform, now - keep_days * 86400, cap),
    ).fetchall()
    return [r["game_id"] for r in rows]


def prune(con, now, keep_days):
    cutoff = now - keep_days * 86400
    con.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM sort_hits WHERE ts < ?", (cutoff,))
