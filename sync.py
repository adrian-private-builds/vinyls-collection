#!/usr/bin/env python3
"""
Discogs Collection Sync
-----------------------
Fetches your public Discogs collection, downloads cover art,
saves collection.json, and regenerates index.html.

Usage:
    python3 sync.py YOUR_DISCOGS_USERNAME

Cron (daily at 8am):
    0 8 * * * cd /path/to/discogs-collection && python3 sync.py YOUR_USERNAME
"""

import sys
import json
import time
import os
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from html import escape

# ── Config ────────────────────────────────────────────────────────────────────
COVERS_DIR = Path("covers")
CUSTOM_COVERS_DIR = Path("covers/custom")
COLLECTION_FILE = Path("collection.json")
HTML_FILE = Path("index.html")
USER_AGENT = "DiscogsCollectionViewer/1.0"
DISCOGS_TOKEN = ""  # set via CLI arg or env var DISCOGS_TOKEN
REQUEST_DELAY = 1.0  # seconds between API calls (Discogs rate limit: 60/min)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_json(url):
    headers = {"User-Agent": USER_AGENT}
    if DISCOGS_TOKEN:
        headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def download_image(url, dest_path):
    if dest_path.exists():
        return True
    try:
        headers = {"User-Agent": USER_AGENT}
        if DISCOGS_TOKEN:
            headers["Authorization"] = f"Discogs token={DISCOGS_TOKEN}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest_path.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  ⚠ Could not download image: {e}")
        return False

# ── Fetch collection ──────────────────────────────────────────────────────────

def fetch_collection(username, folder_id=0):
    releases = []
    page = 1
    total_pages = 1

    print(f"📡 Fetching collection for @{username} (folder {folder_id})...")

    while page <= total_pages:
        url = (
            f"https://api.discogs.com/users/{username}/collection/folders/{folder_id}/releases"
            f"?page={page}&per_page=100&sort=artist&sort_order=asc"
        )
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"❌ User '{username}' not found or collection is private.")
                sys.exit(1)
            raise

        total_pages = data["pagination"]["pages"]
        print(f"  Page {page}/{total_pages} — {data['pagination']['items']} total items")

        for item in data["releases"]:
            info = item["basic_information"]
            artists = ", ".join(a["name"].rstrip("0123456789 ()") for a in info["artists"])
            releases.append({
                "id": info["id"],
                "master_id": info.get("master_id", 0),
                "title": info["title"],
                "artist": artists,
                "year": info.get("year", ""),
                "thumb": info.get("cover_image") or info.get("thumb", ""),
                "genres": info.get("genres", []),
                "styles": info.get("styles", []),
                "formats": [f["name"] for f in info.get("formats", [])],
                "vinyl_color": next(
                    (f.get("text", "") for f in info.get("formats", [])
                     if f.get("name") == "Vinyl" and f.get("text")),
                    ""
                ),
                "date_added": item.get("date_added", ""),
            })

        page += 1
        if page <= total_pages:
            time.sleep(REQUEST_DELAY)

    return releases

# ── Download covers ───────────────────────────────────────────────────────────

def download_covers(releases):
    COVERS_DIR.mkdir(exist_ok=True)
    CUSTOM_COVERS_DIR.mkdir(exist_ok=True)
    print(f"\n🖼  Downloading covers ({len(releases)} albums)...")

    for i, release in enumerate(releases):
        if not release["thumb"]:
            release["local_cover"] = ""
            continue

        ext = ".jpg"
        filename = f"{release['id']}{ext}"
        dest = COVERS_DIR / filename
        local_path = str(COVERS_DIR / filename)

        if not dest.exists():
            print(f"  [{i+1}/{len(releases)}] {release['artist']} — {release['title']}")
            ok = download_image(release["thumb"], dest)
            release["local_cover"] = local_path if ok else ""
            time.sleep(0.3)
        else:
            release["local_cover"] = local_path

    return releases

def _find_custom_cover(release_id):
    """Return path to custom cover if one exists, else None."""
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = CUSTOM_COVERS_DIR / f"{release_id}{ext}"
        if p.exists():
            return p
    return None

# ── Fetch master (original) release years ─────────────────────────────────────

def enrich_master_years(releases):
    # Load cached master years from previous run
    year_cache = {}
    if COLLECTION_FILE.exists():
        try:
            existing = json.loads(COLLECTION_FILE.read_text())
            for r in existing:
                mid = r.get("master_id", 0)
                if mid and r.get("master_year"):
                    year_cache[mid] = r["master_year"]
        except Exception:
            pass

    to_fetch = [r for r in releases if r.get("master_id") and r["master_id"] not in year_cache]

    if to_fetch:
        print(f"\n📅 Fetching original release years ({len(to_fetch)} masters, one-time)...")
        for i, r in enumerate(to_fetch):
            mid = r["master_id"]
            try:
                data = fetch_json(f"https://api.discogs.com/masters/{mid}")
                year_cache[mid] = data.get("year") or 0
            except Exception as e:
                print(f"  ⚠ {r['artist']} — {r['title']}: {e}")
                year_cache[mid] = 0
            if i < len(to_fetch) - 1:
                time.sleep(REQUEST_DELAY)

    for r in releases:
        mid = r.get("master_id", 0)
        r["master_year"] = (year_cache.get(mid) if mid else None) or r.get("year") or 0

    # Manual overrides — keyed by release id, not master_id
    _year_overrides = {
        31380575: 1992,  # Blind Guardian — Somewhere Far Beyond Revisited
        27664872: 1987,  # Cavalera — Schizophrenia
        25117573: 1984,  # Sodom, Hellhammer — In The Sign Of Evil / Apocalyptic Raids
    }
    for r in releases:
        if r["id"] in _year_overrides:
            r["master_year"] = _year_overrides[r["id"]]

    return releases


# ── Merge with existing data ──────────────────────────────────────────────────

def merge_with_existing(new_releases):
    if not COLLECTION_FILE.exists():
        return new_releases, len(new_releases), 0

    existing = json.loads(COLLECTION_FILE.read_text())
    existing_ids = {r["id"] for r in existing}
    new_ids = {r["id"] for r in new_releases}

    added = len(new_ids - existing_ids)
    removed = len(existing_ids - new_ids)

    if added:
        print(f"\n✅ {added} new album(s) added since last sync!")
    if removed:
        print(f"  🗑  {removed} album(s) removed from collection.")

    return new_releases, added, removed

# ── Fetch median prices ──────────────────────────────────────────────────────

