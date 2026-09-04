"""
Build events/<event>/data/engagers.csv from cached scrape data — no Apify calls.

Reads (all under events/<event>/data/):
  - likers.json     all liker entries (one row per like)
  - posts.json      to identify seed accounts and exclude them
  - enriched.json   follower/bio data for the top-N enriched candidates (optional)

Output: engagers.csv — one row per unique engager, sorted by engagements desc.
Includes EVERYONE (no follower-band or privacy filters). Profile pic URL is
included; in Google Sheets the `pic_sheets_formula` column renders the image
inline.

Usage:
  python summary.py --event reading-2026-05-22
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
EVENTS = ROOT / "events"


def build_engagers(event: str, log=print) -> Path:
    """Write engagers.csv for one event from its cached data. Raises RuntimeError if not scraped yet."""
    data = EVENTS / event / "data"
    if not (data / "likers.json").exists():
        raise RuntimeError(f"No likers.json in {data} — run run.py --event {event} first.")

    likers = json.loads((data / "likers.json").read_text())
    posts = json.loads((data / "posts.json").read_text())
    try:
        enriched = json.loads((data / "enriched.json").read_text())
    except FileNotFoundError:
        enriched = []

    seeds = {(p.get("ownerUsername") or "").lower() for p in posts if p.get("ownerUsername")}

    counter: Counter[str] = Counter()
    profile_pic: dict[str, str] = {}
    full_name: dict[str, str] = {}
    verified: dict[str, bool] = {}
    private: dict[str, bool] = {}

    for l in likers:
        u = (l.get("username") or "").lower()
        if not u or u in seeds:
            continue
        counter[u] += 1
        if u not in profile_pic:
            pic = l.get("profile_pic_url") or l.get("profilePicUrl") or ""
            if pic:
                profile_pic[u] = pic
        if u not in full_name:
            full_name[u] = l.get("full_name") or l.get("fullName") or ""
        if l.get("is_verified") or l.get("isVerified"):
            verified[u] = True
        if l.get("is_private") or l.get("isPrivate"):
            private[u] = True

    by_user = {(p.get("username") or "").lower(): p for p in enriched}

    rows = []
    for u, n in counter.most_common():
        prof = by_user.get(u, {})
        pic = profile_pic.get(u) or prof.get("profilePicUrl") or ""
        rows.append({
            "rank": 0,
            "username": u,
            "engagements": n,
            "followers": prof.get("followersCount", ""),
            "fullName": full_name.get(u) or prof.get("fullName", ""),
            "isVerified": verified.get(u, False),
            "isPrivate": private.get(u, False),
            "biography": (prof.get("biography") or "").replace("\n", " ")[:200],
            "profileUrl": f"https://www.instagram.com/{u}/",
            "profilePicUrl": pic,
            "pic_sheets_formula": f'=IMAGE("{pic}")' if pic else "",
            "enriched": "yes" if prof else "no",
        })
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    out = data / "engagers.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    enriched_count = sum(1 for r in rows if r["enriched"] == "yes")
    log(f"wrote {len(rows)} engagers → {out}")
    log(f"  with follower data: {enriched_count}")
    log(f"  without (need enrichment for followers): {len(rows) - enriched_count}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", required=True, help="event folder name under events/")
    args = ap.parse_args()
    try:
        build_engagers(args.event)
    except RuntimeError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
