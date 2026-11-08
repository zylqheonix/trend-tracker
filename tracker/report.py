"""Writes the JSON 'API' files and a readable markdown digest. Optional Discord push."""
import json
import os
import urllib.request
from datetime import datetime, timezone

from .net import UA, ssl_context


def _fmt(n):
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def _line(g, platform):
    flow = "visits" if platform == "roblox" else "reviews"  # per-day inflow
    bits = [f"**{g['name'] or g['id']}**", f"{_fmt(g['ccu_prev'])} → {_fmt(g['ccu_now'])} CCU "
            f"({g['ccu_growth_pct']:+.0f}%)"]
    if g.get("flow_growth_pct") is not None:
        bits.append(f"{flow}/day {g['flow_growth_pct']:+.0f}%")
    if g.get("age_days") is not None:
        bits.append(f"{g['age_days']}d old")
    if platform == "steam" and g.get("tags"):
        bits.append(", ".join(g["tags"][:2]))
    elif g.get("genre"):
        bits.append(g["genre"])
    if g.get("entered_trending"):
        bits.append("🆕 entered trending")
    if g.get("confidence") == "partial":
        bits.append(f"⚠ partial ({g['span_h']}h data)")
    return "- " + " · ".join(bits) + (f" — [link]({g['url']})" if g.get("url") else "")


def digest_md(results, now, window_h):
    ts = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Trend digest — {ts} (window {window_h}h)\n"]
    for platform, label in (("roblox", "Roblox"), ("steam", "Steam")):
        r = results.get(platform)
        if not r:
            continue
        out.append(f"## {label} ({r['n_scored']} games scored)\n")
        out.append("### Breakouts (≤60 days old, growing fastest)")
        out += [_line(g, platform) for g in r["breakout"][:10]] or ["- (none yet)"]
        out.append("\n### Rising (not already huge)")
        out += [_line(g, platform) for g in r["rising"][:10]] or ["- (none yet)"]
        out.append(f"\n### {'Genres' if platform == 'roblox' else 'Tags'} gaining momentum")
        for gr in r["genres"][:8]:
            out.append(f"- **{gr['genre']}** — CCU {gr['ccu_growth_pct']:+.0f}% across "
                       f"{gr['games']} games, {gr['rising']} rising ({gr['young_rising']} new) "
                       f"· e.g. {', '.join(filter(None, gr['top']))}")
        if not r["genres"]:
            out.append("- (needs ~24h of history)")
        out.append("")
    return "\n".join(out)


def write_outputs(results, now, window_h, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    meta = {"generated_at": now, "window_h": window_h}
    for platform, r in results.items():
        with open(os.path.join(out_dir, f"{platform}_{window_h}h.json"), "w") as f:
            json.dump({**meta, **r}, f, indent=1)
    md = digest_md(results, now, window_h)
    with open(os.path.join(out_dir, f"digest_{window_h}h.md"), "w") as f:
        f.write(md)
    return md


def push_discord(md, webhook):
    """Discord caps messages at 2000 chars: send in chunks split on line breaks."""
    chunks, cur = [], ""
    for line in md.splitlines():
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": c}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=20, context=ssl_context()).read()
