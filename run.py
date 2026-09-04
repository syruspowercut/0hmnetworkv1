"""
ig-engagers — find the most engaged Instagram accounts around a set of seed accounts.

Pipeline (each step caches to events/<event>/data/*.json — delete a cache file
to re-run just that step without paying for the earlier ones again):
  1. posts       — for each seed in seeds.txt, pull recent posts
  2. likers      — pull likers from each post
  3. aggregate   — count likes per username, drop seeds + one-off likers
  4. enrich      — pull profile data (followers, bio, ...) for top candidates
  5. rank        — score, filter to micro-influencer band, write ranked.csv with DMs

Then run summary.py for the unfiltered engagers.csv.

Usage:
  python run.py --event reading-2026-05-22 --since 2025-09-01
  python run.py --event my-event --since 2026-01-01 --enrich-top 400
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

ROOT = Path(__file__).parent
EVENTS = ROOT / "events"

# --- defaults (override per run via CLI flags) ----------------------
POSTS_FETCH_LIMIT = 200          # request up to N most-recent posts per seed
LIKERS_PER_POST = 300            # per-post cap on the likers actor
TOP_CANDIDATES_TO_ENRICH = 250   # how many candidates get a profile scrape

# Override actor IDs via env if you swap to a different store actor.
LIKERS_ACTOR = os.environ.get("LIKERS_ACTOR", "datadoping/instagram-likes-scraper")
POSTS_ACTOR = os.environ.get("POSTS_ACTOR", "apify/instagram-scraper")
PROFILE_ACTOR = os.environ.get("PROFILE_ACTOR", "apify/instagram-profile-scraper")

# Scoring bands for step 5. Edit here if your idea of "micro" differs.
FOLLOWER_HARD_MIN = 500
FOLLOWER_HARD_MAX = 100_000
FOLLOWER_SWEET_LO = 1_000
FOLLOWER_SWEET_HI = 30_000


@dataclass
class Run:
    event_dir: Path
    data: Path
    seeds_file: Path
    dm_file: Path
    location_file: Path
    since: datetime | None
    posts_limit: int
    likers_per_post: int
    enrich_top: int
    client: ApifyClient
    apify_runs: list[tuple[str, str]] = field(default_factory=list)  # (actor, run_id)


# --- helpers ---------------------------------------------------------
def load_lines(path: Path) -> list[str]:
    """Non-empty, non-comment lines from a text file; [] if missing."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def cached(run: Run, name: str):
    p = run.data / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def write(run: Run, name: str, data) -> None:
    (run.data / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))


def call_actor(run: Run, actor: str, run_input: dict) -> list[dict]:
    result = run.client.actor(actor).call(run_input=run_input)
    run.apify_runs.append((actor, result["id"]))
    return list(run.client.dataset(result["defaultDatasetId"]).iterate_items())


def _post_timestamp(p: dict) -> datetime | None:
    ts = p.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


# --- step 1: posts -------------------------------------------------
def step_posts(run: Run, seeds: list[str]) -> list[dict]:
    if (data := cached(run, "posts")) is not None:
        print(f"[posts]      cached: {len(data)} posts")
        return data

    print(f"[posts]      scraping up to {run.posts_limit} posts from {len(seeds)} seed(s)...")
    raw = call_actor(run, POSTS_ACTOR, {
        "directUrls": [f"https://www.instagram.com/{s}/" for s in seeds],
        "resultsType": "posts",
        "resultsLimit": run.posts_limit,
        "addParentData": False,
    })

    if run.since:
        posts = [p for p in raw if (dt := _post_timestamp(p)) is not None and dt >= run.since]
        print(f"[posts]      kept {len(posts)}/{len(raw)} posts since {run.since.date()}")
    else:
        posts = raw
        print(f"[posts]      got {len(posts)} posts")
    write(run, "posts", posts)
    return posts


