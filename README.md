# Resume Radar: Craigslist resume alerts

Watches the **resumes** section of all 413 US Craigslist cities for your keywords.
It searches titles and post text, the same way the craigslist.org search bar does.
Every match is kept in a local database. New posts appear on a dashboard and
as Chrome notifications.

## Run it

Requires Python 3.8 or newer. There are no packages to install.

```
python cl_monitor.py
```

1. Open **http://localhost:8765** in Chrome.
2. Click **Enable notifications** (the bell) and allow them.
3. Optional: click **Sound** to also play a chime for new posts.
4. Keep the tab open. It can stay in the background.

### Share it with a friend

1. Set a `password` in `config.json` and restart the bot.
2. Run `cloudflared tunnel --url http://localhost:8765`.
3. Send your friend the `https://….trycloudflare.com` link it prints.

The friend signs in once, and their Chrome remembers it.

## Dashboard

**Live scan panel**: shows that the bot is working and what it's doing right now.
- What it's doing now: checking a city (e.g. *Checking dallas / fort worth (TX)*),
  waiting before the next city, on a short break, waiting for the next round, or paused after a block.
- The round number, cities checked so far (e.g. `57 / 413`), when the round started, and the estimated time left.
- The last request (city, number of results, number of new posts) and a countdown to the next one.
- "Running for …" and "Last request … ago". If the bot stops answering, the panel turns red
  and says **Bot is not responding**.
- A feed of the last 25 requests, with time, city, results, new posts, how long the request took, and OK, Error or Blocked.

The terminal prints one line per request, too:
```
12:28:28 [round 1 · 6/413] chicago: 40 result(s), 0 new (0.61s)
```

**Matches**

- **Unread / All / Starred / Hidden** tabs. The tab title shows the unread count.
- Opening a post marks it read. Each post has buttons to star, mark read or unread, and hide, all with Undo.
- Search titles and cities (press `/` to jump to the search box), filter by state,
  and sort by "newest found" or "newest posted".
- **Mark all read** marks everything read, or only the selected state.
- The side panel shows new posts per day for the last 14 days, recent scan rounds, and the last error.

Badges on each post:

| Badge | Meaning |
|---|---|
| **new** | Appeared while the bot was watching. You were notified. |
| **existing** | Was already posted when the bot first scanned that city. No notification. |
| **repost** | Same title already posted in the same city within 30 days. No notification. |

## How the database works

Everything is stored in `matches.db` (SQLite) next to the script:
- every match, with read, starred and hidden status;
- which cities have been scanned;
- a log of each scan round.

Read status is shared: if your friend marks a post read, it's read for you too.
To start over, stop the bot and delete `matches.db`.

## Request pacing

To be gentle with Craigslist and look less like a bot:
- the bot waits a **random 3–8 s** between requests;
- it **shuffles the city order** every round;
- it takes a **30–90 s break every 40–70 requests**;
- it waits a **random 5–15 min** between rounds;
- if Craigslist blocks a request (403 or 429), it pauses 15 min,
  then doubles the pause each time it's blocked again in a row, up to 2 hours.

One round over all 413 cities takes about **45 minutes**. A new post is usually caught within about an hour.

## Settings (`config.json`)

Ranges like `[3, 8]` mean "a random value between 3 and 8".
Restart the bot after changing anything.

| key | meaning |
|---|---|
| `keywords` | Search terms, e.g. `["any work", "any job"]`. Each one is a separate search, the same as typing it into the Craigslist search bar. |
| `post_category` | Code used in each post's link. Resumes are searched under `rrr`, but each post's address uses `res`. Default `res`, normally left alone. |
| `areas` | `"all"`, or city hostnames such as `["orangecounty", "losangeles"]`. Hostnames are listed in `us_areas.json`. |
| `delay_between_requests_seconds` | Random gap between requests. |
| `long_break_every_requests` / `long_break_seconds` | How often to take a longer break, and how long it lasts. |
| `pause_between_rounds_minutes` | Random wait after each full round. |
| `shuffle_city_order` | Visit cities in a different order every round. |
| `backoff_minutes_when_blocked` / `max_backoff_minutes` | Pause after a block, and the longest pause allowed. |
| `alert_on_first_scan` | `true` = also notify for posts that already existed when a city was first scanned. |
| `alert_on_reposts` / `repost_window_days` | Whether to notify for reposts, and how far back to look for the same title. |
| `host` / `port` | Where the dashboard listens. Keep `127.0.0.1` when using a Cloudflare tunnel. |
| `password` | Empty = no login. Set one before sharing a tunnel link. |
| `user_agent` | The name the bot sends to Craigslist. |
