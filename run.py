#!/usr/bin/env python3
"""Trend tracker CLI.

  python run.py collect            # pull one snapshot from Roblox + Steam
  python run.py score              # rank by growth, write public/ (json, digest, dashboard)
  python run.py all                # collect then score (what the daily job runs)
  python run.py score --window 168 # 7-day trends instead of 24h

Env:
  TRACKER_DB        path to sqlite db (default data/trends.db)
  DISCORD_WEBHOOK   if set, the digest is posted there (only when --notify is passed)
"""
import argparse
import json
import os
import shutil
import sys

from tracker import db, report, roblox, scoring, steam

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_cfg():
    with open(os.path.join(ROOT, "config.json")) as f:
        return json.load(f)


def write_site(con, now, platforms, all_res, out):
    """Per-game history for sparklines + meta, and copy the dashboard page in."""
    for p in platforms:
        ids = set()
        for res in all_res.values():
            for key in ("breakout", "rising", "big_movers", "falling"):
                ids.update(g["id"] for g in res[p][key])
        hist = scoring.histories(con, p, ids, now - 30 * 86400)
        with open(os.path.join(out, f"history_{p}.json"), "w") as f:
            json.dump(hist, f, separators=(",", ":"))
    tracked = {p: con.execute("SELECT COUNT(DISTINCT game_id) FROM snapshots WHERE platform=? "
                              "AND ts=(SELECT MAX(ts) FROM snapshots WHERE platform=?)",
                              (p, p)).fetchone()[0] for p in platforms}
    days = {p: con.execute("SELECT COUNT(DISTINCT ts/86400) FROM snapshots WHERE platform=?",
                           (p,)).fetchone()[0] for p in platforms}
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump({"generated_at": now, "tracked": tracked, "days_of_data": days}, f)
    shutil.copy(os.path.join(ROOT, "site", "index.html"), os.path.join(out, "index.html"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "score", "all"])
    ap.add_argument("--window", type=int, default=24, help="hours (24 or 168)")
    ap.add_argument("--only", choices=["roblox", "steam"])
    ap.add_argument("--notify", action="store_true", help="post digest to DISCORD_WEBHOOK")
    ap.add_argument("--out", default=os.path.join(ROOT, "public"))
    args = ap.parse_args()

    cfg = load_cfg()
    db_path = os.environ.get("TRACKER_DB", os.path.join(ROOT, "data", "trends.db"))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = db.connect(db_path)
    now = db.hour_now()
    platforms = [args.only] if args.only else ["roblox", "steam"]

    if args.cmd in ("collect", "all"):
        print(f"collect @ {now}")
        ok = 0
        for p in platforms:
            try:
                ok += (roblox if p == "roblox" else steam).collect(con, now, cfg)
                con.commit()
            except Exception as e:  # one platform failing shouldn't kill the other
                print(f"  ! {p} collection failed: {e}", file=sys.stderr)
        db.prune(con, now, cfg["history_days"])
        con.commit()
        if ok == 0:
            print("nothing collected (network blocked or APIs changed)", file=sys.stderr)
            sys.exit(2)

    if args.cmd in ("score", "all"):
        results = {p: scoring.rank(con, p, now, args.window) for p in platforms}
        md = report.write_outputs(results, now, args.window, args.out)
        all_res = {args.window: results}
        if args.window == 24:  # also refresh the 7-day view
            all_res[168] = {p: scoring.rank(con, p, now, 168) for p in platforms}
            report.write_outputs(all_res[168], now, 168, args.out)
        write_site(con, now, platforms, all_res, args.out)
        print(md)
        hook = os.environ.get("DISCORD_WEBHOOK")
        if args.notify and hook:
            report.push_discord(md, hook)


if __name__ == "__main__":
    main()