# --- step 2: likers ------------------------------------------------
def step_likers(run: Run, posts: list[dict]) -> list[dict]:
    if (data := cached(run, "likers")) is not None:
        print(f"[likers]     cached: {len(data)} liker entries")
        return data

    # post URL → seed (= post owner) so we can tag each liker.
    url_to_seed: dict[str, str] = {}
    for p in posts:
        owner = (p.get("ownerUsername") or "").lower()
        url = p.get("url") or (
            f"https://www.instagram.com/p/{p['shortCode']}/" if p.get("shortCode") else ""
        )
        if owner and url:
            url_to_seed[url] = owner

    urls = list(url_to_seed.keys())
    if not urls:
        write(run, "likers", [])
        return []

    only_seed = next(iter(set(url_to_seed.values()))) if len(set(url_to_seed.values())) == 1 else ""

    print(f"[likers]     scraping likers on {len(urls)} posts (cap {run.likers_per_post}/post)...")
    items = call_actor(run, LIKERS_ACTOR, {
        "posts": urls,
        "max_count": run.likers_per_post,
    })

    # Normalize fields. datadoping emits snake_case (full_name, is_verified,
    # liked_post). instaprism emitted camelCase but stuffed username into
    # userId. Handle both shapes so we can swap actors via env without code
    # changes.
    for it in items:
        if not it.get("username") and it.get("userId") and it["userId"] != "login":
            it["username"] = it["userId"]
        if it.get("full_name") and not it.get("fullName"):
            it["fullName"] = it["full_name"]
        if "is_verified" in it and "isVerified" not in it:
            it["isVerified"] = it["is_verified"]
        if "is_private" in it and "isPrivate" not in it:
            it["isPrivate"] = it["is_private"]
        src = it.get("liked_post") or it.get("sourcePost") or it.get("postUrl") or ""
        it["_seed"] = url_to_seed.get(src, only_seed)

    write(run, "likers", items)
    real = sum(1 for i in items if i.get("username"))
    print(f"[likers]     got {len(items)} liker entries ({real} with usernames)")
    return items


# --- step 3: aggregate ---------------------------------------------
def step_aggregate(run: Run, likers: list[dict], seeds: list[str]) -> list[dict]:
    if (data := cached(run, "candidates")) is not None:
        print(f"[aggregate]  cached: {len(data)} candidates")
        return data

    seeds_lower = {s.lower() for s in seeds}
    counter: Counter[str] = Counter()
    seed_set: dict[str, set[str]] = defaultdict(set)
    is_verified: dict[str, bool] = {}

    for l in likers:
        u = (l.get("username") or "").lower()
        if not u or u in seeds_lower:
            continue
        counter[u] += 1
        if seed := l.get("_seed"):
            seed_set[u].add(seed)
        if l.get("isVerified"):
            is_verified[u] = True

    # Multi-seed: the strong signal is "liked posts from ≥2 different seeds".
    # Single seed: that filter doesn't apply, so keep all and let the follower-
    # band filter in step_rank do the cutting. Only enforce a recurring-liker
    # filter when we're swimming in data.
    if len(seeds) > 1:
        keep = lambda u, n: len(seed_set[u]) >= 2  # noqa: E731
    elif len(counter) > 300:
        keep = lambda u, n: n >= 2  # noqa: E731
    else:
        keep = lambda u, n: True  # noqa: E731

    candidates = [
        {
            "username": u,
            "engagements": n,
            "seeds_engaged": sorted(seed_set.get(u, [])),
            "verifiedFromLikes": is_verified.get(u, False),
        }
        for u, n in counter.most_common()
        if keep(u, n)
    ]
    write(run, "candidates", candidates)
    print(f"[aggregate]  {len(candidates)} candidates after filtering")
    return candidates


