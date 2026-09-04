"""
Local web UI for ig-engagers. Binds to 127.0.0.1 only — nothing leaves your machine
except the Apify API calls the pipeline makes.

  python app.py               # start server and open browser
  python app.py --no-browser  # just start server
"""
from __future__ import annotations

import argparse
import re
import threading
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, url_for

import run as pipeline
import summary

ROOT = Path(__file__).parent
EVENTS = ROOT / "events"
ENV = ROOT / ".env"
PORT = 8765
CSV_FILES = ("engagers.csv", "ranked.csv", "rejected.csv")

app = Flask(__name__)

# One job at a time. State lives in memory for the life of the server.
_lock = threading.Lock()
_job: dict = {"event": None, "running": False, "done": False, "error": None, "log": []}


# --- helpers ---------------------------------------------------------
def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.strip().lower()).strip("-")[:60]


def has_token() -> bool:
    if not ENV.exists():
        return False
    for line in ENV.read_text().splitlines():
        if line.startswith("APIFY_TOKEN=") and "xxxx" not in line and len(line) > len("APIFY_TOKEN="):
            return True
    return False


def save_token(tok: str) -> None:
    ENV.write_text(f"APIFY_TOKEN={tok.strip()}\n")


def list_events() -> list[dict]:
    if not EVENTS.exists():
        return []
    out = []
    for d in sorted(EVENTS.iterdir(), reverse=True):
        if d.is_dir() and (d / "seeds.txt").exists():
            data = d / "data"
            out.append({
                "name": d.name,
                "seeds": pipeline.load_lines(d / "seeds.txt"),
                "files": [f for f in CSV_FILES if (data / f).exists()],
            })
    return out


def read_or(path: Path, default: str = "") -> str:
    return path.read_text() if path.exists() else default


def form_text(name: str) -> str:
    """Form field with browser CRLF line endings normalised to LF."""
    return request.form.get(name, "").replace("\r\n", "\n").replace("\r", "\n")


def worker(event: str, since: str | None, likers_per_post: int, enrich_top: int, posts_only: bool) -> None:
    log = _job["log"].append
    try:
        pipeline.run_pipeline(event, since, likers_per_post=likers_per_post, enrich_top=enrich_top, log=log,
                              stop_after="posts" if posts_only else None)
        if posts_only:
            log("\nPosts fetched. Untick \"posts only\" and run again to scrape likers.")
        else:
            log("")
            summary.build_engagers(event, since=since, log=log)
            log("\nDone.")
    except Exception as e:  # surface anything — actor failures, bad token, etc.
        _job["error"] = f"{type(e).__name__}: {e}"
        log(f"\nERROR — {type(e).__name__}: {e}")
    finally:
        _job["running"] = False
        _job["done"] = True


# --- routes ----------------------------------------------------------
@app.get("/")
def index():
    prefill = slug(request.args.get("event", ""))
    ev = EVENTS / prefill if prefill else None
    return render_template_string(
        INDEX,
        events=list_events(),
        has_token=has_token(),
        job=_job,
        prefill=prefill,
        seeds=read_or(ev / "seeds.txt") if ev else "",
        dm=read_or(ev / "dm_template.txt") if ev else read_or(EVENTS / "reading-2026-05-22" / "dm_template.txt"),
        location=read_or(ev / "location_terms.txt") if ev else "",
        defaults=dict(likers=pipeline.LIKERS_PER_POST, enrich=pipeline.TOP_CANDIDATES_TO_ENRICH),
    )


@app.post("/run")
def start_run():
    with _lock:
        if _job["running"]:
            return "A run is already in progress.", 409

        tok = request.form.get("token", "").strip()
        if tok:
            save_token(tok)
        if not has_token():
            return "No Apify token saved. Paste one and try again.", 400

        event = slug(request.form.get("event", ""))
        seeds_txt = form_text("seeds").strip()
        if not event or not seeds_txt:
            return "Event name and at least one seed handle are required.", 400

        def as_int(name: str, default: int, lo: int = 1) -> int:
            try:
                return max(lo, int(request.form.get(name, "") or default))
            except ValueError:
                return default

        ev = EVENTS / event
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "seeds.txt").write_text(seeds_txt + "\n")
        (ev / "dm_template.txt").write_text(form_text("dm").rstrip() + "\n")
        (ev / "location_terms.txt").write_text(form_text("location").strip() + "\n")

        _job.update(event=event, running=True, done=False, error=None, log=[])
        threading.Thread(
            target=worker,
            args=(event, request.form.get("since", "").strip() or None,
                  as_int("likers", pipeline.LIKERS_PER_POST), as_int("enrich", pipeline.TOP_CANDIDATES_TO_ENRICH, lo=0),
                  request.form.get("posts_only") == "on"),
            daemon=True,
        ).start()
    return redirect(url_for("status", event=event))


@app.get("/status/<event>")
def status(event: str):
    return render_template_string(STATUS, event=slug(event), files=CSV_FILES)


@app.get("/api/status")
def api_status():
    return jsonify(_job)


@app.get("/download/<event>/<name>")
def download(event: str, name: str):
    if name not in CSV_FILES:
        abort(404)
    p = EVENTS / slug(event) / "data" / name
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True, download_name=f"{slug(event)}-{name}")