def enrich_prices(releases):
    # Load cached prices from previous run
    price_cache = {}
    if COLLECTION_FILE.exists():
        try:
            existing = json.loads(COLLECTION_FILE.read_text())
            for r in existing:
                rid = r.get("id")
                if rid and r.get("median_price") is not None:
                    price_cache[rid] = r["median_price"]
        except Exception:
            pass

    to_fetch = [r for r in releases if r["id"] not in price_cache]

    if to_fetch:
        print(f"\n💰 Fetching median prices ({len(to_fetch)} releases)...")
        for i, r in enumerate(to_fetch):
            rid = r["id"]
            try:
                url = f"https://api.discogs.com/marketplace/stats/{rid}?curr_abbr=USD"
                data = fetch_json(url)
                median = None
                if data.get("lowest_price") and data["lowest_price"].get("value"):
                    median = round(data["lowest_price"]["value"], 2)
                price_cache[rid] = median
                if median:
                    print(f"  [{i+1}/{len(to_fetch)}] {r['artist']} — {r['title']}: ${median}")
                else:
                    print(f"  [{i+1}/{len(to_fetch)}] {r['artist']} — {r['title']}: no price data")
            except Exception as e:
                print(f"  ⚠ {r['artist']} — {r['title']}: {e}")
                price_cache[rid] = None
            if i < len(to_fetch) - 1:
                time.sleep(REQUEST_DELAY)

    for r in releases:
        r["median_price"] = price_cache.get(r["id"])

    return releases

# ── Vinyl color dot ───────────────────────────────────────────────────────────

import re as _re

_COLOR_MAP = [
    (r'\bred\b',        '#c0392b'),
    (r'\bpink\b',       '#e91e8a'),
    (r'\bneon pink\b',  '#ff6ec7'),
    (r'\brose',         '#8b3a4a'),
    (r'\borchid',       '#b55dba'),
    (r'\bruby\b',       '#9b111e'),
    (r'\boxblood\b',    '#6a1a21'),
    (r'\bmagenta\b',    '#c4007a'),
    (r'\borange\b',     '#e67e22'),
    (r'\byellow\b',     '#f1c40f'),
    (r'\bcream\b',      '#f5e6c8'),
    (r'\bgold\b',       '#d4a537'),
    (r'\bbrown\b',      '#7b5b3a'),
    (r'\bgreen\b',      '#27ae60'),
    (r'\bolive\b',      '#6b8e23'),
    (r'\bforest\b',     '#228b22'),
    (r'\bcyan\b',       '#00bcd4'),
    (r'\bcuracao\b',    '#00a0b0'),
    (r'\bteal\b',       '#009688'),
    (r'\bblue\b',       '#2e86c1'),
    (r'\bsky blue\b',   '#87ceeb'),
    (r'\bpurple\b',     '#8e44ad'),
    (r'\bviolet\b',     '#7c3aed'),
    (r'\bwhite\b',      '#e8e4dc'),
    (r'\bclear\b',      'rgba(220,220,220,0.35)'),
    (r'\btransparent\b','rgba(220,220,220,0.35)'),
    (r'\bsilver\b',     '#aab2bd'),
    (r'\bgr[ae]y\b',    '#7f8c8d'),
    (r'\bgraphite\b',   '#5a5a5a'),
    (r'\bsmoke\b',      'rgba(60,60,60,0.7)'),
    (r'\bblack\b',      '#2c2c2c'),
    (r'\bbone\b',       '#e3dac9'),
    (r'\bmarble',       '#bbb'),
    (r'\bglow in the dark', '#b5f5b0'),
    (r'\bneon\b',       '#39ff14'),
    (r'\bsea glass\b',  '#8fbc8f'),
    (r'\brainbow\b',    'linear-gradient(90deg,#c0392b,#e67e22,#f1c40f,#27ae60,#2e86c1,#8e44ad)'),
]

def vinyl_dot_html(color_str):
    if not color_str:
        return ""
    for pattern, css_color in _COLOR_MAP:
        if _re.search(pattern, color_str, _re.IGNORECASE):
            return f'<span class="vinyl-dot" style="background:{css_color}"></span>'
    return ""

# ── Generate HTML ─────────────────────────────────────────────────────────────

def apply_custom_covers(releases):
    """Override local_cover with custom image if one exists in covers/custom/."""
    for r in releases:
        custom = _find_custom_cover(r["id"])
        if custom:
            r["local_cover"] = str(custom)
    return releases

