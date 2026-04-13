"""
Fetch original release dates from MusicBrainz for every album in collection.json.
Results saved to release_dates.json as: { "<discogs_release_id>": { "date": "YYYY-MM-DD", "source": "musicbrainz", ... } }
Rate limit: 1 req/sec (MusicBrainz requirement).
"""

import json
import time
import re
import urllib.request
import urllib.parse
from pathlib import Path

COLLECTION_FILE = Path("collection.json")
OUTPUT_FILE = Path("release_dates.json")
USER_AGENT = "VinylzCollectionDates/1.0 (adriandampc@gmail.com)"
SLEEP = 1.1  # seconds between requests

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}")
        return None

def search_musicbrainz(artist, title):
    """Search MusicBrainz release-group and return best first-release-date."""
    # Clean artist name (remove "The", trailing numbers like "(9)")
    artist_clean = re.sub(r'\s*\(\d+\)\s*$', '', artist).strip()

    query = urllib.parse.quote(f'releasegroup:"{title}" AND artist:"{artist_clean}"')
    url = f"https://musicbrainz.org/ws/2/release-group/?query={query}&fmt=json&limit=5"
    data = fetch(url)
    if not data:
        return None, None

    groups = data.get("release-groups", [])
    if not groups:
        # Fallback: looser search
        query2 = urllib.parse.quote(f'"{title}" {artist_clean}')
        url2 = f"https://musicbrainz.org/ws/2/release-group/?query={query2}&fmt=json&limit=5"
        data = fetch(url2)
        time.sleep(SLEEP)
        if not data:
            return None, None
        groups = data.get("release-groups", [])

    for g in groups:
        score = int(g.get("score", 0))
        if score < 70:
            continue
        date = g.get("first-release-date", "")
        mbid = g.get("id", "")
        if date:
            return date, mbid

    return None, None

def main():
    releases = json.loads(COLLECTION_FILE.read_text())

    # Load existing results to allow resuming
    if OUTPUT_FILE.exists():
        results = json.loads(OUTPUT_FILE.read_text())
        print(f"Resuming — {len(results)} already fetched")
    else:
        results = {}

    total = len(releases)
    for i, r in enumerate(releases):
        rid = str(r["id"])
        if rid in results:
            continue

        artist = r["artist"]
        title = r["title"]
        master_year = r.get("master_year") or r.get("year")

        print(f"[{i+1}/{total}] {artist} — {title}", end=" ... ", flush=True)

        date, mbid = search_musicbrainz(artist, title)
        time.sleep(SLEEP)

        if date:
            print(f"✓ {date}")
            results[rid] = {
                "artist": artist,
                "title": title,
                "date": date,
                "master_year": master_year,
                "mbid": mbid,
                "source": "musicbrainz"
            }
        else:
            print(f"✗ not found (master_year: {master_year})")
            results[rid] = {
                "artist": artist,
                "title": title,
                "date": None,
                "master_year": master_year,
                "mbid": None,
                "source": None
            }

        # Save after every 10 records
        if (i + 1) % 10 == 0:
            OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    found = sum(1 for v in results.values() if v.get("date"))
    print(f"\nDone. {found}/{len(results)} dates found → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
