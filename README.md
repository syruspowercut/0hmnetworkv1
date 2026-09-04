# ig-engagers

Find the most engaged Instagram accounts around one or more "seed" accounts, then
rank them as potential local reps / micro-influencers for an event.

Built for finding people to promote a club night in a city where the brand had no
awareness. Give it a local account whose audience you want (a uni society, a venue,
a rival promoter), and it tells you who keeps showing up in their likes — with
follower counts, bios, and a pre-filled DM.

Scraping runs on [Apify](https://apify.com) actors, so there's no login, no cookies,
and no risk to your own Instagram account.

## What you get

For each event, two CSVs:

| File | What it is |
|---|---|
| `engagers.csv` | **Everyone** who liked ≥1 seed post, sorted by how many posts they liked. Follower count + profile pic where known. No filtering. This is usually the one you want. |
| `ranked.csv` | Only accounts in the micro-influencer band (500–100k followers, public), scored on follower sweet-spot + engagement + location signals in bio, with a copy-paste DM per row. |

Plus `rejected.csv` (what `ranked.csv` filtered out, and why) and the raw JSON
caches so you can re-run steps without re-paying.

## Cost

Pay-per-result on Apify. A real run — one seed, 57 posts, ~2,500 likers, 250 profile
enrichments — came to about **$5**. Apify's free tier gives $5/month credit, which
covers one run of that size. Each invocation prints its spend at the end.

Almost all of it is the likers step: **posts × likers-per-post cap × ~$1.55/1k**. So
the levers, biggest first:

1. **Fewer posts** — a `--since` date, or a lower posts limit.
2. **Lower likers cap** — you keep the first N likers per post. That undercounts how many
   posts each person liked, but rarely changes *who* tops the list; it mostly drops the
   people who liked only a couple of posts.
3. **Enrichment → 0** — skips the follower-count/bio scrape ($2.60/1k). `engagers.csv`
   still works, just without follower numbers. Profile pics cost nothing either way.

To see what a run will cost before paying for it, use **posts only** (the checkbox in
the UI, or `--stop-after posts`): it fetches the posts (~5¢), prints like counts and a
cost estimate at several caps, and stops. Run again without it to continue — the posts
are cached.

## Quick start (Mac, no terminal)

1. Get the code — either

   ```bash
   git clone https://github.com/syruspowercut/0hmnetworkv1.git
   ```

   or **Code → Download ZIP** on GitHub and unzip it.

2. Double-click **`start.command`**. The first run installs Python dependencies (about
   30 seconds); after that your browser opens at `http://127.0.0.1:8765`.

3. Paste your Apify token (sign up at [apify.com](https://apify.com) — free tier gives
   $5/month credit, enough for one decent run), type an event name and one or more seed
   handles, hit **Run**. Progress streams to the page; CSVs are downloadable when it's
   done.

Everything runs on your machine. The only outbound traffic is to Apify. Your token is
saved to `.env` in the folder and nowhere else.

**If macOS refuses to open `start.command`** ("from an unidentified developer") —
that's Gatekeeper on files downloaded as a ZIP. Right-click → **Open** → **Open**. Only
needed once. A `git clone` doesn't trigger it.

**If it opens in a text editor instead of running** — the ZIP dropped the executable
bit. Open Terminal, `cd` into the folder, and run `bash start.command`.

**If macOS asks to install "command line developer tools"** — say yes; that's how a
fresh Mac gets `python3`. Then double-click again.

## Command line

Same pipeline without the UI. Needs Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # paste your Apify token in
```

### Running for a new event

1. Make a folder under `events/`. The name is just a label:

   ```bash
   mkdir events/birmingham-2026-08-15
   ```

2. Put a `seeds.txt` in it — one Instagram handle per line, `#` for comments:

   ```
   # local accounts whose audience we want
   bhamacs
   digbethnightlife
   ```

   One seed works. Two or more is better: with multiple seeds the pipeline only keeps
   people who engaged with **at least two** of them, which is a much stronger signal.

3. Optionally add `location_terms.txt` (words that boost a candidate's score if they
   appear in their bio — postcodes, uni nicknames, the city name) and `dm_template.txt`
   (see [DM template](#dm-template) below). Copy the ones in
   `events/reading-2026-05-22/` as a starting point.

4. Run it:

   ```bash
   .venv/bin/python run.py --event birmingham-2026-08-15 --since 2026-01-01
   .venv/bin/python summary.py --event birmingham-2026-08-15
   ```

   `--since` drops posts older than that date. Omit it to keep whatever the post
   scraper returns (up to `--posts-limit`, default 200).

Output lands in `events/birmingham-2026-08-15/data/`.

#### Flags

```
--event NAME            event folder under events/ (required)
--since YYYY-MM-DD      only keep posts on/after this date
--posts-limit N         max recent posts per seed to fetch (default 200)
--likers-per-post N     cap on likers scraped per post (default 300)
--enrich-top N          how many top candidates get a profile scrape (default 250; 0 = skip)
--stop-after posts      fetch posts, print a likers cost estimate at several caps, stop
```

`--enrich-top` is the main cost lever after the likers scrape. Profiles without
enrichment still appear in `engagers.csv`, just without follower counts.

#### Re-running cheaply

Every step caches to `events/<event>/data/*.json`. To redo a step, delete its cache
and run again — earlier steps are skipped:

- Change scoring / DM template → delete nothing, just re-run (`rank` is free)
- Drop likes on old posts → re-run with a later since date, or `summary.py --since YYYY-MM-DD`;
  nothing is re-scraped, `engagers.csv` is just rebuilt from the cached likes
- Enrich more people → delete `enriched.json`, bump `--enrich-top`
- Add a seed → delete everything in `data/`

#### DM template

If `events/<event>/dm_template.txt` exists, every row in `ranked.csv` gets a `dm`
column with that text, personalised. Write whatever you like; these labels are swapped
in per person:

| Label | Becomes |
|---|---|
| `[NAME]` | their first name (from their profile, honorifics stripped; falls back to handle) |
| `[HANDLE]` | their Instagram handle, no `@` |
| `[SEEDS]` | the seed account(s) they engaged with, as `@handle, @handle` |

Everything else is left exactly as written, so brackets and braces in your own wording
are safe. Delete the file if you don't want DMs generated — the column will be empty.

## Tuning

Scoring bands live at the top of `run.py` (`FOLLOWER_HARD_MIN`, `FOLLOWER_SWEET_LO`,
etc.). The Reading run found the most engaged people were mostly *under* 1k followers —
core community members rather than influencers — so if that's what you're after,
lower `FOLLOWER_HARD_MIN` or just use `engagers.csv` and ignore the ranking.

## Actors

Three Apify actors, overridable via env vars if one breaks or a better one appears:

| Step | Default actor | Env var |
|---|---|---|
| posts | `apify/instagram-scraper` | `POSTS_ACTOR` |
| likers | `datadoping/instagram-likes-scraper` | `LIKERS_ACTOR` |
| profiles | `apify/instagram-profile-scraper` | `PROFILE_ACTOR` |

The likers actor is the one that matters. Instagram hides most likers from logged-out
viewers, and cheap actors that only hit the public endpoint return ~2 likers per post.
`datadoping` runs its own auth pool server-side and returned 40× more on the same posts
at a lower per-result price. If you swap it, check the field names it emits —
`step_likers` normalises `snake_case`/`camelCase` and a couple of known quirks, but a
new actor may need another line there.

## Caveats

- **Profile pic URLs expire.** They're signed Instagram CDN links, good for hours to a
  few days. If you want the pics, paste the `pic_sheets_formula` column into Google
  Sheets soon after the run — Sheets caches the image.
- **The output is personal data.** `events/*/data/` is gitignored for a reason. Don't
  commit it, don't share it beyond the people who need it.
- **Actors break.** Instagram changes things; Apify actors go stale. If a step
  suddenly returns garbage, check the actor's page on Apify for recent reviews before
  assuming the pipeline is at fault.

## License

MIT
