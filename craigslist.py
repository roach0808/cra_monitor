"""Fetch and decode Craigslist search results.

Uses the same search endpoint the craigslist.org search page uses, so a
keyword query matches titles AND post text, exactly like the search bar.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

SEARCH_API = "https://sapi.craigslist.org/web/v8/postings/search/full"


class Blocked(Exception):
    """Craigslist refused the request (HTTP 403 / 429)."""

    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def fetch(area, keyword, category, user_agent, timeout=30):
    params = {
        "batch": f"{area['id']}-0-360-0-0",
        "cc": "US",
        "lang": "en",
        "searchPath": category,
        "query": keyword,
    }
    req = urllib.request.Request(
        SEARCH_API + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(e.code) from e
        raise


def parse(payload, area, keyword, category):
    """Turn the compact search response into posts with real URLs."""
    data = payload.get("data") or {}
    dec = data.get("decode") or {}
    min_id = dec.get("minPostingId", 0)
    min_date = dec.get("minPostedDate", 0)
    locations = dec.get("locations") or []
    descriptions = dec.get("locationDescriptions") or []
    posts = []
    for item in data.get("items") or []:
        try:
            pid = min_id + int(item[0])
            posted = min_date + int(item[1])
            title = item[-1] if isinstance(item[-1], str) else ""
            slug = next((x[1] for x in item if isinstance(x, list) and len(x) > 1 and x[0] == 6), "post")

            host, sub, place = area["host"], "", ""
            loc = item[4] if len(item) > 4 and isinstance(item[4], str) else ""
            parts = loc.split("~")[0].split(":")
            if parts and parts[0].isdigit():
                idx = int(parts[0])
                if 0 < idx < len(locations) and isinstance(locations[idx], list):
                    entry = locations[idx]
                    host = entry[1] if len(entry) > 1 and entry[1] else host
                    sub = entry[2] if len(entry) > 2 and entry[2] else ""
            if len(parts) >= 3 and parts[2].isdigit():
                d = int(parts[2])
                if 0 < d < len(descriptions) and isinstance(descriptions[d], str):
                    place = descriptions[d]

            path = f"{sub}/" if sub else ""
            posts.append({
                "id": pid,
                "title": title.strip(),
                "url": f"https://{host}.craigslist.org/{path}{category}/d/{slug}/{pid}.html",
                "area": area["name"],
                "area_host": area["host"],
                "state": area.get("state", ""),
                "place": place.strip(),
                "keyword": keyword,
                "posted": posted,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return posts
