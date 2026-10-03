# Fresh India News Bot

Every 30 min: the 10 biggest breaking stories (incl. crime/violence), published in the last ~45 min, from 12 sources.
Scrapes the websites directly (no RSS), reads each article's publish time, dedupes with SQLite.

## Setup
1. @BotFather -> /newbot -> copy token. Also /setprivacy -> Disable (so it works in groups).
2. Push this folder to GitHub.
3. Railway: New Project -> Deploy from GitHub repo. Add a Volume mounted at `/data`.
4. Variables: BOT_TOKEN, OWNER_IDS, DB_PATH=/data/newsbot.db (see .env.example).
5. Start command: `python bot.py` (Procfile already has it).

## Use
- Group: add bot, make admin -> send `/subscribe`
- Topic/section group: inside each topic send `/subscribe india` / `world` / `business` / `sports` / `crime` / `all`
- Channel: add bot as admin (post permission) -> post `/subscribe` in the channel
  (or put the channel id in TARGETS env)
- `/unsubscribe`, `/status`, owner-only `/fetchnow`

## Check scrapers before going live
    pip install -r requirements.txt
    python scraper.py
Shows how many fresh articles each source returns. Fix the `pattern` of any source that shows 0.
