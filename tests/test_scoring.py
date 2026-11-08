"""Offline tests: synthetic hourly data -> check the ranking behaves like a trend detector."""
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tracker import db, report, scoring  # noqa: E402

H = 3600
NOW = 1_800_000_000 // H * H


def daily(t):
    """Player-count daily cycle: x0.6 at night .. x1.4 at peak."""
    return 1 + 0.4 * math.sin(2 * math.pi * (t % 86400) / 86400)


def build(con, gid, platform, ccu_fn, hours=72, created=None, genre=None, tags=None,
          reviews_fn=None, hits=(), step_h=1, jitter_h=0):
    db.upsert_game(con, platform, gid, NOW - hours * H, name=f"game-{gid}", genre=genre,
                   created=created, tags=tags, url=f"https://example/{gid}")
    visits = 1_000_000
    for i, h in enumerate(range(hours, -1, -step_h)):
        # jitter mimics a daily cron that fires a bit late on some days
        t = NOW - h * H + (jitter_h * H if i % 2 else 0) * (h > 0)
        ccu = int(ccu_fn(t) * daily(t))
        visits += ccu * 3 * step_h  # ~3 visits per concurrent player per hour
        every = 6 if step_h == 1 else 1
        rev = reviews_fn(t) if reviews_fn and (t // H // step_h) % every == 0 else None
        db.add_snapshot(con, platform, gid, t, ccu=ccu,
                        visits=visits if platform == "roblox" else None, reviews=rev)
    for src, first_t in hits:
        db.add_hit(con, platform, gid, first_t, src, 1)


def linear(a, b, hours=24):
    """ccu goes a -> b over the last `hours`, flat at a before that."""
    def f(t):
        x = min(1, max(0, 1 - (NOW - t) / (hours * H)))
        return a + (b - a) * x
    return f


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = db.connect(os.path.join(self.tmp, "t.db"))
        c = self.con
        build(c, "1", "roblox", lambda t: 10_000, created="2024-01-01", genre="Simulation")
        build(c, "2", "roblox", linear(500, 5000), created="2027-01-20", genre="Simulation",
              hits=[("up-and-coming", NOW - 10 * H)])
        build(c, "3", "roblox", linear(200_000, 240_000), created="2022-01-01", genre="RPG")
        build(c, "4", "roblox", lambda t: 20, created="2027-01-01", genre="Obby")
        build(c, "5", "roblox", linear(300, 1200, hours=8), hours=8, created="2027-02-01",
              genre="Simulation")
        build(c, "6", "roblox", linear(3000, 1500), created="2025-01-01", genre="Simulation")
        build(c, "7", "roblox", linear(800, 2400), created="2023-05-01", genre="Simulation")
        # Steam: a new roguelike whose review velocity is accelerating
        build(c, "100", "steam", linear(400, 3000), created="Jan 20, 2027",
              tags=["Roguelike", "Deckbuilding"],
              reviews_fn=lambda t: int(50 + 40 * max(0, 72 - (NOW - t) / H) ** 1.6))
        build(c, "101", "steam", lambda t: 5000, created="Mar 3, 2020",
              tags=["Roguelike", "Action"], reviews_fn=lambda t: 90_000 + (t - NOW + 72 * H) // H)
        build(c, "102", "steam", linear(1000, 1300), created="Jun 1, 2024",
              tags=["Roguelike", "Strategy"], reviews_fn=lambda t: 4000 + (t - NOW + 72 * H) // H)
        c.commit()

    @staticmethod
    def _all(r):
        return r["rising"] + r["big_movers"] + r["falling"] + r["breakout"]

    def test_growth_beats_size(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        order = [g["id"] for g in r["rising"]]
        self.assertEqual(order[0], "2", f"fast young riser should lead, got {order}")
        self.assertIn("7", order)
        self.assertNotIn("1", order, "flat game is not 'rising'")
        self.assertNotIn("4", order, "tiny games filtered")
        self.assertIn("3", [g["id"] for g in r["big_movers"]])
        self.assertNotIn("3", order, "huge games kept out of 'rising'")

    def test_daily_cycle_is_cancelled(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        series = scoring._load_series(self.con, "roblox", NOW - 96 * H)["1"]
        flat = scoring.score_game(series, "roblox", NOW, 24, "2024-01-01")
        self.assertLess(abs(flat["ccu_growth_pct"]), 2.0, flat)
        self.assertNotIn("1", [g["id"] for g in self._all(r)])

    def test_decliner_scores_low(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        g6 = next(g for g in r["falling"] if g["id"] == "6")
        self.assertLess(g6["ccu_growth_pct"], -40)
        self.assertLess(g6["score"], 0)

    def test_partial_history_flagged(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        g5 = next(g for g in r["breakout"] if g["id"] == "5")
        self.assertEqual(g5["confidence"], "partial")

    def test_entry_bonus_and_visits(self):
        g2 = next(g for g in scoring.rank(self.con, "roblox", NOW, 24)["rising"] if g["id"] == "2")
        self.assertTrue(g2["entered_trending"])
        self.assertGreater(g2["flow_growth_pct"], 100)

    def test_steam_review_velocity(self):
        r = scoring.rank(self.con, "steam", NOW, 24)
        self.assertEqual(r["rising"][0]["id"], "100")
        self.assertNotIn("101", [g["id"] for g in r["rising"]])
        self.assertGreater(r["rising"][0]["flow_growth_pct"], 20)
        tags = {t["genre"]: t for t in r["genres"]}
        self.assertIn("Roguelike", tags)
        self.assertEqual(tags["Roguelike"]["games"], 3)

    def test_genre_momentum(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        sim = next(gr for gr in r["genres"] if gr["genre"] == "Simulation")
        self.assertGreaterEqual(sim["rising"], 2)
        self.assertGreater(sim["ccu_growth_pct"], 0)

    def test_outputs_written(self):
        res = {p: scoring.rank(self.con, p, NOW, 24) for p in ("roblox", "steam")}
        md = report.write_outputs(res, NOW, 24, os.path.join(self.tmp, "pub"))
        self.assertIn("Breakouts", md)
        with open(os.path.join(self.tmp, "pub", "roblox_24h.json")) as f:
            self.assertEqual(json.load(f)["window_h"], 24)

    def test_date_parsing(self):
        for s in ("2026-11-01", "Nov 1, 2026", "1 Nov, 2026", "November 1, 2026"):
            self.assertIsNotNone(scoring.parse_date(s), s)
        self.assertIsNone(scoring.parse_date("Coming soon"))


class DailyCadenceTests(unittest.TestCase):
    """One snapshot per day (what the scheduled job now does), 30 days of history."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        c = self.con = db.connect(os.path.join(self.tmp, "d.db"))
        D = 24
        build(c, "a", "roblox", linear(800, 6000, hours=5 * D), hours=30 * D, step_h=D,
              jitter_h=2, created="2027-01-01", genre="Simulation")      # rising all week
        build(c, "b", "roblox", lambda t: 20_000, hours=30 * D, step_h=D, jitter_h=2,
              created="2023-01-01", genre="Simulation")                  # flat
        build(c, "c", "roblox", linear(1000, 2600, hours=D), hours=30 * D, step_h=D,
              created="2024-06-01", genre="Tycoon")                      # jumped today
        build(c, "d", "roblox", linear(400, 1500, hours=2 * D), hours=3 * D, step_h=D,
              created="2027-01-12", genre="Simulation")                  # only 3 days old
        build(c, "s", "steam", linear(500, 4000, hours=6 * D), hours=30 * D, step_h=D,
              created="Jan 1, 2027", tags=["Roguelike"],
              reviews_fn=lambda t: int(100 + 5 * max(0, 30 * D - (NOW - t) / H) ** 1.3))
        c.commit()

    def ids(self, r, key="rising"):
        return [g["id"] for g in r[key]]

    def test_daily_window(self):
        r = scoring.rank(self.con, "roblox", NOW, 24)
        self.assertIn("c", self.ids(r), "jumped today -> rising in 24h view")
        self.assertIn("a", self.ids(r))
        self.assertNotIn("b", self.ids(r), "flat game not rising")
        a = next(g for g in r["rising"] if g["id"] == "a")
        self.assertEqual(a["confidence"], "full")
        self.assertIsNotNone(a["flow_growth_pct"], "visits/day computed from daily snapshots")

    def test_weekly_window(self):
        r = scoring.rank(self.con, "roblox", NOW, 168)
        order = self.ids(r)
        self.assertEqual(order[0], "a", f"sustained weekly riser leads the 7d view: {order}")
        a = r["rising"][0]
        self.assertGreater(a["ccu_growth_pct"], 300)
        d = next((g for g in r["breakout"] if g["id"] == "d"), None)
        self.assertIsNotNone(d)
        self.assertEqual(d["confidence"], "partial", "3 days of data in a 7d view is partial")

    def test_steam_daily(self):
        r = scoring.rank(self.con, "steam", NOW, 168)
        self.assertEqual(self.ids(r)[0], "s")
        self.assertIsNotNone(r["rising"][0]["flow_growth_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
