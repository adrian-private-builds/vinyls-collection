"""
Retry fetching full dates for year-only entries in release_dates.json.
Strategy:
  1. MusicBrainz release-group/{mbid}?inc=releases — pick earliest full date
  2. Wikipedia API search — parse infobox release date
"""

import json, time, re, urllib.request, urllib.parse
from pathlib import Path

OUTPUT_FILE = Path("release_dates.json")
USER_AGENT  = "VinylzCollectionDates/1.0 (adriandampc@gmail.com)"
SLEEP       = 1.1

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ERR {url}: {e}")
        return None

def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ERR {url}: {e}")
        return None

def mb_by_mbid(mbid):
    """Fetch release group releases and return earliest full date."""
    url = f"https://musicbrainz.org/ws/2/release-group/{mbid}?inc=releases&fmt=json"
    data = fetch(url)
    time.sleep(SLEEP)
    if not data:
        return None
    releases = data.get("releases", [])
    dates = []
    for rel in releases:
        d = rel.get("date", "")
        parts = d.split("-")
        if len(parts) == 3 and all(parts):
            dates.append(d)
    if dates:
        return sorted(dates)[0]
    return None

def wikipedia_date(artist, title):
    """Search Wikipedia for the album page and extract release date."""
    query = urllib.parse.quote(f"{title} {artist} album")
    search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json&srlimit=3"
    data = fetch(search_url)
    time.sleep(SLEEP)
    if not data:
        return None

    results = data.get("query", {}).get("search", [])
    for result in results[:2]:
        page_title = result["title"]
        # Fetch page extract
        page_url = f"https://en.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(page_title)}&prop=revisions&rvprop=content&rvslots=main&format=json"
        page_data = fetch(page_url)
        time.sleep(SLEEP)
        if not page_data:
            continue

        pages = page_data.get("query", {}).get("pages", {})
        for page in pages.values():
            content = page.get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("*", "")
            if not content:
                continue

            # Look for release date patterns in infobox
            patterns = [
                r'\|\s*released\s*=\s*\{\{(?:start date|release date)[^}]*?(\d{4})\s*[,|]\s*(\d{1,2})\s*[,|]\s*(\d{1,2})',
                r'\|\s*released\s*=\s*(\w+ \d+,? \d{4})',
                r'\|\s*released\s*=.*?(\d{1,2})\s+(\w+)\s+(\d{4})',
            ]
            for pat in patterns:
                m = re.search(pat, content, re.IGNORECASE)
                if m:
                    groups = m.groups()
                    if len(groups) == 3 and groups[0].isdigit() and len(groups[0]) == 4:
                        # YYYY, MM, DD
                        try:
                            return f"{int(groups[0]):04d}-{int(groups[1]):02d}-{int(groups[2]):02d}"
                        except:
                            pass
                    elif len(groups) == 1:
                        # Parse "Month DD, YYYY" or "DD Month YYYY"
                        raw = groups[0].strip()
                        for fmt in ["%B %d, %Y", "%B %d %Y", "%d %B %Y"]:
                            try:
                                from datetime import datetime
                                dt = datetime.strptime(raw, fmt)
                                return dt.strftime("%Y-%m-%d")
                            except:
                                pass
                    elif len(groups) == 3:
                        # DD, Month, YYYY
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(f"{groups[0]} {groups[1]} {groups[2]}", "%d %B %Y")
                            return dt.strftime("%Y-%m-%d")
                        except:
                            pass
    return None

def main():
    results = json.loads(OUTPUT_FILE.read_text())

    year_only = {k: v for k, v in results.items()
                 if v.get("date") and len(v["date"]) <= 4}
    print(f"Retrying {len(year_only)} year-only entries...\n")

    improved = 0
    for rid, v in year_only.items():
        artist, title, mbid = v["artist"], v["title"], v.get("mbid", "")
        print(f"  {artist} — {title} ({v['date']})", end=" ... ", flush=True)

        date = None

        # Strategy 1: MusicBrainz by MBID
        if mbid:
            date = mb_by_mbid(mbid)

        # Strategy 2: Wikipedia
        if not date:
            date = wikipedia_date(artist, title)

        if date and len(date) > 4:
            print(f"✓ {date}")
            results[rid]["date"] = date
            results[rid]["source"] = "musicbrainz-releases" if mbid else "wikipedia"
            improved += 1
        else:
            print("✗ still not found")

        # Save every 5
        if improved % 5 == 0 and improved > 0:
            OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nImproved {improved}/{len(year_only)} entries → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
