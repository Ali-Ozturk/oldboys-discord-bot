# CS2 Discord Match Bot

Local-first Discord bot that reads match URLs in `data/kampliste.txt`, scrapes their HTML, and provides automatic match updates plus `/matches`, `/results`, and `/scoreboard`.

## Features in this first version

- Polls tracked match pages and detects veto/live and finished states.
- Maintains one persistent Discord post per match. It creates or updates the post when a match enters veto/live, is within 30 minutes of kickoff, or finishes; live score/map changes edit that post, and the final result replaces its live status.
- The post includes current map scores and rounds when available, completed map scores, and the final series result plus MVP and rating when available.
- `/refresh` makes `data/kampliste.txt` the source of truth: it refreshes current URLs, updates schedules, and clears saved tracking state for removed URLs. Existing Discord posts for removed matches remain as history.
- `/matches` fetches the tracked URLs and lists future matches with readable start times.
- `/results` fetches tracked pages and displays finished match results, map scores, and MVP/rating when available.
- `/scoreboard` averages each configured player's EsR ratings across finished tracked matches. Players with no matches are included with zero played.
- Uses SQLite at `data/matchbot.sqlite3` for snapshots and event keys, so notifications are deduplicated across restarts.
- Stores each match's Discord post ID in SQLite, so restarts continue editing the same post. Refreshes far-future fixtures every six hours by default, then increases to the configured polling interval near kickoff and during veto/live play.

All slash-command responses are ephemeral. Automatic notifications go to the configured channel. The bot suppresses mentions from scraped names/content.

## Requirements

- Python 3.11+ (for `zoneinfo`)
- A Discord application/bot with the `bot` and `applications.commands` install scopes
- Bot permission to view and send messages in the notification channel

## Run locally

1. Create a Discord application and bot in the Discord Developer Portal. Copy its bot token.
2. Invite it to your server with `bot` and `applications.commands` scopes, and allow it to view/send messages in the announcement channel.
3. Create a virtual environment and install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

   On Ubuntu, use `python3 -m venv .venv`, activate with `source .venv/bin/activate`, then run `pip install -r requirements.txt`.

4. Copy `.env.example` to `.env` and set `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID`. Optionally set `DISCORD_GUILD_ID` while developing; guild-scoped slash commands normally appear faster than global commands.
5. Put one full Esportligaen match URL per line in `data/kampliste.txt`.
6. Start with `python bot.py` (or `py bot.py` on Windows).

Keep `.env` private. The SQLite database is created on first run. Back up `data/matchbot.sqlite3` along with the bot if you want to preserve notification history across a move to Ubuntu. Stop the process before copying the database.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | required | Discord bot token |
| `DISCORD_CHANNEL_ID` | required for announcements | Channel ID for automatic updates/reminders |
| `DISCORD_GUILD_ID` | unset | Optional development guild for fast command sync |
| `MATCH_URLS_FILE` | `data/kampliste.txt` | Tracked URL list |
| `PLAYERS_FILE` | `data/players.txt` | Player profile URLs and scoreboard display names (`profile URL | nickname`) |
| `POLL_INTERVAL_SECONDS` | `25` | Scrape cadence near kickoff and while veto/live; values below 25 are raised to 25 |
| `SCHEDULE_REFRESH_HOURS` | `6` | How often to refresh a fixture while kickoff is far away |
| `ACTIVE_WINDOW_BEFORE_MINUTES` | `120` | Start frequent polling this many minutes before kickoff |
| `ACTIVE_WINDOW_AFTER_HOURS` | `3` | Continue frequent polling this long after kickoff if still upcoming |
| `TIMEZONE` | `Europe/Copenhagen` | Used for page dates that omit an offset |
| `LOG_LEVEL` | `INFO` | Python logging level |

## Current limitations to know

The supplied captures include an upcoming page and a BO2 with map 1 finished while map 2 is live. Completed live-series maps use the `.single_match_map.finished` class and `.score_finished_tag`; individual finished-series map scores can also be read from `.matchroom-scoreboard__meta-score`, including BO1 pages without `.map-results__card`. The parser prefers the visible `DD-MM-YYYY HH:mm` kickoff (interpreted in `TIMEZONE`); `/matches`, reminders, and poll scheduling use that normalized instant. `/refresh` forces an immediate schedule update if the site changes the date/time.

The status post is edited as the page changes, so users see one evolving post per match rather than separate start, half-time, map-finished, and final messages. During live play it displays the currently observed score and map; after completion it displays the final result. Scraping is sequential and defaults to one request every 25 seconds per due tracked match. Far-future pages are revisited every six hours by default; adjust these settings only if the site permits it.

The site may change its markup, localization, or anonymous-page access. Unknown pages and fetch errors are logged/skipped and are not interpreted as a score of zero. Scoreboard ratings require the player's profile URL to match the URL linked from the match page.

## Ubuntu service

For initial deployment, run the same Python command inside a persistent `systemd` service under a dedicated user, with the project directory and `.env` readable only to that user. Store the SQLite database on persistent local disk. The bot reconnects through discord.py; systemd can restart the process after a crash. A concrete unit file can be added when the Ubuntu host's paths and service user are known.
