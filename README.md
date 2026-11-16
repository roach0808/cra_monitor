# Craigslist resume alerts

Checks the **resumes** section of all 413 US Craigslist cities for your keywords.
It searches titles and post text, the same way the craigslist.org search bar does.
When a new matching post appears, Chrome shows a notification.

## Run it

Requires Python 3.8 or newer. There are no packages to install.

```
python cl_monitor.py
```

1. Open **http://localhost:8765** in Chrome.
2. Click **Enable notifications** and allow them.
3. Keep that tab open. It can stay in the background, but Chrome itself must be running.

Click a notification to open the post.

## How it works

- One request per city per keyword. With 1 keyword that's 413 requests per round.
  At 3 seconds apart, a full round takes about 20 minutes. The next round starts 5 minutes later.
- **First round:** posts that already exist are saved and listed on the page,
  marked "already there at start". They don't trigger notifications.
- **After that:** each new post triggers one notification.
  Matches are stored in `matches.db`, so a restart never alerts on the same post twice.
- To start fresh, stop the bot and delete `matches.db`.

## Settings (`config.json`)

| key | meaning |
|---|---|
| `keywords` | Search terms, e.g. `["any work", "any job"]`. Each one is a separate search, the same as typing it into the Craigslist search bar. |
| `areas` | `"all"`, or a list of city hostnames, e.g. `["orangecounty", "losangeles"]`. Hostnames are listed in `us_areas.json`. |
| `seconds_between_requests` | Pause between requests. Lower is faster, but Craigslist is more likely to block you. |
| `seconds_between_rounds` | Pause after each full round of all cities. |
| `backoff_minutes_when_blocked` | How long to wait if Craigslist returns 403 or 429. |
| `alert_on_first_scan` | `true` = notify for every existing match on the first round, too. |
| `port` | Port for the local page. |

Restart the bot after changing settings.
