"""
Platform adapters — where brand numbers come from (Phase 5D, ADR-0004).

Real platform data only: an official API, or the platform's own analytics export. An
adapter returns ``None`` / ``[]`` when it has nothing and **never** fills a metric with a
default number; a metric the platform does not give stays ``None``. Adapters never raise:
a failure is returned as a message for the caller to store on the account.

  YouTubeAdapter         YouTube Data API v3 with an API key (public data of the channel)
  LinkedInExportAdapter  the CSV saved from LinkedIn's creator analytics export
"""

import csv
import io
import os
import re
from datetime import datetime, timezone

import httpx

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
MAX_VIDEOS = 200
PAGE = 50
SHORT_MAX_SECONDS = 60
TIMEOUT = 15

METRIC_KEYS = ("views", "likes", "comments", "shares", "saves", "impressions", "watch_time_s")


class AdapterError(Exception):
    """A platform could not be read. The message is safe to show."""


def _int(value):
    """An int from an API or CSV cell; None when there is no number (never 0 by default)."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if text in ("", "-", "—", "n/a", "N/A"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _iso_duration_seconds(text) -> int | None:
    """'PT1M5S' → 65. None when unreadable."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(text or ""))
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _parse_dt(text) -> datetime | None:
    """A naive UTC datetime from the formats the platforms use; None when unreadable."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y",
                "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


class PlatformAdapter:
    platform = ""

    def fetch_account(self, account) -> dict | None:
        """{followers, following, posts_count, total_views, raw}, or None."""
        return None

    def fetch_posts(self, account, since: datetime | None = None) -> list[dict]:
        """[{external_id, url, title, kind, posted_at, metrics: {…}, raw}]"""
        return []


# ── YouTube ──────────────────────────────────────────────────

class YouTubeAdapter(PlatformAdapter):
    platform = "youtube"

    def __init__(self):
        self.error: str | None = None
        self._uploads: str | None = None

    @staticmethod
    def key() -> str | None:
        return os.getenv("YOUTUBE_API_KEY") or None

    def _get(self, path: str, params: dict) -> dict:
        key = self.key()
        if not key:
            raise AdapterError("YOUTUBE_API_KEY not set")
        try:
            resp = httpx.get(f"{YOUTUBE_API}/{path}", params={**params, "key": key}, timeout=TIMEOUT)
        except Exception as exc:                       # noqa: BLE001 — never raise past the adapter
            raise AdapterError(f"YouTube could not be reached ({type(exc).__name__})")
        if resp.status_code != 200:
            try:
                reason = resp.json().get("error", {}).get("message") or ""
            except Exception:                          # noqa: BLE001
                reason = ""
            raise AdapterError(f"YouTube answered {resp.status_code}" + (f": {reason[:160]}" if reason else ""))
        try:
            return resp.json()
        except Exception:                              # noqa: BLE001
            raise AdapterError("YouTube sent an unreadable answer")

    def resolve_channel(self, handle_or_id: str) -> dict:
        """A handle ('@name' or 'name') or a channel id → {external_id, handle}. Raises
        AdapterError with a message that is safe to show."""
        text = (handle_or_id or "").strip()
        if not text:
            raise AdapterError("give the channel's handle or id")
        if re.fullmatch(r"UC[0-9A-Za-z_-]{20,}", text):
            data = self._get("channels", {"part": "snippet", "id": text})
            items = data.get("items") or []
            if not items:
                raise AdapterError("no YouTube channel has that id")
            custom = (items[0].get("snippet") or {}).get("customUrl")                # VERIFY: snippet.customUrl
            return {"external_id": text, "handle": custom or text}
        handle = text if text.startswith("@") else "@" + text
        data = self._get("channels", {"part": "id", "forHandle": handle})          # VERIFY: the forHandle parameter
        items = data.get("items") or []
        if items and items[0].get("id"):
            return {"external_id": items[0]["id"], "handle": handle}
        data = self._get("search", {"part": "snippet", "type": "channel", "q": handle.lstrip("@"), "maxResults": 1})
        items = data.get("items") or []
        channel_id = ((items[0].get("id") or {}).get("channelId") if items else None)   # VERIFY: search result shape
        if not channel_id:
            raise AdapterError(f"no YouTube channel found for {handle}")
        return {"external_id": channel_id, "handle": handle}

    def fetch_account(self, account) -> dict | None:
        self.error = None
        try:
            data = self._get("channels", {"part": "statistics,contentDetails", "id": account.external_id})
        except AdapterError as exc:
            self.error = str(exc)
            return None
        items = data.get("items") or []
        if not items:
            self.error = "the channel was not found"
            return None
        stats = items[0].get("statistics") or {}
        self._uploads = (((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads"))
        hidden = bool(stats.get("hiddenSubscriberCount"))
        return {
            "followers": None if hidden else _int(stats.get("subscriberCount")),
            "following": None,                                        # YouTube has no such number
            "posts_count": _int(stats.get("videoCount")),
            "total_views": _int(stats.get("viewCount")),
            "raw": stats,
        }

    def fetch_posts(self, account, since: datetime | None = None) -> list[dict]:
        if self._uploads is None and self.error is None:
            self.fetch_account(account)
        if not self._uploads:
            return []
        ids, token = [], None
        try:
            while len(ids) < MAX_VIDEOS:
                params = {"part": "contentDetails", "playlistId": self._uploads, "maxResults": PAGE}
                if token:
                    params["pageToken"] = token
                data = self._get("playlistItems", params)
                for item in data.get("items") or []:
                    vid = (item.get("contentDetails") or {}).get("videoId")
                    if vid and vid not in ids:
                        ids.append(vid)
                token = data.get("nextPageToken")
                if not token:
                    break
            ids = ids[:MAX_VIDEOS]
            posts = []
            for start in range(0, len(ids), PAGE):                    # 50 ids per call: one quota unit each
                batch = ids[start:start + PAGE]
                data = self._get("videos", {"part": "statistics,snippet,contentDetails", "id": ",".join(batch)})
                for item in data.get("items") or []:
                    snippet, stats = item.get("snippet") or {}, item.get("statistics") or {}
                    seconds = _iso_duration_seconds((item.get("contentDetails") or {}).get("duration"))
                    # VERIFY: the API does not flag Shorts; a duration of 60 s or less is the heuristic.
                    kind = "short" if seconds is not None and seconds <= SHORT_MAX_SECONDS else "video"
                    posted = _parse_dt(snippet.get("publishedAt"))
                    if since is not None and posted is not None and posted < since:
                        continue
                    posts.append({
                        "external_id": item.get("id"),
                        "url": f"https://www.youtube.com/watch?v={item.get('id')}",
                        "title": snippet.get("title"), "kind": kind, "posted_at": posted,
                        "metrics": {"views": _int(stats.get("viewCount")), "likes": _int(stats.get("likeCount")),
                                    "comments": _int(stats.get("commentCount")), "shares": None, "saves": None,
                                    "impressions": None, "watch_time_s": None},
                        "raw": {"statistics": stats, "duration_s": seconds},
                    })
            return posts
        except AdapterError as exc:
            self.error = str(exc)
            return []


# ── Analytics exports (CSV) ──────────────────────────────────

class ExportAdapter(PlatformAdapter):
    """Reads the CSV a user saved from a platform's own analytics export."""

    HEADERS: dict[str, str] = {}          # lower-cased csv header → field
    DEFAULT_KIND = "post"

    def __init__(self, text: str = ""):
        self.text = text or ""
        self.error: str | None = None

    def _rows(self) -> list[dict]:
        rows = list(csv.reader(io.StringIO(self.text)))
        # The header is the first row that names at least two known columns (exports often
        # carry a title line or two above the table).
        header_at = None
        for i, row in enumerate(rows[:15]):
            known = sum(1 for cell in row if cell.strip().lower() in self.HEADERS)
            if known >= 2:
                header_at = i
                break
        if header_at is None:
            self.error = "the file has none of the expected columns"
            return []
        header = [self.HEADERS.get(cell.strip().lower()) for cell in rows[header_at]]
        out = []
        for row in rows[header_at + 1:]:
            item = {}
            for field, cell in zip(header, row):
                if field and cell.strip() and field not in item:
                    item[field] = cell.strip()
            if item:
                out.append(item)
        return out

    def fetch_posts(self, account=None, since: datetime | None = None) -> list[dict]:
        posts = []
        for item in self._rows():
            url = item.get("url")
            external_id = item.get("external_id") or url
            if not external_id:
                continue
            posts.append({
                "external_id": external_id, "url": url,
                "title": (item.get("title") or "")[:300] or None,
                "kind": item.get("kind") or self.DEFAULT_KIND,
                "posted_at": _parse_dt(item.get("posted_at")),
                "metrics": {"views": _int(item.get("views")), "likes": _int(item.get("likes")),
                            "comments": _int(item.get("comments")), "shares": _int(item.get("shares")),
                            "saves": _int(item.get("saves")), "impressions": _int(item.get("impressions")),
                            "watch_time_s": None},
                "raw": item,
            })
        return posts


class LinkedInExportAdapter(ExportAdapter):
    platform = "linkedin"
    # VERIFY against a real export: these are the "Content" sheet's columns as documented
    # when this was written, with the synonyms seen in older exports.
    HEADERS = {
        "post url": "url", "url": "url", "post link": "url",
        "post publish date": "posted_at", "publish date": "posted_at", "date": "posted_at", "created date": "posted_at",
        "post title": "title", "title": "title", "post text": "title", "text": "title",
        "impressions": "impressions", "views": "views",
        "reactions": "likes", "likes": "likes",
        "comments": "comments",
        "reposts": "shares", "shares": "shares",
        "saves": "saves",
        "post type": "kind", "type": "kind",
        # "Engagement rate" is the platform's own ratio. It is not stored: the rate is always
        # recomputed from the counts so every platform is measured the same way.
    }


EXPORT_ADAPTERS = {"linkedin": LinkedInExportAdapter}
API_ADAPTERS = {"youtube": YouTubeAdapter}
PLATFORMS = ("youtube", "linkedin", "x", "instagram", "tiktok")