def generate_html(releases, username, added_count):
    releases = apply_custom_covers(releases)
    now = datetime.now().strftime("%B %d, %Y at %H:%M")

    _sort_chars = str.maketrans("øłØŁ", "olOL")

    def artist_sort_key(name):
        low = name.lower().translate(_sort_chars)
        return low[4:] if low.startswith("the ") else low

    # Sort: artist (ignoring "The"), then by first release year, then title
    sorted_releases = sorted(releases, key=lambda r: (
        artist_sort_key(r["artist"]),
        r.get("master_year") or 9999,
        r["title"].lower(),
    ))

    # Group by first letter (ignoring leading "The ")
    groups = {}
    for r in sorted_releases:
        base = artist_sort_key(r["artist"])
        letter = base[0].upper() if base else "#"
        if not letter.isalpha():
            letter = "#"
        groups.setdefault(letter, []).append(r)

    # Build letter nav
    letters_html = ""
    for letter in sorted(groups.keys()):
        letters_html += f'<a href="#letter-{letter}" class="nav-letter">{letter}</a>'

    # Build album cards grouped by letter
    albums_html = ""
    card_idx = 0
    for letter in sorted(groups.keys()):
        albums_html += f'<div class="letter-group"><h2 class="letter-heading" id="letter-{letter}">{letter}</h2><div class="album-grid">'
        for r in groups[letter]:
            cover = r.get("local_cover", "")
            if cover:
                fallback = escape(r.get("thumb", ""))
                onerror = f' onerror="this.onerror=null;this.src=\'{fallback}\'"' if fallback else ""
                img_tag = f'<img src="{escape(cover)}" alt="{escape(r["title"])}" loading="lazy"{onerror}>'
            elif r.get("thumb"):
                img_tag = f'<img src="{escape(r["thumb"])}" alt="{escape(r["title"])}" loading="lazy">'
            else:
                initial = r["artist"][0].upper() if r["artist"] else "?"
                img_tag = f'<div class="cover-placeholder">{initial}</div>'

            display_year = r.get("master_year") or r.get("year") or ""
            year = f'<span class="year">{display_year}</span>' if display_year else ""
            display_genres = r.get("styles") or r.get("genres") or []
            genres = display_genres[0] if display_genres else ""
            genre_tag = f'<span class="genre">{escape(genres)}</span>' if genres else ""
            fmt_tag = ""
            color = r.get("vinyl_color", "")
            color_tag = f'<span class="vinyl-color">{vinyl_dot_html(color)}{escape(color)}</span>' if color else ""

            albums_html += f"""
            <div class="album-card" data-idx="{card_idx}">
                <div class="cover-wrap">{img_tag}</div>
                <div class="album-info">
                    <div class="album-title">{escape(r["title"])}</div>
                    <div class="album-artist">{escape(r["artist"])}</div>
                    <div class="album-meta">{year}{genre_tag}{fmt_tag}</div>
                    {color_tag}
                </div>
            </div>"""
            card_idx += 1
        albums_html += "</div></div>"

    badge = f'<div class="new-badge">+{added_count} new</div>' if added_count else ""

    # ── Stats ────────────────────────────────────────────────────────────────
    from collections import Counter

    # Special weights (box sets / double albums count as multiple positions)
    _weights = {
        ("Monster Magnet",                    "1993-2000"):        4,
        ("Bathory",                           "Nordland I & II"):  2,
        ("King Gizzard And The Lizard Wizard","K.G. / L.W."):      2,
        ("King Gizzard And The Lizard Wizard","K.G.L.W"):          2,
    }
    # Artist aliases (count releases under canonical name)
    _aliases = {
        "Cavalera": "Sepultura",
    }

    def release_weight(r):
        for (a, t_prefix), w in _weights.items():
            if r["artist"] == a and r["title"].startswith(t_prefix):
                return w
        return 1

    def canonical_artist(name):
        return _aliases.get(name, name)

    artist_counts = Counter()
    genre_counts  = Counter()
    year_counts   = Counter()
    for r in releases:
        w = release_weight(r)
        artist_counts[canonical_artist(r["artist"])] += w
        for g in (r.get("styles") or r.get("genres") or []):
            genre_counts[g] += w
        if r.get("master_year"):
            year_counts[r["master_year"]] += w
    total = sum(release_weight(r) for r in releases)

    def stat_rows(counter, n=10):
        items = counter.most_common(n)
        if not items:
            return ""
        max_val = items[0][1]
        rows = ""
        for label, count in items:
            pct = round(count / max_val * 100)
            rows += f"""<div class="stat-row">
              <span class="stat-label">{escape(str(label))}</span>
              <span class="stat-bar-wrap"><span class="stat-bar" style="width:{pct}%"></span></span>
              <span class="stat-count">{count}</span>
            </div>"""
        return rows

    stats_html = f"""
<section class="stats" id="stats">
  <h2 class="stats-heading">Collection Stats</h2>
  <div class="stats-grid">
    <div class="stat-block">
      <h3 class="stat-title">Top Artists</h3>
      {stat_rows(artist_counts)}
    </div>
    <div class="stat-block">
      <h3 class="stat-title">Top Genres</h3>
      {stat_rows(genre_counts)}
    </div>
    <div class="stat-block">
      <h3 class="stat-title">Most Popular Years</h3>
      {stat_rows(year_counts)}
    </div>
  </div>
</section>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Meg &amp; Adrian Vinyl Collection</title>
<link rel="icon" type="image/png" href="favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Ovo&family=Inter:wght@300;400;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0e0e0e;
    --surface: #181818;
    --border: #2a2a2a;
    --text: #e8e4dc;
    --muted: #999;
    --accent: #c8a96e;
    --accent2: #e8c88e;
    --red: #c0392b;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  html {{ scroll-behavior: smooth; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
  }}

  /* ── Header ── */
  header {{
    padding: 4rem 4rem 2rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
  }}

  .header-left h1 {{
    font-family: 'Ovo', serif;
    font-size: clamp(2.5rem, 6vw, 5rem);
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.02em;
    color: var(--text);
  }}

  .header-left h1 em {{
    color: var(--accent);
    font-style: italic;
  }}

  .h1-sub {{
    font-size: 0.38em;
    font-weight: 400;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--muted);
    font-family: 'Inter', sans-serif;
  }}

  .header-left .subtitle {{
    margin-top: 0.75rem;
    font-size: 0.8rem;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}

  .header-right {{
    text-align: right;
    font-size: 0.8rem;
    color: var(--muted);
    line-height: 1.8;
  }}

  .view-toggle-link {{
    display: inline;
    background: none;
    border: none;
    padding: 0;
    font-family: inherit;
    font-size: inherit;
    color: var(--muted);
    letter-spacing: inherit;
    text-transform: inherit;
    cursor: pointer;
    opacity: 0.6;
    transition: opacity 0.2s, color 0.2s;
  }}
  .view-toggle-link:hover {{ opacity: 1; color: var(--accent); }}
  .view-toggle-link.active {{ opacity: 1; color: var(--accent); }}

  .header-right .count {{
    font-family: 'Ovo', serif;
    font-size: 2rem;
    color: var(--accent);
    display: block;
    line-height: 1;
  }}

  .new-badge {{
    display: inline-block;
    background: var(--red);
    color: white;
    font-size: 0.8rem;
    padding: 0.2rem 0.5rem;
    letter-spacing: 0.05em;
    margin-top: 0.5rem;
  }}

  /* ── Letter nav ── */
  .letter-nav {{
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 0.75rem 4rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    align-items: center;
    justify-content: flex-start;
  }}

  .nav-letter {{
    color: var(--muted);
    text-decoration: none;
    font-size: 0.8rem;
    letter-spacing: 0.05em;
    padding: 0.25rem 0.4rem;
    transition: color 0.15s, background 0.15s;
  }}

  .nav-letter:hover {{
    color: var(--accent);
    background: rgba(200,169,110,0.08);
  }}
  .nav-stats {{
    margin-left: auto;
    color: var(--accent);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    font-size: 0.8rem;
    border: 1px solid rgba(200,169,110,0.45);
    padding: 0.2rem 0.6rem;
    min-height: 40px;
    display: inline-flex;
    align-items: center;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
  }}
  .nav-stats:hover, .nav-stats:focus-visible {{
    border-color: var(--accent);
    outline: none;
  }}

  .sort-controls {{
    display: flex;
    gap: 0.25rem;
    margin-right: 0.5rem;
    flex-shrink: 0;
  }}
  .sort-btn {{
    background: none;
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    min-height: 40px;
    cursor: pointer;
    transition: color 0.15s, background 0.15s, border-color 0.15s;
  }}
  .sort-btn.active {{
    color: #111;
    border-color: var(--accent);
    background: var(--accent);
  }}
  .sort-btn:hover:not(.active), .sort-btn:focus-visible:not(.active) {{
    border-color: var(--accent);
    outline: none;
  }}
  .nav-sep {{
    width: 1px;
    background: var(--border);
    margin: 0 0.5rem;
    flex-shrink: 0;
  }}

  .search-wrap {{
    position: relative;
    display: flex;
    align-items: center;
    margin-left: 0.5rem;
    gap: 0.35rem;
    flex-shrink: 0;
  }}
  #search {{
    background: var(--surface);
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem;
    padding: 0.3rem 1.2rem 0.3rem 0.5rem;
    min-height: 40px;
    width: 0;
    max-width: 180px;
    border-radius: 2px;
    outline: none;
    transition: width 0.2s, border-color 0.15s, opacity 0.2s;
    opacity: 0;
  }}
  #search.open {{
    width: 180px;
    opacity: 1;
  }}
  #search:focus {{
    border-color: var(--accent);
  }}
  #search::placeholder {{
    color: var(--muted);
    opacity: 0.6;
  }}
  .search-toggle {{
    background: none;
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    min-height: 40px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.3rem;
    transition: color 0.15s, background 0.15s, border-color 0.15s;
  }}
  .search-toggle:hover:not(.active), .search-toggle:focus-visible:not(.active) {{
    border-color: var(--accent);
    outline: none;
  }}
  .search-toggle.active {{
    color: #111;
    border-color: var(--accent);
    background: var(--accent);
  }}
  .search-clear {{
    position: absolute;
    right: 0.3rem;
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.8rem;
    padding: 0 0.2rem;
    display: none;
    line-height: 1;
  }}
  .search-clear.visible {{ display: block; }}
  .search-clear:hover {{ color: var(--accent); }}
  .search-count {{
    font-size: 0.8rem;
    color: var(--muted);
    margin-left: 0.4rem;
    white-space: nowrap;
    flex-shrink: 0;
  }}

  /* ── Content ── */
  .content {{
    padding: 3rem 4rem;
  }}

  .letter-group {{
    margin-bottom: 3.5rem;
  }}

  @keyframes fadeIn {{
    from {{ opacity: 0; }}
    to   {{ opacity: 1; }}
  }}
  .letter-heading {{
    font-family: 'Ovo', serif;
    font-size: 0.8rem;
    font-weight: 400;
    color: var(--accent);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 1.5rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
    animation: fadeIn 0.3s ease both;
  }}

  .album-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1.5rem;
  }}

  /* ── Album card ── */
  @keyframes cardIn {{
    from {{ opacity: 0; transform: translateY(12px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .album-card {{
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
    user-select: none;
    -webkit-user-select: none;
    animation: cardIn 0.3s ease both;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }}
  .album-card:hover {{
    transform: translateY(-3px);
  }}
  .album-card * {{
    pointer-events: none;
  }}

  .cover-wrap {{
    width: 100%;
    aspect-ratio: 1;
    overflow: hidden;
    background: var(--surface);
    margin-bottom: 0.75rem;
    position: relative;
    transition: transform 0.4s ease;
  }}

  .cover-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: filter 0.4s ease;
    filter: grayscale(15%);
  }}

  .album-card:hover .cover-wrap {{
    transform: scale(1.06) rotate(2deg);
  }}

  /* ── Covers-only view ── */
  body.covers-only .album-info {{ display: none; }}
  body.covers-only .cover-wrap {{ margin-bottom: 0; }}
  body.covers-only .album-grid {{
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 0.5rem;
  }}
  body.covers-only .letter-heading {{ display: none; }}
  body.covers-only .letter-group {{ margin-bottom: 0.5rem; }}

  .album-card:hover .cover-wrap img {{
    filter: grayscale(0%);
  }}

  .cover-placeholder {{
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Ovo', serif;
    font-size: 3rem;
    color: var(--muted);
    background: repeating-linear-gradient(
      45deg,
      var(--surface),
      var(--surface) 10px,
      #1a1a1a 10px,
      #1a1a1a 20px
    );
  }}

  .album-info {{
    line-height: 1.4;
  }}

  .album-title {{
    font-size: 0.8rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 400;
  }}

  .album-artist {{
    font-size: 0.8rem;
    color: var(--accent);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 0.15rem;
  }}

  .album-meta {{
    margin-top: 0.3rem;
    display: flex;
    flex-wrap: nowrap;
    gap: 0.4rem;
    align-items: center;
    min-width: 0;
    overflow: hidden;
  }}

  .year, .genre, .format {{
    font-size: 0.8rem;
    color: var(--muted);
    letter-spacing: 0.04em;
  }}

  .genre {{
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    max-width: 100%;
  }}

  .vinyl-color {{
    display: flex;
    align-items: center;
    gap: 0.3rem;
    margin-top: 0.25rem;
    font-size: 0.8rem;
    color: var(--muted);
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .vinyl-dot {{
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
    flex-shrink: 0;
    border: 1px solid rgba(255,255,255,0.15);
  }}

  .year::after {{ content: "·"; margin-left: 0.4rem; }}
  .year:last-child::after {{ content: ""; }}

  /* ── Footer ── */
  footer {{
    border-top: 1px solid var(--border);
    padding: 2rem 4rem;
    font-size: 0.8rem;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 1rem;
  }}

  /* ── Floating buttons ── */
  .btn-top {{
    position: fixed;
    bottom: 5rem;
    right: 2rem;
    z-index: 1001;
    min-height: 40px;
    background: var(--surface);
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    font-family: 'Inter', sans-serif;
    font-size: 1rem;
    line-height: 0;
    padding: 0.65rem 0.75rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, background 0.15s, opacity 0.25s ease, transform 0.25s ease;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    opacity: 0;
    transform: translateY(8px);
    pointer-events: none;
  }}
  .btn-top.visible {{
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
  }}
  .btn-top:hover, .btn-top:focus-visible {{
    border-color: var(--accent);
    outline: none;
  }}

  body.modal-open .btn-top {{ display: none; }}
  .modal-nav-random {{ display: none; }}

  /* ── Random button (floating) ── */
  .btn-random {{
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    z-index: 1001;
    min-height: 40px;
    background: var(--surface);
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 0.6rem 1.1rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, background 0.15s, box-shadow 0.15s;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }}
  .btn-random:hover, .btn-random:focus-visible {{
    border-color: var(--accent);
    outline: none;
  }}

  /* ── Modal ── */
  .modal-overlay {{
    display: flex;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.85);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    backdrop-filter: blur(4px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease;
  }}
  .modal-overlay.open {{
    opacity: 1;
    pointer-events: auto;
  }}
  .modal {{
    background: var(--surface);
    border: 1px solid var(--border);
    max-width: 780px;
    width: 100%;
    display: flex;
    gap: 0;
    position: relative;
    max-height: 90vh;
    overflow: hidden;
    transform: scale(0.96) translateY(8px);
    transition: transform 0.2s ease;
  }}
  .modal-overlay.open .modal {{
    transform: scale(1) translateY(0);
  }}
  .modal-cover {{
    width: 340px;
    min-width: 340px;
    aspect-ratio: 1;
    background: var(--bg);
    flex-shrink: 0;
    overflow: hidden;
    align-self: flex-start;
  }}
  .modal-cover img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }}
  @keyframes shimmer {{
    0%   {{ background-position: -200% 0; }}
    100% {{ background-position:  200% 0; }}
  }}
  .modal-cover.loading {{
    background: linear-gradient(90deg, var(--surface) 25%, #252525 50%, var(--surface) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.2s ease-in-out infinite;
  }}
  .modal-cover .cover-placeholder {{
    font-size: 6rem;
    border: none;
  }}
  .modal-body {{
    padding: 2.5rem 2rem 2rem;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    overflow-y: auto;
    flex: 1;
  }}
  .modal-title {{
    font-family: 'Ovo', serif;
    font-size: clamp(1.2rem, 3vw, 1.8rem);
    font-weight: 700;
    line-height: 1.2;
    color: var(--text);
    margin-bottom: 0.5rem;
  }}
  .modal-artist {{
    font-size: 0.9rem;
    color: var(--accent);
    letter-spacing: 0.05em;
    margin-bottom: 1.5rem;
  }}
  .modal-details {{
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    font-size: 0.8rem;
    color: var(--muted);
  }}
  .modal-details .row {{
    display: flex;
    gap: 0.75rem;
  }}
  .modal-details .label {{
    color: #777;
    width: 6.5rem;
    flex-shrink: 0;
    font-size: 0.8rem;
    padding-top: 0.05rem;
  }}
  .modal-details .value {{
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }}
  .modal-details .vinyl-dot {{
    width: 0.6rem;
    height: 0.6rem;
  }}
  .modal-close {{
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: rgba(0,0,0,0.4);
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    font-size: 1rem;
    cursor: pointer;
    line-height: 1;
    min-height: 40px;
    min-width: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    z-index: 10;
  }}
  .modal-close:hover, .modal-close:focus-visible {{
    border-color: var(--accent);
    outline: none;
  }}
  .modal-nav-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
    gap: 1rem;
  }}
  .modal-nav-btn {{
    background: none;
    border: 1px solid rgba(200,169,110,0.45);
    color: var(--accent);
    cursor: pointer;
    padding: 0.35rem 0.7rem;
    min-height: 40px;
    line-height: 0;
    transition: color 0.15s, border-color 0.15s, background 0.15s;
    flex-shrink: 0;
  }}
  .modal-nav-btn:hover:not(:disabled), .modal-nav-btn:focus-visible:not(:disabled) {{
    border-color: var(--accent);
    outline: none;
  }}
  .modal-nav-btn:disabled {{
    opacity: 0.25;
    cursor: default;
  }}
  .modal-pos {{ display: none; }}

  /* ── Stats ── */
  .stats {{
    padding: 3rem 4rem 4rem;
    border-top: 1px solid var(--border);
  }}

  .stats-heading {{
    font-family: 'Ovo', serif;
    font-size: 0.8rem;
    font-weight: 400;
    color: var(--accent);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 2rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}

  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 3rem;
  }}

  .stat-title {{
    font-size: 0.8rem;
    font-weight: 400;
    color: var(--muted);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 1.25rem;
  }}

  .stat-row {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.6rem;
  }}

  .stat-label {{
    font-size: 0.8rem;
    color: var(--text);
    width: 9rem;
    flex-shrink: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .stat-bar-wrap {{
    flex: 1;
    height: 2px;
    background: var(--border);
  }}

  .stat-bar {{
    display: block;
    height: 100%;
    background: var(--accent);
    opacity: 0.6;
  }}

  .stat-count {{
    font-size: 0.8rem;
    color: var(--muted);
    width: 1.5rem;
    text-align: right;
    flex-shrink: 0;
  }}



  @media (max-width: 700px) {{
    /* Header */
    header {{
      padding: 2rem 1.25rem 1.25rem;
      flex-direction: column;
      align-items: flex-start;
      gap: 0.75rem;
    }}
    .header-right {{
      text-align: left;
    }}
    .header-left h1 {{
      font-size: clamp(2rem, 11vw, 3rem);
    }}

    /* Nav — two-row layout on mobile */
    .letter-nav {{
      padding: 0.5rem 1.25rem;
      gap: 0.4rem 0.15rem;
    }}
    .sort-controls {{ order: 1; flex-shrink: 0; }}
    .nav-sep {{ display: none; }}
    .search-wrap {{ order: 2; margin-left: 0; }}
    #search.open {{ width: 120px; max-width: 120px; }}
    .search-count {{ order: 3; }}
    .nav-stats {{ order: 4; margin-left: 0; }}
    #nav-letters {{
      order: 5;
      width: 100%;
      display: flex;
      flex-wrap: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      gap: 0;
    }}
    #nav-letters::-webkit-scrollbar {{ display: none; }}
    .nav-letter {{
      font-size: 0.8rem;
      padding: 0.2rem 0.3rem;
      flex-shrink: 0;
    }}

    /* Content */
    .content {{ padding: 1.5rem 1.25rem; }}
    .album-grid {{ grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 1rem; }}

    /* Footer */
    footer {{ padding: 1.5rem 1.25rem; }}

    /* Random button */
    .btn-random {{
      bottom: 1.25rem;
      right: 1rem;
      padding: 0.5rem 0.9rem;
    }}
    .btn-top {{
      bottom: 4.5rem;
      right: 1rem;
    }}
    body.modal-open > .btn-random {{ display: none; }}
    .modal-nav-random {{
      display: inline-block;
      position: static;
      box-shadow: none;
    }}

    /* Modal — full screen on mobile */
    .modal-overlay {{ padding: 0; }}
    .modal {{
      flex-direction: column;
      width: 100%;
      height: 100%;
      max-height: 100%;
      border: none;
    }}
    .modal-cover {{
      width: 100%;
      min-width: unset;
      height: 100vw;
      max-height: 45vh;
      flex-shrink: 0;
    }}
    .modal-body {{
      padding: 1.25rem 1.25rem 2rem;
      overflow-y: auto;
      flex: 1;
    }}
    .modal-title {{ font-size: 1.2rem; }}
    .stats {{ padding: 2rem 1.25rem 3rem; }}
    .stats-grid {{ grid-template-columns: 1fr; gap: 2rem; }}
    .stat-label {{ width: 7rem; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1><em>Meg &amp; Adrian</em><br><span class="h1-sub">Vinyl Collection</span></h1>
    <p class="subtitle">Discogs collection · synced {now} · <button id="view-toggle" class="view-toggle-link" onclick="toggleCoversView()">covers</button></p>
  </div>
  <div class="header-right">
    <span class="count">{total}</span>
    albums
    {badge}
  </div>
</header>

<button class="btn-random" onclick="openRandom()">Random</button>
<button class="btn-top" onclick="window.scrollTo({{top:0,behavior:'smooth'}})" id="btn-top"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3,11 8,5 13,11"/></svg></button>

<nav class="letter-nav">
  <div id="sort-controls" class="sort-controls">
    <button class="sort-btn active" data-sort="artist" onclick="setSort('artist')">A–Z</button>
    <button class="sort-btn" data-sort="year" onclick="setSort('year')">Year</button>
    <button class="sort-btn" data-sort="price" onclick="setSort('price')">Price</button>
    <button class="sort-btn" data-sort="added" onclick="setSort('added')">New</button>
  </div>
  <div class="search-wrap">
    <button class="search-toggle" onclick="toggleSearch()" title="Search">Search</button>
    <input type="text" id="search" placeholder="Search..." autocomplete="off" spellcheck="false">
    <button class="search-clear" id="search-clear" onclick="clearSearch()">&times;</button>
  </div>
  <span class="search-count" id="search-count"></span>
  <span class="nav-sep"></span>
  <span id="nav-letters">{letters_html}</span>
  <a href="#stats" class="nav-letter nav-stats">Stats</a>
</nav>

<main class="content" id="content">
  {albums_html}
</main>

{stats_html}

<footer>
  <span>Generated from <a href="https://www.discogs.com/user/{escape(username)}/collection" style="color:var(--accent);text-decoration:none">discogs.com/user/{escape(username)}/collection</a></span>
  <span>Last sync: {now}</span>
</footer>

<!-- Modal -->
<div class="modal-overlay" id="modal" onclick="handleOverlayClick(event)">
  <div class="modal" id="modal-box">
    <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    <div class="modal-cover" id="modal-cover"></div>
    <div class="modal-body">
      <div>
        <div class="modal-title" id="modal-title"></div>
        <div class="modal-artist" id="modal-artist"></div>
        <div class="modal-details" id="modal-details"></div>
      </div>
      <div class="modal-nav-row">
        <button class="modal-nav-btn" id="modal-prev" onclick="modalNav(-1)"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9,2 4,7 9,12"/></svg></button>
        <button class="modal-nav-random btn-random" onclick="openRandom()">Random</button>
        <span class="modal-pos" id="modal-pos"></span>
        <button class="modal-nav-btn" id="modal-next" onclick="modalNav(1)"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="5,2 10,7 5,12"/></svg></button>
      </div>
    </div>
  </div>
</div>

<script>
const COLLECTION = {json.dumps(sorted_releases, ensure_ascii=False)};
COLLECTION.forEach((r, i) => r._idx = i);

// ── Helpers ──────────────────────────────────────────────────────────────────

function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function artistSortKey(name) {{
  return (name||'').replace(/[øØ]/g,'o').replace(/[łŁ]/g,'l')
    .toLowerCase().replace(/^the /, '');
}}

const _colorMap = [
  [/\\bred\\b/i,        '#c0392b'],
  [/\\bpink\\b/i,       '#e91e8a'],
  [/\\bneon pink\\b/i,  '#ff6ec7'],
  [/\\brose/i,         '#8b3a4a'],
  [/\\borchid/i,       '#b55dba'],
  [/\\bruby\\b/i,       '#9b111e'],
  [/\\boxblood\\b/i,    '#6a1a21'],
  [/\\bmagenta\\b/i,    '#c4007a'],
  [/\\borange\\b/i,     '#e67e22'],
  [/\\byellow\\b/i,     '#f1c40f'],
  [/\\bcream\\b/i,       '#f5e6c8'],
  [/\\bgold\\b/i,       '#d4a537'],
  [/\\bbrown\\b/i,      '#7b5b3a'],
  [/\\bgreen\\b/i,      '#27ae60'],
  [/\\bolive\\b/i,      '#6b8e23'],
  [/\\bforest\\b/i,     '#228b22'],
  [/\\bcyan\\b/i,       '#00bcd4'],
  [/\\bcuracao\\b/i,    '#00a0b0'],
  [/\\bteal\\b/i,       '#009688'],
  [/\\bblue\\b/i,       '#2e86c1'],
  [/\\bsky blue\\b/i,   '#87ceeb'],
  [/\\bpurple\\b/i,     '#8e44ad'],
  [/\\bviolet\\b/i,     '#7c3aed'],
  [/\\bwhite\\b/i,      '#e8e4dc'],
  [/\\bclear\\b/i,      'rgba(220,220,220,0.35)'],
  [/\\btransparent\\b/i,'rgba(220,220,220,0.35)'],
  [/\\bsilver\\b/i,     '#aab2bd'],
  [/\\bgr[ae]y\\b/i,    '#7f8c8d'],
  [/\\bgraphite\\b/i,   '#5a5a5a'],
  [/\\bsmoke\\b/i,      'rgba(60,60,60,0.7)'],
  [/\\bblack\\b/i,      '#2c2c2c'],
  [/\\bbone\\b/i,       '#e3dac9'],
  [/\\bmarble/i,       '#bbb'],
  [/\\bglow in the dark/i, '#b5f5b0'],
  [/\\bneon\\b/i,       '#39ff14'],
  [/\\bsea glass\\b/i,  '#8fbc8f'],
  [/\\brainbow\\b/i,    'linear-gradient(90deg,#c0392b,#e67e22,#f1c40f,#27ae60,#2e86c1,#8e44ad)'],
];

function vinylCss(str) {{
  if (!str) return null;
  for (const [re, color] of _colorMap) {{
    if (re.test(str)) return color;
  }}
  return null;
}}

function dotHtml(str) {{
  const c = vinylCss(str);
  if (!c) return '';
  const style = 'background:' + c;
  return '<span class="vinyl-dot" style="' + style + '"></span>';
}}

// ── Card renderer ─────────────────────────────────────────────────────────────

function renderCard(r) {{
  const coverSrc = r.local_cover || r.thumb || '';
  const onerr = (r.local_cover && r.thumb) ? ` onerror="this.onerror=null;this.src='${{esc(r.thumb)}}'"` : '';
  const coverHtml = coverSrc
    ? '<img src="' + esc(coverSrc) + '" alt="' + esc(r.title) + '" loading="lazy"' + onerr + '>'
    : '<div class="cover-placeholder">' + ((r.artist||'?')[0].toUpperCase()) + '</div>';
  const yr = r.master_year || r.year || '';
  const yearHtml  = yr ? '<span class="year">' + yr + '</span>' : '';
  const genreList = (r.styles && r.styles.length ? r.styles : r.genres) || [];
  const genres    = genreList.length ? genreList[0] : '';
  const genreHtml = genres ? '<span class="genre">' + esc(genres) + '</span>' : '';
  const fmtHtml   = '';
  const colorHtml = r.vinyl_color ? '<span class="vinyl-color">' + dotHtml(r.vinyl_color) + esc(r.vinyl_color) + '</span>' : '';
  return '<div class="album-card" data-idx="' + r._idx + '">' +
    '<div class="cover-wrap">' + coverHtml + '</div>' +
    '<div class="album-info">' +
      '<div class="album-title">' + esc(r.title) + '</div>' +
      '<div class="album-artist">' + esc(r.artist) + '</div>' +
      '<div class="album-meta">' + yearHtml + genreHtml + fmtHtml + '</div>' +
      colorHtml +
    '</div></div>';
}}

// ── Group renderer ────────────────────────────────────────────────────────────

function applyGroups(map, keys, labelFn, idFn) {{
  // Rebuild nav letters
  const navLetters = document.getElementById('nav-letters');
  navLetters.innerHTML = keys.map(k =>
    '<a href="#' + idFn(k) + '" class="nav-letter">' + esc(labelFn(k)) + '</a>'
  ).join('');

  // Rebuild content
  document.getElementById('content').innerHTML = keys.map(k =>
    '<div class="letter-group">' +
    '<h2 class="letter-heading" id="' + idFn(k) + '">' + esc(labelFn(k)) + '</h2>' +
    '<div class="album-grid">' + map[k].map(renderCard).join('') + '</div>' +
    '</div>'
  ).join('');

  bindCards();
}}

// ── Search ────────────────────────────────────────────────────────────────────

let _searchQuery = '';
let _currentSort = 'artist';

function toggleCoversView() {{
  const isCovers = document.body.classList.toggle('covers-only');
  const btn = document.getElementById('view-toggle');
  btn.classList.toggle('active', isCovers);
  btn.textContent = isCovers ? 'grid' : 'covers';
}}

function toggleSearch() {{
  const input = document.getElementById('search');
  const btn = document.querySelector('.search-toggle');
  const isOpen = input.classList.toggle('open');
  btn.classList.toggle('active', isOpen);
  if (isOpen) {{
    input.focus();
  }} else {{
    if (_searchQuery) clearSearch();
  }}
}}

function clearSearch() {{
  const input = document.getElementById('search');
  input.value = '';
  _searchQuery = '';
  document.getElementById('search-clear').classList.remove('visible');
  document.getElementById('search-count').textContent = '';
  rerender();
}}

function getFiltered() {{
  if (!_searchQuery) return COLLECTION;
  const q = _searchQuery.toLowerCase();
  return COLLECTION.filter(r => {{
    const artist = (r.artist || '').toLowerCase();
    const title  = (r.title || '').toLowerCase();
    const firstRelease = String(r.master_year || '');
    const details = (r.vinyl_color || '').toLowerCase();
    const styles = (r.styles || []).join(' ').toLowerCase();
    const genres = (r.genres || []).join(' ').toLowerCase();
    return artist.includes(q) || title.includes(q) || firstRelease.includes(q) || details.includes(q) || styles.includes(q) || genres.includes(q);
  }});
}}

document.getElementById('search').addEventListener('input', function() {{
  _searchQuery = this.value.trim();
  document.getElementById('search-clear').classList.toggle('visible', _searchQuery.length > 0);
  const filtered = getFiltered();
  if (_searchQuery) {{
    document.getElementById('search-count').textContent = filtered.length + ' / ' + COLLECTION.length;
  }} else {{
    document.getElementById('search-count').textContent = '';
  }}
  rerender();
}});

document.getElementById('search').addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') {{
    e.stopPropagation();
    if (_searchQuery) {{ clearSearch(); }} else {{ toggleSearch(); }}
  }}
}});

// ── Sort modes ────────────────────────────────────────────────────────────────

function renderByArtist() {{
  const source = getFiltered();
  const sorted = [...source].sort((a, b) => {{
    const ka = artistSortKey(a.artist), kb = artistSortKey(b.artist);
    if (ka !== kb) return ka.localeCompare(kb);
    const ya = a.master_year || a.year || 9999;
    const yb = b.master_year || b.year || 9999;
    return ya !== yb ? ya - yb : a.title.toLowerCase().localeCompare(b.title.toLowerCase());
  }});
  const map = {{}};
  sorted.forEach(r => {{
    let l = artistSortKey(r.artist)[0] || '#';
    l = /[a-z]/i.test(l) ? l.toUpperCase() : '#';
    (map[l] = map[l] || []).push(r);
  }});
  const keys = Object.keys(map).sort((a, b) => a==='#'?1 : b==='#'?-1 : a.localeCompare(b));
  applyGroups(map, keys, k => k, k => 'letter-' + k);
}}

function renderByYear() {{
  const source = getFiltered();
  const sorted = [...source].sort((a, b) => {{
    const ya = a.master_year || a.year || 9999;
    const yb = b.master_year || b.year || 9999;
    return ya !== yb ? ya - yb : artistSortKey(a.artist).localeCompare(artistSortKey(b.artist));
  }});
  const map = {{}};
  sorted.forEach(r => {{
    const y = r.master_year || r.year;
    const key = y ? String(Math.floor(y / 10) * 10) + 's' : '—';
    (map[key] = map[key] || []).push(r);
  }});
  const keys = Object.keys(map).sort((a, b) => a==='—'?1 : b==='—'?-1 : parseInt(a)-parseInt(b));
  applyGroups(map, keys, k => k, k => 'decade-' + k.replace(/[^a-z0-9]/gi, '-'));
}}

function renderByPrice() {{
  const source = getFiltered();
  const sorted = [...source].sort((a, b) => {{
    const pa = a.median_price || 0;
    const pb = b.median_price || 0;
    if (pa !== pb) return pb - pa;
    return artistSortKey(a.artist).localeCompare(artistSortKey(b.artist));
  }});
  const map = {{}};
  sorted.forEach(r => {{
    const p = r.median_price;
    let key;
    if (!p)        key = '—';
    else if (p >= 100) key = '$100+';
    else if (p >= 50)  key = '$50–99';
    else if (p >= 25)  key = '$25–49';
    else if (p >= 10)  key = '$10–24';
    else               key = 'Under $10';
    (map[key] = map[key] || []).push(r);
  }});
  const order = ['$100+', '$50–99', '$25–49', '$10–24', 'Under $10', '—'];
  const keys = order.filter(k => map[k]);
  applyGroups(map, keys, k => k, k => 'price-' + k.replace(/[^a-z0-9]/gi, '-'));
}}

function renderByAdded() {{
  const source = getFiltered();
  const sorted = [...source].sort((a, b) => {{
    const da = a.date_added || '';
    const db = b.date_added || '';
    if (da !== db) return db.localeCompare(da);
    return artistSortKey(a.artist).localeCompare(artistSortKey(b.artist));
  }});
  const map = {{}};
  sorted.forEach(r => {{
    const key = r.date_added ? r.date_added.slice(0, 4) : '—';
    (map[key] = map[key] || []).push(r);
  }});
  const order = Object.keys(map).sort((a, b) => b.localeCompare(a));
  applyGroups(map, order, k => k, k => 'added-' + k.replace(/[^a-z0-9]/gi, '-'));
}}

function setSort(mode) {{
  _currentSort = mode;
  document.querySelectorAll('.sort-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sort === mode)
  );
  rerender();
}}

function rerender() {{
  if (_currentSort === 'year') renderByYear();
  else if (_currentSort === 'price') renderByPrice();
  else if (_currentSort === 'added') renderByAdded();
  else renderByArtist();
}}

// ── Modal ─────────────────────────────────────────────────────────────────────

let _displayOrder = [];
let _modalPos = 0;

function openModal(idx) {{
  _displayOrder = Array.from(document.querySelectorAll('.album-card'))
    .map(c => parseInt(c.dataset.idx));
  _modalPos = _displayOrder.indexOf(idx);
  if (_modalPos === -1) _modalPos = 0;
  showModal(_displayOrder[_modalPos]);
}}

let _pendingCoverImg = null;

function showModal(idx) {{
  const r = COLLECTION[idx];
  const cover = document.getElementById('modal-cover');
  const mSrc = r.local_cover || r.thumb || '';

  // Cancel any in-flight image load so its onload can't overwrite the new cover
  if (_pendingCoverImg) {{
    _pendingCoverImg.onload = null;
    _pendingCoverImg.onerror = null;
    _pendingCoverImg = null;
  }}

  cover.innerHTML = '';
  cover.classList.add('loading');

  if (!mSrc) {{
    cover.innerHTML = '<div class="cover-placeholder">' + ((r.artist||'?')[0].toUpperCase()) + '</div>';
    cover.classList.remove('loading');
  }} else {{
    const img = new Image();
    img.style.cssText = 'width:100%;height:100%;object-fit:contain;display:block;';
    img.alt = '';
    _pendingCoverImg = img;
    img.onload = () => {{
      if (_pendingCoverImg !== img) return;
      _pendingCoverImg = null;
      cover.innerHTML = '';
      cover.appendChild(img);
      cover.classList.remove('loading');
    }};
    img.onerror = () => {{
      if (_pendingCoverImg !== img) return;
      // local cover missing (e.g. gitignored) — fall back to Discogs URL
      if (img.src !== r.thumb && r.thumb) {{
        img.src = r.thumb;
      }} else {{
        _pendingCoverImg = null;
        cover.classList.remove('loading');
      }}
    }};
    img.src = mSrc;
  }}
  document.getElementById('modal-title').textContent  = r.title;
  document.getElementById('modal-artist').textContent = r.artist;
  const rows = [];
  if (r.master_year)                 rows.push(['First Release', r.master_year]);
  if (r.year)                        rows.push(['Release Year',  r.year]);
  const modalGenres = (r.styles && r.styles.length ? r.styles : r.genres) || [];
  if (modalGenres.length)            rows.push(['Genre',  modalGenres.join(', ')]);
  if (r.formats && r.formats.length) rows.push(['Format', r.formats.join(', ')]);
  if (r.vinyl_color)                 rows.push(['Details', r.vinyl_color, true]);
  if (r.median_price)                rows.push(['Median Price', '$' + Math.round(r.median_price)]);
  if (r.date_added) {{
    const d = new Date(r.date_added);
    const dateStr = d.toLocaleDateString('en-US', {{ year: 'numeric', month: 'short', day: 'numeric' }});
    rows.push(['Date Added', dateStr]);
  }}
  document.getElementById('modal-details').innerHTML = rows
    .map(([l, v, isDot]) => '<div class="row"><span class="label">' + l + '</span><span class="value">' + (isDot ? dotHtml(v) : '') + esc(v) + '</span></div>')
    .join('');
  document.getElementById('modal-pos').textContent = (_modalPos + 1) + ' / ' + _displayOrder.length;
  document.getElementById('modal-prev').disabled = _modalPos <= 0;
  document.getElementById('modal-next').disabled = _modalPos >= _displayOrder.length - 1;
  document.getElementById('modal').classList.add('open');
  document.body.classList.add('modal-open');
  document.documentElement.style.overflow = 'hidden';
  document.body.style.overflow = 'hidden';
}}

function modalNav(dir) {{
  const next = _modalPos + dir;
  if (next < 0 || next >= _displayOrder.length) return;
  _modalPos = next;
  showModal(_displayOrder[_modalPos]);
}}

function closeModal() {{
  document.getElementById('modal').classList.remove('open');
  document.body.classList.remove('modal-open');
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
}}

function handleOverlayClick(e) {{
  if (e.target === document.getElementById('modal')) closeModal();
}}

function openRandom() {{
  openModal(Math.floor(Math.random() * COLLECTION.length));
}}

// ── Init ──────────────────────────────────────────────────────────────────────

// Single delegated listener — survives re-renders, works on iOS
document.getElementById('content').addEventListener('click', function(e) {{
  const card = e.target.closest('.album-card');
  if (card) openModal(parseInt(card.dataset.idx));
}});

// No-op bindCards kept so applyGroups() calls don't error
function bindCards() {{}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeModal();
  if (document.getElementById('modal').classList.contains('open')) {{
    if (e.key === 'ArrowLeft')  modalNav(-1);
    if (e.key === 'ArrowRight') modalNav(1);
  }}
}});

const btnTop = document.getElementById('btn-top');
window.addEventListener('scroll', () => {{
  btnTop.classList.toggle('visible', window.scrollY > 400);
}}, {{passive: true}});
</script>

</body>
</html>"""

    return html

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 sync.py YOUR_DISCOGS_USERNAME [FOLDER_ID] [TOKEN]")
        sys.exit(1)

    username = sys.argv[1].strip()
    folder_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    token_arg = sys.argv[3].strip() if len(sys.argv) > 3 else ""

    global DISCOGS_TOKEN
    DISCOGS_TOKEN = token_arg or os.environ.get("DISCOGS_TOKEN", "")

    releases = fetch_collection(username, folder_id)
    releases = enrich_master_years(releases)
    releases, added, removed = merge_with_existing(releases)
    releases = download_covers(releases)
    releases = enrich_prices(releases)

    # Save JSON database
    COLLECTION_FILE.write_text(json.dumps(releases, indent=2, ensure_ascii=False))
    print(f"\n💾 Saved {len(releases)} releases to {COLLECTION_FILE}")

    # Generate HTML
    html = generate_html(releases, username, added)
    HTML_FILE.write_text(html, encoding="utf-8")
    print(f"🌐 Generated {HTML_FILE}")
    print(f"\n✨ Done! Open index.html in your browser.")

if __name__ == "__main__":
    main()