# --- step 4: enrich ------------------------------------------------
def step_enrich(run: Run, candidates: list[dict]) -> list[dict]:
    if (data := cached(run, "enriched")) is not None:
        print(f"[enrich]     cached: {len(data)} profiles")
        return data

    top = candidates[:run.enrich_top]
    usernames = [c["username"] for c in top]
    if not usernames:
        write(run, "enriched", [])
        return []

    print(f"[enrich]     scraping {len(usernames)} profiles...")
    profiles = call_actor(run, PROFILE_ACTOR, {"usernames": usernames})
    by_user = {(p.get("username") or "").lower(): p for p in profiles}

    merged = []
    for c in top:
        p = by_user.get(c["username"].lower(), {})
        merged.append({
            **c,
            "fullName": p.get("fullName", ""),
            "biography": p.get("biography", ""),
            "followersCount": p.get("followersCount") or 0,
            "followsCount": p.get("followsCount") or 0,
            "postsCount": p.get("postsCount") or 0,
            "isPrivate": bool(p.get("private")),
            "isVerified": bool(p.get("verified")),
            "externalUrl": p.get("externalUrl", "") or "",
            "profilePicUrl": p.get("profilePicUrlHD") or p.get("profilePicUrl") or "",
            "profileUrl": f"https://www.instagram.com/{c['username']}/",
        })

    write(run, "enriched", merged)
    print(f"[enrich]     enriched {len(merged)} profiles")
    return merged


# --- step 5: rank --------------------------------------------------
# Tokens you can write into dm_template.txt. Replaced verbatim, so any other
# brackets/braces in your wording are left alone.
DM_TOKENS = {
    "[NAME]": "first_name",
    "[HANDLE]": "handle",
    "[SEEDS]": "seeds",
    # older spelling, still supported
    "{first_name}": "first_name",
    "{handle}": "handle",
    "{seeds}": "seeds",
}
HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "sir"}


def first_name_of(p: dict) -> str:
    words = [w for w in (p.get("fullName") or "").split() if w.strip(".").lower() not in HONORIFICS]
    return words[0] if words else p["username"]


def fill_dm(template: str, p: dict) -> str:
    if not template:
        return ""
    values = {
        "first_name": first_name_of(p),
        "handle": p["username"],
        "seeds": ", ".join(f"@{s}" for s in p.get("seeds_engaged", [])),
    }
    out = template
    for token, key in DM_TOKENS.items():
        out = out.replace(token, values[key])
    return out


def score(p: dict, location_terms: list[str]) -> tuple[float, dict]:
    breakdown: dict = {}
    followers = p.get("followersCount") or 0
    bio = (p.get("biography") or "").lower()
    full = (p.get("fullName") or "").lower()
    engagements = p.get("engagements", 0)
    posts = p.get("postsCount") or 0

    if p.get("isPrivate"):
        return -1, {"rejected": "private"}
    if followers < FOLLOWER_HARD_MIN:
        return -1, {"rejected": f"<{FOLLOWER_HARD_MIN} followers"}
    if followers > FOLLOWER_HARD_MAX:
        return -1, {"rejected": f">{FOLLOWER_HARD_MAX} followers"}

    if FOLLOWER_SWEET_LO <= followers <= FOLLOWER_SWEET_HI:
        band = 40.0
    elif followers < FOLLOWER_SWEET_LO:
        band = 25.0 * (followers / FOLLOWER_SWEET_LO)
    else:
        band = 30.0 * (1 - (followers - FOLLOWER_SWEET_HI) / (FOLLOWER_HARD_MAX - FOLLOWER_SWEET_HI))
    breakdown["follower_band"] = round(band, 1)

    eng = float(min(engagements * 8, 30))
    breakdown["engagement"] = eng

    loc_hits = sum(1 for t in location_terms if t in bio or t in full)
    loc = float(min(loc_hits * 10, 20))
    breakdown["location"] = loc

    act = 10.0 if posts >= 20 else (5.0 if posts >= 5 else 0.0)
    breakdown["activity"] = act

    return round(band + eng + loc + act, 1), breakdown


