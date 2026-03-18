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

# ── Generate HTML ─────────────────────────────────────────────────────────────

def generate_html(releases, username, added_count):
    now = datetime.now().strftime("%B %d, %Y at %H:%M")
    total = len(releases)

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
            genres = ", ".join(r["genres"][:2]) if r["genres"] else ""
            genre_tag = f'<span class="genre">{escape(genres)}</span>' if genres else ""
            fmt_tag = ""
            color = r.get("vinyl_color", "")
            color_tag = f'<span class="vinyl-color">{escape(color)}</span>' if color else ""

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
    for r in releases:
        artist_counts[canonical_artist(r["artist"])] += release_weight(r)

    genre_counts  = Counter(g for r in releases for g in r.get("genres", []))
    year_counts   = Counter(r["master_year"] for r in releases if r.get("master_year"))

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
<section class="stats">
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Mono:wght@300;400&display=swap" rel="stylesheet">
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
    font-family: 'DM Mono', monospace;
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
    font-family: 'Playfair Display', serif;
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
    font-family: 'DM Mono', monospace;
  }}

  .header-left .subtitle {{
    margin-top: 0.75rem;
    font-size: 0.75rem;
    color: var(--muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}

  .header-right {{
    text-align: right;
    font-size: 0.7rem;
    color: var(--muted);
    line-height: 1.8;
  }}

  .header-right .count {{
    font-family: 'Playfair Display', serif;
    font-size: 2rem;
    color: var(--accent);
    display: block;
    line-height: 1;
  }}

  .new-badge {{
    display: inline-block;
    background: var(--red);
    color: white;
    font-size: 0.65rem;
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
  }}

  .nav-letter {{
    color: var(--muted);
    text-decoration: none;
    font-size: 0.7rem;
    letter-spacing: 0.05em;
    padding: 0.25rem 0.4rem;
    transition: color 0.15s, background 0.15s;
  }}

  .nav-letter:hover {{
    color: var(--accent);
    background: rgba(200,169,110,0.08);
  }}

  .sort-controls {{
    display: flex;
    gap: 0.25rem;
    margin-right: 0.5rem;
    flex-shrink: 0;
  }}
  .sort-btn {{
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    padding: 0.2rem 0.55rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, background 0.15s;
  }}
  .sort-btn.active {{
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(200,169,110,0.07);
  }}
  .sort-btn:hover:not(.active) {{
    color: var(--text);
    border-color: #444;
  }}
  .nav-sep {{
    width: 1px;
    background: var(--border);
    margin: 0 0.5rem;
    flex-shrink: 0;
  }}

  /* ── Content ── */
  .content {{
    padding: 3rem 4rem;
  }}

  .letter-group {{
    margin-bottom: 3.5rem;
  }}

  .letter-heading {{
    font-family: 'Playfair Display', serif;
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--accent);
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-bottom: 1.5rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
  }}

  .album-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1.5rem;
  }}

  /* ── Album card ── */
  .album-card {{
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
    user-select: none;
    -webkit-user-select: none;
  }}
  .album-card * {{
    pointer-events: none;
  }}

  .cover-wrap {{
    width: 100%;
    aspect-ratio: 1;
    overflow: hidden;
    background: var(--surface);
    border: 1px solid var(--border);
    margin-bottom: 0.75rem;
    position: relative;
  }}

  .cover-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 0.4s ease, filter 0.4s ease;
    filter: grayscale(15%);
  }}

  .album-card:hover .cover-wrap img {{
    transform: scale(1.04);
    filter: grayscale(0%);
  }}

  .cover-placeholder {{
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'Playfair Display', serif;
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
    font-size: 0.75rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 400;
  }}

  .album-artist {{
    font-size: 0.7rem;
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
    font-size: 0.6rem;
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
    display: block;
    margin-top: 0.25rem;
    font-size: 0.6rem;
    color: var(--muted);
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .year::after {{ content: "·"; margin-left: 0.4rem; }}
  .year:last-child::after {{ content: ""; }}

  /* ── Footer ── */
  footer {{
    border-top: 1px solid var(--border);
    padding: 2rem 4rem;
    font-size: 0.65rem;
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
    background: var(--surface);
    border: 1px solid var(--border);
    color: #aaa;
    font-family: 'DM Mono', monospace;
    font-size: 1rem;
    line-height: 0;
    padding: 0.65rem 0.75rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, opacity 0.2s;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    opacity: 0;
    pointer-events: none;
  }}
  .btn-top.visible {{
    opacity: 1;
    pointer-events: auto;
  }}
  .btn-top:hover {{
    color: var(--accent);
    border-color: var(--accent);
  }}

  /* ── Random button (floating) ── */
  .btn-random {{
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    z-index: 1001;
    background: var(--surface);
    border: 1px solid var(--border);
    color: #aaa;
    font-family: 'DM Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 0.6rem 1.1rem;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s, box-shadow 0.15s;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }}
  .btn-random:hover {{
    color: var(--accent);
    border-color: var(--accent);
    box-shadow: 0 4px 24px rgba(200,169,110,0.15);
  }}

  /* ── Modal ── */
  .modal-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.85);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    backdrop-filter: blur(4px);
  }}
  .modal-overlay.open {{
    display: flex;
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
  }}
  .modal-cover {{
    width: 340px;
    min-width: 340px;
    aspect-ratio: 1;
    background: var(--bg);
    flex-shrink: 0;
    overflow: hidden;
  }}
  .modal-cover img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
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
    font-family: 'Playfair Display', serif;
    font-size: clamp(1.2rem, 3vw, 1.8rem);
    font-weight: 700;
    line-height: 1.2;
    color: var(--text);
    margin-bottom: 0.5rem;
  }}
  .modal-artist {{
    font-size: 0.8rem;
    color: var(--accent);
    letter-spacing: 0.05em;
    margin-bottom: 1.5rem;
  }}
  .modal-details {{
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    font-size: 0.7rem;
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
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-size: 0.6rem;
    padding-top: 0.05rem;
  }}
  .modal-details .value {{
    color: var(--text);
  }}
  .modal-close {{
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    background: none;
    border: none;
    color: var(--muted);
    font-size: 1.1rem;
    cursor: pointer;
    line-height: 1;
    padding: 0.25rem 0.5rem;
    transition: color 0.15s;
  }}
  .modal-close:hover {{ color: var(--text); }}
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
    border: 1px solid var(--border);
    color: var(--muted);
    cursor: pointer;
    padding: 0.35rem 0.7rem;
    line-height: 0;
    transition: color 0.15s, border-color 0.15s;
    flex-shrink: 0;
  }}
  .modal-nav-btn:hover:not(:disabled) {{
    color: var(--accent);
    border-color: var(--accent);
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
    font-family: 'Playfair Display', serif;
    font-size: 0.75rem;
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
    font-size: 0.65rem;
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
    font-size: 0.68rem;
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
    font-size: 0.65rem;
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

    /* Nav */
    .letter-nav {{
      padding: 0.6rem 1.25rem;
      gap: 0.15rem;
    }}
    .nav-letter {{
      font-size: 0.65rem;
      padding: 0.2rem 0.3rem;
    }}

    /* Content */
    .content {{ padding: 1.5rem 1.25rem; }}
    .album-grid {{ grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 1rem; }}

    /* Footer */
    footer {{ padding: 1.5rem 1.25rem; }}

    /* Random button */
    .btn-random {{
      bottom: 1.25rem;
      right: 1.25rem;
      padding: 0.5rem 0.9rem;
    }}

    /* Modal */
    .modal-overlay {{ padding: 0; align-items: flex-end; }}
    .modal {{
      flex-direction: column;
      max-height: 92vh;
      width: 100%;
      border-bottom: none;
    }}
    .modal-cover {{
      width: 100%;
      min-width: unset;
      aspect-ratio: unset;
      height: 45vw;
      max-height: 260px;
    }}
    .modal-body {{
      padding: 1.25rem 1.25rem 2rem;
      overflow-y: auto;
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
    <p class="subtitle">Discogs collection · synced {now}</p>
  </div>
  <div class="header-right">
    <span class="count">{total}</span>
    albums
    {badge}
  </div>
</header>

<button class="btn-random" onclick="openRandom()">&#9654; Random</button>
<button class="btn-top" onclick="window.scrollTo({{top:0,behavior:'smooth'}})" id="btn-top"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3,11 8,5 13,11"/></svg></button>

<nav class="letter-nav">
  <div id="sort-controls" class="sort-controls">
    <button class="sort-btn active" data-sort="artist" onclick="setSort('artist')">A–Z</button>
    <button class="sort-btn" data-sort="year" onclick="setSort('year')">Year</button>
  </div>
  <span class="nav-sep"></span>
  <span id="nav-letters">{letters_html}</span>
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

// ── Card renderer ─────────────────────────────────────────────────────────────

function renderCard(r) {{
  const coverSrc = r.local_cover || r.thumb || '';
  const onerr = (r.local_cover && r.thumb) ? ` onerror="this.onerror=null;this.src='${{esc(r.thumb)}}'"` : '';
  const coverHtml = coverSrc
    ? '<img src="' + esc(coverSrc) + '" alt="' + esc(r.title) + '" loading="lazy"' + onerr + '>'
    : '<div class="cover-placeholder">' + ((r.artist||'?')[0].toUpperCase()) + '</div>';
  const yr = r.master_year || r.year || '';
  const yearHtml  = yr ? '<span class="year">' + yr + '</span>' : '';
  const genres    = (r.genres||[]).slice(0,2).join(', ');
  const genreHtml = genres ? '<span class="genre">' + esc(genres) + '</span>' : '';
  const fmtHtml   = '';
  const colorHtml = r.vinyl_color ? '<span class="vinyl-color">' + esc(r.vinyl_color) + '</span>' : '';
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

// ── Sort modes ────────────────────────────────────────────────────────────────

function renderByArtist() {{
  const sorted = [...COLLECTION].sort((a, b) => {{
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
  const sorted = [...COLLECTION].sort((a, b) => {{
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

function setSort(mode) {{
  document.querySelectorAll('.sort-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sort === mode)
  );
  if (mode === 'year') renderByYear(); else renderByArtist();
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

function showModal(idx) {{
  const r = COLLECTION[idx];
  const cover = document.getElementById('modal-cover');
  const mSrc = r.local_cover || r.thumb || '';
  const mErr = (r.local_cover && r.thumb) ? ` onerror="this.onerror=null;this.src='${{esc(r.thumb)}}'"` : '';
  cover.innerHTML = mSrc
    ? '<img src="' + esc(mSrc) + '" alt=""' + mErr + '>'
    : '<div class="cover-placeholder">' + ((r.artist||'?')[0].toUpperCase()) + '</div>';
  document.getElementById('modal-title').textContent  = r.title;
  document.getElementById('modal-artist').textContent = r.artist;
  const rows = [];
  if (r.master_year)                 rows.push(['First Release', r.master_year]);
  if (r.year)                        rows.push(['Release Year',  r.year]);
  if (r.genres && r.genres.length)   rows.push(['Genre',  r.genres.join(', ')]);
  if (r.formats && r.formats.length) rows.push(['Format', r.formats.join(', ')]);
  if (r.vinyl_color)                 rows.push(['Details', r.vinyl_color]);
  document.getElementById('modal-details').innerHTML = rows
    .map(([l, v]) => '<div class="row"><span class="label">' + l + '</span><span class="value">' + esc(v) + '</span></div>')
    .join('');
  document.getElementById('modal-pos').textContent = (_modalPos + 1) + ' / ' + _displayOrder.length;
  document.getElementById('modal-prev').disabled = _modalPos <= 0;
  document.getElementById('modal-next').disabled = _modalPos >= _displayOrder.length - 1;
  document.getElementById('modal').classList.add('open');
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
