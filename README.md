# Discogs Collection Viewer

A local, self-updating HTML page of your Discogs record collection.
No server, no dependencies beyond Python 3 (built-in).

---

## Setup

1. **Download** `sync.py` into a folder, e.g. `~/discogs-collection/`

2. **Run it once** with your Discogs username:
   ```bash
   cd ~/discogs-collection
   python3 sync.py YOUR_DISCOGS_USERNAME
   ```

3. **Open** `index.html` in any browser. Done.

---

## What it creates

```
discogs-collection/
├── sync.py          ← the script
├── collection.json  ← your data (auto-updated)
├── index.html       ← your collection page (auto-generated)
└── covers/          ← downloaded album art
    ├── 1234567.jpg
    └── ...
```

---

## Daily Auto-Sync (cron on Mac)

Run this once in Terminal to add a daily 8am sync:

```bash
(crontab -l 2>/dev/null; echo "0 8 * * * cd $HOME/discogs-collection && python3 sync.py YOUR_DISCOGS_USERNAME >> sync.log 2>&1") | crontab -
```

Replace `YOUR_DISCOGS_USERNAME` with your actual username.
Replace `$HOME/discogs-collection` with the actual path if different.

To verify it was added:
```bash
crontab -l
```

To remove it:
```bash
crontab -e   # then delete the line
```

---

## Notes

- **Rate limiting**: The script waits 1 second between Discogs API pages and 0.3s between image downloads. This keeps you well under Discogs' 60 requests/minute limit.
- **Incremental**: On subsequent runs, covers already downloaded are skipped. Only new albums trigger downloads.
- **Private collections**: Won't work — Discogs API requires OAuth for private collections. Public collections work with zero auth.
- **Large collections**: 500+ records will take a few minutes on first run (image downloads). After that, syncs are fast.
