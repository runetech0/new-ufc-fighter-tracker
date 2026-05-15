# UFC Fighter Tracker

Automatically tracks new and removed fighters on the [UFC athlete roster](https://www.ufc.com/athletes/all) and posts a tweet for each change.

---

## How It Works

1. **First run** — launches a browser, scrapes every athlete from the UFC website, and caches them all in a local SQLite database. No tweets are posted during this phase.
2. **Polling loop** — every 20 minutes, re-scrapes the full roster and compares it against the database:
   - Athletes not yet in the database → saved and tweeted as **new**.
   - Athletes previously in the database but no longer on the roster → marked as removed and tweeted as **removed**.
3. **Tweet formats** — both include the fighter's portrait image if available:
   ```
   ✅ New Fighter Added!
   Jon Jones "Bones"
   Division: Heavyweight
   Record: 27-1-0
   🔗 https://www.ufc.com/athlete/jon-jones
   ```
   ```
   ❌ Fighter Removed from Roster!
   Jon Jones "Bones"
   Division: Heavyweight
   Record: 27-1-0
   ```
4. **TEST_MODE** — when enabled in `config.toml`, two test tweets are posted after every scan (one random active fighter, one random removed fighter) so you can verify the full pipeline without waiting for a genuine roster change.

---

## Architecture

```
main.py               — Entry point. Wires up browser, poster, and tracker.
app/
  browser.py          — Patchright browser that navigates UFC.com, captures
                        the AJAX session (URL + headers), and extracts the
                        first server-rendered athlete batch.
  scraper.py          — Async HTTP scraper (httpx). Fetches all paginated
                        AJAX pages concurrently using 5 workers and a
                        shared page counter protected by asyncio.Lock.
  tracker.py          — Orchestrator. Manages the first-run cache, the
                        polling loop, and coordinates saving, removal
                        detection, and tweeting.
  poster.py           — Self-contained Patchright browser that logs into
                        X (Twitter) via auth_token cookie, posts tweets
                        with optional image, and closes itself after use.
  db.py               — aiosqlite helpers (init, save, count, removal,
                        random athlete).
  models.py           — Athlete dataclass.
  config_reader.py    — Reads config.toml into typed dataclasses.
```

---

## Requirements

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (package manager)
- A valid X (Twitter) `auth_token` cookie for the posting account

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd ufc-tracker
uv sync
```

### 2. Install Patchright browser

```bash
uv run patchright install chromium
```

### 3. Configure

Rename `sample-config.toml` to `config.toml` and fill in the values:

```toml
[BROWSER]
HEADLESS = true                 # Set to false to watch the UFC scraper browser
PAGE_LOAD_TIMEOUT_SECONDS = 30

[TWITTER]
AUTH_TOKEN = "your_auth_token_here"   # X.com auth_token cookie value
SCREENSHOTS_DIR = "screenshots"       # Folder for debug screenshots
TEST_MODE = false                     # Set to true to post test tweets after every scan
```

#### Getting your `auth_token`

1. Log into [x.com](https://x.com) in your browser
2. Open DevTools → Application → Cookies → `https://x.com`
3. Copy the value of the `auth_token` cookie
4. Paste it into `config.toml`

---

## Running

```bash
uv run main.py
```

On the **first run**, the bot will silently cache all ~3 000+ UFC fighters and then enter the polling loop. On every subsequent poll it will tweet any fighters that are new or have been removed since the last scan.

---

## Database

Fighter data is stored in `athletes.db` (SQLite). Schema:

| Column        | Type    | Description                                      |
|---------------|---------|--------------------------------------------------|
| `profile_url` | TEXT    | Primary key — UFC profile URL                    |
| `name`        | TEXT    | Fighter full name                                |
| `nickname`    | TEXT    | Fight nickname (nullable)                        |
| `weight_class`| TEXT    | Division (nullable)                              |
| `record`      | TEXT    | W-L-D record string (nullable)                   |
| `image_url`   | TEXT    | Fighter portrait image URL (nullable)            |
| `is_active`   | INTEGER | `1` = on roster, `0` = removed from roster       |

> **Note:** If you have an existing `athletes.db` from an older version, missing columns (`image_url`, `is_active`) are added automatically on startup and backfilled during the next scrape.

---

## Screenshots

The poster browser saves screenshots at key steps to `screenshots/` for debugging:

- `tweet_composer_loaded_N.png` — compose page opened
- `media_upload_done_N.png` — after media attached
- `posted_tweet_N.png` — after successful post

---

## Project Structure

```
ufc-tracker/
├── app/
│   ├── browser.py
│   ├── config_reader.py
│   ├── db.py
│   ├── logs_config.py
│   ├── models.py
│   ├── poster.py
│   ├── scraper.py
│   └── tracker.py
├── screenshots/
├── sessions/
├── tests/
│   └── test_poster.py
├── config.toml
├── main.py
├── pyproject.toml
└── README.md
```

---

## Support

For help or questions, reach out on Telegram: [@runetech](https://t.me/runetech)
