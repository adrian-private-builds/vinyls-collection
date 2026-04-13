"""
Import exact release dates from album_release_dates.xlsx into release_dates.json.
- Splits multi-dates on /
- Parses various formats: "February 6, 1996", "Nov 18, 2002", "Mar 2003", "October 1984", "2016"
- Stores as dates[] array; date = last entry (for birthday year)
"""

import json, re
from datetime import datetime
from pathlib import Path
import openpyxl

XLSX   = Path("/Users/adrian/Downloads/album_release_dates.xlsx")
OUTPUT = Path("release_dates.json")

MONTH_ABBR = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
    "january":1,"february":2,"march":3,"april":4,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def parse_date(raw):
    """Parse a single date string into YYYY, YYYY-MM, or YYYY-MM-DD."""
    raw = raw.strip()
    # Strip noise words
    raw = re.sub(r'\b(reissue|original|comp|vinyl|release|later|earlier)\b', '', raw, flags=re.I).strip(' ,')

    # "Month DD, YYYY" or "Month DD YYYY"
    m = re.match(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$', raw)
    if m:
        mon = MONTH_ABBR.get(m.group(1).lower())
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"

    # "Month YYYY"
    m = re.match(r'([A-Za-z]+)\s+(\d{4})$', raw)
    if m:
        mon = MONTH_ABBR.get(m.group(1).lower())
        if mon:
            return f"{m.group(2)}-{mon:02d}"

    # "YYYY" only
    m = re.match(r'^(\d{4})$', raw)
    if m:
        return m.group(1)

    return None

def parse_multi(cell):
    """Split on / and parse each part."""
    if not cell or str(cell).strip() == "":
        return []
    parts = [p.strip() for p in str(cell).split("/")]
    dates = []
    for p in parts:
        d = parse_date(p)
        if d:
            dates.append(d)
    return dates

def normalize_artist(a):
    return re.sub(r'\s+', ' ', (a or "").strip().lower())

def main():
    results = json.loads(OUTPUT.read_text())

    # Build lookup: (normalized_artist, normalized_title) -> rid
    lookup = {}
    for rid, v in results.items():
        key = (normalize_artist(v["artist"]), normalize_artist(v["title"]))
        lookup[key] = rid

    wb = openpyxl.load_workbook(XLSX)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))[1:]

    updated = not_found = skipped = 0
    for row in rows:
        artist, title, year, date_cell, notes = row
        dates = parse_multi(date_cell)

        if not dates:
            print(f"  SKIP (no parseable date): {artist} — {title} | raw: {date_cell!r}")
            skipped += 1
            continue

        key = (normalize_artist(artist), normalize_artist(title))
        rid = lookup.get(key)

        if not rid:
            # Try partial match on title
            for (a, t), r in lookup.items():
                if normalize_artist(artist) in a or a in normalize_artist(artist):
                    if normalize_artist(title)[:20] in t or t[:20] in normalize_artist(title):
                        rid = r
                        break

        if not rid:
            print(f"  NOT FOUND: {artist} — {title}")
            not_found += 1
            continue

        results[rid]["dates"] = dates
        results[rid]["date"]  = dates[-1]  # last date = used for birthday year
        print(f"  OK: {artist} — {title} → {dates}")
        updated += 1

    OUTPUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nUpdated: {updated}, Not found: {not_found}, Skipped: {skipped}")

if __name__ == "__main__":
    main()