def step_rank(run: Run, enriched: list[dict]) -> None:
    template = run.dm_file.read_text() if run.dm_file.exists() else ""
    location_terms = [t.lower() for t in load_lines(run.location_file)]
    rows = []
    rejected = []

    for p in enriched:
        s, breakdown = score(p, location_terms)
        row_base = {
            "username": p["username"],
            "followers": p.get("followersCount", 0),
            "engagements": p.get("engagements", 0),
            "fullName": p.get("fullName", ""),
            "biography": (p.get("biography") or "").replace("\n", " ")[:200],
            "profileUrl": p.get("profileUrl"),
        }
        if s < 0:
            rejected.append({**row_base, "reason": breakdown.get("rejected", "")})
            continue

        rows.append({
            "score": s,
            **row_base,
            "score_breakdown": json.dumps(breakdown),
            "dm": fill_dm(template, p),
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    out = run.data / "ranked.csv"
    if rows:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"[rank]       wrote {len(rows)} ranked rows → {out}")
    else:
        print(f"[rank]       no rows passed filters — check {run.data / 'enriched.json'}")

    if rejected:
        rej_out = run.data / "rejected.csv"
        with rej_out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rejected[0].keys()))
            w.writeheader()
            w.writerows(rejected)
        print(f"[rank]       wrote {len(rejected)} rejected rows → {rej_out}")


# --- cost ------------------------------------------------------------
def print_cost(run: Run) -> None:
    if not run.apify_runs:
        print("\n[cost]       no Apify runs this invocation (everything cached)")
        return
    total = 0.0
    print("\n[cost]       Apify spend this invocation:")
    for actor, run_id in run.apify_runs:
        info = run.client.run(run_id).get() or {}
        usd = float(info.get("usageTotalUsd") or 0)
        total += usd
        print(f"             {actor:<45} ${usd:.2f}")
    print(f"             {'total':<45} ${total:.2f}")


# --- main ----------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", required=True,
                    help="event folder name under events/ (must contain seeds.txt)")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only keep posts on/after this date (default: keep all fetched)")
    ap.add_argument("--posts-limit", type=int, default=POSTS_FETCH_LIMIT,
                    help=f"max recent posts to fetch per seed (default {POSTS_FETCH_LIMIT})")
    ap.add_argument("--likers-per-post", type=int, default=LIKERS_PER_POST,
                    help=f"per-post cap on likers scraped (default {LIKERS_PER_POST})")
    ap.add_argument("--enrich-top", type=int, default=TOP_CANDIDATES_TO_ENRICH,
                    help=f"how many top candidates get a profile scrape (default {TOP_CANDIDATES_TO_ENRICH})")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / ".env")

    token = os.environ.get("APIFY_TOKEN")
    if not token or token.startswith("apify_api_xxxx"):
        raise SystemExit(
            "APIFY_TOKEN missing or placeholder. Copy .env.example to .env and "
            "paste your token from https://console.apify.com/settings/integrations"
        )

    event_dir = EVENTS / args.event
    if not event_dir.is_dir():
        raise SystemExit(
            f"No event folder at {event_dir}\n"
            f"Create it with a seeds.txt inside (see events/reading-2026-05-22/ for an example)."
        )

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            raise SystemExit(f"--since must be YYYY-MM-DD, got {args.since!r}")

    run = Run(
        event_dir=event_dir,
        data=event_dir / "data",
        seeds_file=event_dir / "seeds.txt",
        dm_file=event_dir / "dm_template.txt",
        location_file=event_dir / "location_terms.txt",
        since=since,
        posts_limit=args.posts_limit,
        likers_per_post=args.likers_per_post,
        enrich_top=args.enrich_top,
        client=ApifyClient(token),
    )
    run.data.mkdir(exist_ok=True)

    seeds = [s.lstrip("@") for s in load_lines(run.seeds_file)]
    if not seeds:
        raise SystemExit(f"No seeds in {run.seeds_file}. Add an IG handle (one per line).")
    print(f"event: {args.event}\nseeds: {seeds}\n")

    posts = step_posts(run, seeds)
    likers = step_likers(run, posts)
    candidates = step_aggregate(run, likers, seeds)
    enriched = step_enrich(run, candidates)
    step_rank(run, enriched)
    print_cost(run)


if __name__ == "__main__":
    main()