# --- templates -------------------------------------------------------
CSS = """
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 -apple-system, system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; } h2 { font-size: 1.1rem; margin: 2rem 0 .5rem; }
  .muted { opacity: .65; font-size: .9rem; }
  label { display: block; font-weight: 600; margin-top: 1rem; }
  label small { font-weight: 400; opacity: .65; }
  input[type=text], input[type=password], input[type=date], input[type=number], textarea {
    width: 100%; box-sizing: border-box; padding: .5rem; font: inherit; border: 1px solid #8884; border-radius: 6px; background: transparent; }
  textarea { font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
  .row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }
  button { margin-top: 1.25rem; padding: .6rem 1.2rem; font: inherit; font-weight: 600; border: 0; border-radius: 6px; background: #2563eb; color: #fff; cursor: pointer; }
  button[disabled] { opacity: .5; cursor: default; }
  pre { background: #8881; padding: 1rem; border-radius: 6px; white-space: pre-wrap; min-height: 8rem; font-size: 13px; }
  .ok { color: #16a34a; } .bad { color: #dc2626; }
  table { border-collapse: collapse; width: 100%; } td, th { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8883; }
  a { color: #2563eb; }
</style>
"""

INDEX = """<!doctype html><meta charset="utf-8"><title>ig-engagers</title>""" + CSS + """
<h1>ig-engagers</h1>
<p class="muted">Find the most engaged Instagram accounts around a seed account. Runs locally; only talks to Apify.</p>

{% if job.running %}
<p class="bad">A run is in progress for <b>{{ job.event }}</b> — <a href="/status/{{ job.event }}">watch it</a>.</p>
{% endif %}

<form method="post" action="/run">
  <label>Apify token
    <small>{% if has_token %}— one is saved; leave blank to keep it{% else %}— get one at <a href="https://console.apify.com/settings/integrations" target="_blank">console.apify.com</a> (free tier: $5/month){% endif %}</small></label>
  <input type="password" name="token" placeholder="{{ 'saved' if has_token else 'apify_api_…' }}" autocomplete="off">

  <label>Event name <small>— letters, numbers, dashes. Used as the folder name.</small></label>
  <input type="text" name="event" value="{{ prefill }}" placeholder="birmingham-2026-08-15" required>

  <label>Seed accounts <small>— one Instagram handle per line. 2+ gives a much stronger signal.</small></label>
  <textarea name="seeds" rows="3" placeholder="bhamacs&#10;digbethnightlife" required>{{ seeds }}</textarea>

  <div class="row">
    <div><label>Posts since <small>— optional; on a re-run it re-filters cached likes for free</small></label><input type="date" name="since"></div>
    <div><label>Likers per post</label><input type="number" name="likers" value="{{ defaults.likers }}" min="1"></div>
    <div><label>Profiles to enrich <small>— 0 = skip</small></label><input type="number" name="enrich" value="{{ defaults.enrich }}" min="0"></div>
  </div>
  <p class="muted">Cost is pay-per-result on Apify. Likers are the big line: posts × cap × $1.55/1k. Enrichment (follower counts + bios) is $2.60/1k — set it to 0 if you only need <b>engagers.csv</b>.</p>
  <label style="font-weight:400"><input type="checkbox" name="posts_only"> <b>Posts only</b> <small>— fetch the posts (~5¢), print like counts and a cost estimate at several caps, and stop. Run again without it to continue; posts are cached.</small></label>

  <label>Location terms <small>— optional; words in a bio that mark someone as local. One per line.</small></label>
  <textarea name="location" rows="3" placeholder="birmingham&#10;brum&#10;b1">{{ location }}</textarea>

  <label>DM template <small>— optional; [NAME] [HANDLE] [SEEDS] are filled in per person. Leave empty for no DMs.</small></label>
  <textarea name="dm" rows="6">{{ dm }}</textarea>

  <button type="submit" {% if job.running %}disabled{% endif %}>Run</button>
</form>

{% if events %}
<h2>Previous runs</h2>
<table>
  <tr><th>Event</th><th>Seeds</th><th>Downloads</th><th></th></tr>
  {% for e in events %}
  <tr>
    <td>{{ e.name }}</td>
    <td class="muted">{{ e.seeds|join(', ') }}</td>
    <td>{% for f in e.files %}<a href="/download/{{ e.name }}/{{ f }}">{{ f }}</a>{% if not loop.last %} · {% endif %}{% endfor %}</td>
    <td><a href="/?event={{ e.name }}">re-run</a></td>
  </tr>
  {% endfor %}
</table>
<p class="muted">Re-running reuses cached steps for free. To re-scrape, delete files in <code>events/&lt;event&gt;/data/</code> first.</p>
{% endif %}
"""

STATUS = """<!doctype html><meta charset="utf-8"><title>{{ event }} — ig-engagers</title>""" + CSS + """
<h1>{{ event }}</h1>
<p class="muted"><a href="/">← back</a> &nbsp; <span id="state">Running…</span></p>
<pre id="log"></pre>
<div id="downloads" hidden>
  <h2>Downloads</h2>
  <p>{% for f in files %}<a href="/download/{{ event }}/{{ f }}">{{ f }}</a>{% if not loop.last %} &nbsp;·&nbsp; {% endif %}{% endfor %}</p>
  <p class="muted"><b>engagers.csv</b> is everyone, sorted by how many seed posts they liked. <b>ranked.csv</b> is the filtered micro-influencer shortlist with DMs.</p>
</div>
<script>
  const logEl = document.getElementById('log'), stateEl = document.getElementById('state'), dl = document.getElementById('downloads');
  async function tick() {
    const j = await (await fetch('/api/status')).json();
    if (j.event !== {{ event|tojson }}) { stateEl.textContent = 'No run in progress for this event.'; return; }
    logEl.textContent = j.log.join('\\n');
    if (j.done) {
      stateEl.innerHTML = j.error ? '<span class="bad">Failed</span>' : '<span class="ok">Done</span>';
      dl.hidden = !!j.error;
      return;
    }
    setTimeout(tick, 1000);
  }
  tick();
</script>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{PORT}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"ig-engagers running at {url} — Ctrl-C to stop")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
